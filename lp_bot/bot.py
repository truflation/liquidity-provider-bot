"""
Main Liquidity Provider Bot implementation.

Runs on a scheduler, checking order book state after each block
and updating positions to maintain liquidity within rewards-eligible bounds.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING

# Lazy import to avoid loading Go bindings on module import
if TYPE_CHECKING:
    from trufnetwork_sdk_py.client import TNClient

from .config import Config, StreamConfig, PricingMethod
from .models import OrderLevel, MarketState, BotOrder, PricingResult
from .pricing import calculate_mm_prices, ensure_uncrossed_spread
from .order_book import (
    build_market_state,
    levels_to_positive_prices,
)


logger = logging.getLogger(__name__)


@dataclass
class ActiveOrder:
    """Tracks an active order placed by the bot."""
    query_id: int
    outcome: bool
    side: str  # "bid" or "ask"
    price: int  # SDK format (negative for bids, positive for asks)
    amount: int


@dataclass
class MarketContext:
    """Context for a single market the bot is providing liquidity to."""
    query_id: int
    stream_config: StreamConfig
    current_orders: list[ActiveOrder] = field(default_factory=list)
    last_update_block: int = 0
    settle_time: Optional[int] = None


@dataclass
class OrderRecommendation:
    """A recommended order to be placed."""
    query_id: int
    stream_name: str
    outcome: bool
    side: str  # "bid" or "ask"
    price: int
    amount: int

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "stream_name": self.stream_name,
            "outcome": self.outcome,
            "side": self.side,
            "price": self.price,
            "amount": self.amount,
        }


@dataclass
class MarketSnapshot:
    """Snapshot of market state and recommendations."""
    query_id: int
    stream_name: str
    stream_id: str
    timestamp: str
    best_bid: Optional[int]
    best_ask: Optional[int]
    mid_price: Optional[float]
    lower_bound: int
    upper_bound: int
    calculated_bid: float
    calculated_ask: float
    recommendations: list[OrderRecommendation]

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "stream_name": self.stream_name,
            "stream_id": self.stream_id,
            "timestamp": self.timestamp,
            "market_state": {
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "mid_price": self.mid_price,
            },
            "bounds": {
                "lower": self.lower_bound,
                "upper": self.upper_bound,
            },
            "calculated_prices": {
                "bid": round(self.calculated_bid, 2),
                "ask": round(self.calculated_ask, 2),
            },
            "recommendations": [r.to_dict() for r in self.recommendations],
        }


class LiquidityProviderBot:
    """
    Liquidity Provider Bot for TRUF Network prediction markets.

    Provides liquidity by placing bid/ask orders within rewards-eligible
    bounds using configurable pricing strategies.
    """

    def __init__(
        self,
        config: Config,
        client: Optional["TNClient"] = None,
        read_only: bool = False,
        output_file: Optional[str] = None,
    ):
        """
        Initialize the LP Bot.

        Args:
            config: Bot configuration
            client: Optional TNClient instance. If not provided,
                    will be created using config credentials.
            read_only: If True, only monitor markets without placing orders.
                      Useful for testing or when no API token is available.
            output_file: Path to JSON file for writing order recommendations.
                        Defaults to 'lp_bot_orders.json' in read-only mode.
        """
        self.config = config
        self._client = client
        self._client_initialized = client is not None
        self.markets: dict[int, MarketContext] = {}
        self.running = False
        self.read_only = read_only
        self._last_block = 0

        # Set output file path
        if output_file:
            self.output_file = Path(output_file)
        elif read_only:
            self.output_file = Path("lp_bot_orders.json")
        else:
            self.output_file = None

        # Store latest snapshots for JSON output
        self._snapshots: list[MarketSnapshot] = []

    @property
    def client(self) -> "TNClient":
        """Lazily initialize the TNClient when first accessed."""
        if not self._client_initialized:
            # Skip client initialization if using sample data
            if self.config.use_sample_data:
                raise RuntimeError(
                    "TNClient not available in sample data mode. "
                    "This should not be called when use_sample_data=True."
                )

            if not self.config.api_token:
                raise RuntimeError(
                    "Cannot initialize TNClient without an API token (private key). "
                    "Set TRUF_API_TOKEN environment variable or provide api_token in config. "
                    "Note: The SDK requires a private key even for read operations."
                )
            # Validate key format (should be 64 hex chars)
            token = self.config.api_token.strip()
            if token.startswith("0x"):
                token = token[2:]
            if len(token) != 64:
                raise RuntimeError(
                    f"Invalid private key length: {len(token)} chars (expected 64 hex chars). "
                    "Generate a new key with: python -m lp_bot.main --generate-key"
                )
            # Import here to avoid loading Go bindings until needed
            from trufnetwork_sdk_py.client import TNClient
            logger.info(f"Initializing TNClient for {self.config.node_url}...")
            self._client = TNClient(
                url=self.config.node_url,
                token=token,
            )
            self._client_initialized = True
            logger.info("TNClient initialized successfully")
        return self._client

    def write_output(self) -> None:
        """Write current snapshots to JSON output file."""
        if not self.output_file:
            return

        output = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "config": {
                "pricing_method": self.config.pricing_method.value,
                "alpha": self.config.alpha,
                "default_order_amount": self.config.default_order_amount,
            },
            "markets": [s.to_dict() for s in self._snapshots],
        }

        with open(self.output_file, "w") as f:
            json.dump(output, f, indent=2)

        logger.info(f"Wrote {len(self._snapshots)} market snapshots to {self.output_file}")

    def register_market(self, query_id: int, stream_config: StreamConfig, settle_time: Optional[int] = None) -> None:
        """
        Register a market for liquidity provision.

        Args:
            query_id: The market's query ID
            stream_config: Configuration for this stream/market
            settle_time: Optional settlement timestamp (for pre-settlement cutoff)
        """
        self.markets[query_id] = MarketContext(
            query_id=query_id,
            stream_config=stream_config,
            settle_time=settle_time,
        )
        logger.info(
            f"Registered market {query_id} ({stream_config.name}) "
            f"with bounds ±{stream_config.bounds_pct*100:.0f}% from mid"
        )

    def discover_markets(self, limit: int = 50) -> list[dict]:
        """
        Discover active (non-settled) markets from the network.

        Args:
            limit: Maximum number of markets to fetch

        Returns:
            List of dicts with 'id' and 'settle_time' for active markets
        """
        try:
            markets = self.client.list_markets(limit=limit)
            active = []
            for m in markets:
                settled = m.get("settled", False) if isinstance(m, dict) else getattr(m, "settled", False)
                if settled:
                    continue
                qid = m.get("id") if isinstance(m, dict) else getattr(m, "id", None)
                st = m.get("settle_time") if isinstance(m, dict) else getattr(m, "settle_time", None)
                if qid is not None:
                    active.append({"id": int(qid), "settle_time": st})
            logger.info(f"Discovered {len(active)} active markets (out of {len(markets)} total)")
            return active
        except Exception as e:
            logger.error(f"Failed to discover markets: {e}")
            return []

    def get_market_state(self, query_id: int, outcome: bool = True) -> MarketState:
        """
        Fetch current market state from the order book.

        Args:
            query_id: Market ID
            outcome: True for YES shares, False for NO shares

        Returns:
            MarketState with current bids, asks, and best prices
        """
        # Use sample data if configured
        if self.config.use_sample_data:
            from .sample_data import get_sample_order_book_with_spread
            order_book = get_sample_order_book_with_spread(query_id, outcome)
            logger.debug(f"Using sample data for market {query_id}, outcome={outcome}")
            return build_market_state(
                query_id=query_id,
                outcome=outcome,
                order_book_entries=order_book,
                current_time=time.time(),
            )

        # Fetch from network
        try:
            order_book = self.client.get_order_book(query_id, outcome)
            return build_market_state(
                query_id=query_id,
                outcome=outcome,
                order_book_entries=order_book,
                current_time=time.time(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch market state for {query_id}: {e}")
            return MarketState(
                query_id=query_id,
                outcome=outcome,
                best_bid=None,
                best_ask=None,
                bid_levels=[],
                ask_levels=[],
            )

    def calculate_target_prices(
        self,
        market_state: MarketState,
        stream_config: StreamConfig,
    ) -> PricingResult:
        """
        Calculate target bid/ask prices for a market.

        Args:
            market_state: Current order book state
            stream_config: Configuration with bounds_pct

        Returns:
            PricingResult with calculated bid/ask prices
        """
        # Convert levels to positive prices for pricing module
        bids_positive = levels_to_positive_prices(market_state.bid_levels)
        asks_positive = levels_to_positive_prices(market_state.ask_levels)

        # Calculate dynamic bounds from mid price
        if market_state.mid_price is not None:
            mid = market_state.mid_price
        elif market_state.best_bid is not None and market_state.best_ask is not None:
            mid = (market_state.best_bid + market_state.best_ask) / 2
        else:
            # Fallback to 50 if no market data
            mid = 50.0

        lower_bound, upper_bound = stream_config.calculate_bounds(mid)

        logger.debug(
            f"Market {market_state.query_id}: mid={mid:.2f}, "
            f"bounds=[{lower_bound:.2f}, {upper_bound:.2f}] (±{stream_config.bounds_pct*100:.0f}%)"
        )

        result = calculate_mm_prices(
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            alpha=self.config.alpha,
            method=self.config.pricing_method,
            bids=bids_positive if bids_positive else None,
            asks=asks_positive if asks_positive else None,
            target_depth_pct=self.config.target_depth_pct,
            apply_time_decay=self.config.apply_time_decay,
            half_life_seconds=self.config.half_life_seconds,
        )

        # Ensure we don't create a crossed market
        result = ensure_uncrossed_spread(result, min_spread=1.0)

        return result

    def cancel_existing_orders(self, context: MarketContext) -> None:
        """
        Cancel all existing orders for a market.

        Args:
            context: Market context with current orders
        """
        if self.read_only:
            logger.debug(f"[READ-ONLY] Would cancel {len(context.current_orders)} orders")
            context.current_orders.clear()
            return

        for order in context.current_orders:
            if order.side == "ask":
                # Ask orders exist on both sides (split mint + sell).
                # Cancel NO-side and YES-side asks.
                for cancel_outcome, cancel_price in [
                    (order.outcome, order.price),
                    (not order.outcome, 100 - order.price),
                ]:
                    try:
                        self.client.cancel_order(
                            query_id=order.query_id,
                            outcome=cancel_outcome,
                            price=cancel_price,
                            wait=True,
                        )
                    except Exception as e:
                        err_str = str(e).lower()
                        if "order not found" in err_str or "old order not found" in err_str:
                            logger.debug(f"Order already gone for market {order.query_id}")
                        else:
                            logger.warning(f"Failed to cancel ask order: {e}")
            else:
                try:
                    self.client.cancel_order(
                        query_id=order.query_id,
                        outcome=order.outcome,
                        price=order.price,
                        wait=True,
                    )
                except Exception as e:
                    err_str = str(e).lower()
                    if "order not found" in err_str or "old order not found" in err_str:
                        logger.debug(f"Order already gone for market {order.query_id}")
                    else:
                        logger.warning(f"Failed to cancel bid order: {e}")

        context.current_orders.clear()

    def place_orders(
        self,
        context: MarketContext,
        pricing: PricingResult,
        market_state: MarketState,
        outcome: bool = True,
    ) -> list[ActiveOrder]:
        """
        Place bid and ask orders based on calculated prices.

        Args:
            context: Market context
            pricing: Calculated target prices
            market_state: Current market state (for snapshot)
            outcome: True for YES shares, False for NO shares

        Returns:
            List of placed ActiveOrder objects
        """
        placed_orders = []
        stream_config = context.stream_config
        amount = max(self.config.default_order_amount, stream_config.min_order_size)

        # Get bounds from pricing result (already calculated dynamically)
        lower_bound = int(pricing.lower_bound)
        upper_bound = int(pricing.upper_bound)

        # Convert float prices to integer cents
        bid_price_int, ask_price_int = pricing.to_int_prices()

        # Ensure prices are within bounds
        bid_price_int = max(lower_bound, min(bid_price_int, upper_bound - 1))
        ask_price_int = max(lower_bound + 1, min(ask_price_int, upper_bound))

        # Ensure bid < ask
        if bid_price_int >= ask_price_int:
            mid = (lower_bound + upper_bound) // 2
            bid_price_int = mid - 1
            ask_price_int = mid + 1

        # Build recommendations
        recommendations = [
            OrderRecommendation(
                query_id=context.query_id,
                stream_name=stream_config.name,
                outcome=outcome,
                side="bid",
                price=bid_price_int,
                amount=amount,
            ),
            OrderRecommendation(
                query_id=context.query_id,
                stream_name=stream_config.name,
                outcome=outcome,
                side="ask",
                price=ask_price_int,
                amount=amount,
            ),
        ]

        # Create snapshot for JSON output
        snapshot = MarketSnapshot(
            query_id=context.query_id,
            stream_name=stream_config.name,
            stream_id=stream_config.stream_id,
            timestamp=datetime.utcnow().isoformat() + "Z",
            best_bid=market_state.best_bid,
            best_ask=market_state.best_ask,
            mid_price=market_state.mid_price,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            calculated_bid=pricing.bid_price,
            calculated_ask=pricing.ask_price,
            recommendations=recommendations,
        )

        # Store snapshot (replace existing for same query_id)
        self._snapshots = [s for s in self._snapshots if s.query_id != context.query_id]
        self._snapshots.append(snapshot)

        # Read-only mode: log and return without placing
        if self.read_only:
            logger.info(
                f"[READ-ONLY] Market {context.query_id} ({stream_config.name}): "
                f"BID {bid_price_int} / ASK {ask_price_int} x{amount}"
            )
            return []

        # Place bid order
        try:
            tx_hash = self.client.place_buy_order(
                query_id=context.query_id,
                outcome=outcome,
                price=bid_price_int,
                amount=amount,
                wait=True,
            )
            bid_order = ActiveOrder(
                query_id=context.query_id,
                outcome=outcome,
                side="bid",
                price=-bid_price_int,  # SDK format
                amount=amount,
            )
            placed_orders.append(bid_order)
            logger.info(
                f"Placed bid at {bid_price_int} for {amount} shares "
                f"on market {context.query_id} (tx: {tx_hash[:16]}...)"
            )
        except Exception as e:
            logger.error(f"Failed to place bid order: {e}")

        # Place ask orders on BOTH sides of the book.
        # 1. place_split_limit_order mints pairs and sells NO at (100 - true_price)
        # 2. place_sell_order sells the retained YES shares as a YES ask
        try:
            self.client.place_split_limit_order(
                query_id=context.query_id,
                true_price=ask_price_int,
                amount=amount,
                wait=True,
            )
            tx_hash = self.client.place_sell_order(
                query_id=context.query_id,
                outcome=True,
                price=ask_price_int,
                amount=amount,
                wait=True,
            )
            ask_order = ActiveOrder(
                query_id=context.query_id,
                outcome=not outcome,  # Track as NO side (for cancel logic)
                side="ask",
                price=100 - ask_price_int,  # NO side price
                amount=amount,
            )
            placed_orders.append(ask_order)
            logger.info(
                f"Placed ask at {ask_price_int} (YES + NO@{100-ask_price_int}) "
                f"for {amount} shares on market {context.query_id} (tx: {tx_hash[:16]}...)"
            )
        except Exception as e:
            logger.error(f"Failed to place ask order: {e}")

        return placed_orders

    def should_update_orders(
        self,
        current_orders: list[ActiveOrder],
        new_pricing: PricingResult,
        threshold: float = 1.0,
    ) -> bool:
        """
        Determine if orders should be updated based on price change.

        Args:
            current_orders: Currently active orders
            new_pricing: Newly calculated prices
            threshold: Minimum price change to trigger update

        Returns:
            True if orders should be updated
        """
        if not current_orders:
            return True

        new_bid, new_ask = new_pricing.to_int_prices()

        for order in current_orders:
            current_price = abs(order.price)
            if order.side == "bid" and abs(current_price - new_bid) >= threshold:
                return True
            if order.side == "ask" and abs(current_price - new_ask) >= threshold:
                return True

        return False

    def update_market(self, query_id: int, outcome: bool = True) -> None:
        """
        Update orders for a single market.

        Args:
            query_id: Market ID to update
            outcome: True for YES shares, False for NO shares
        """
        context = self.markets.get(query_id)
        if not context:
            logger.warning(f"Market {query_id} not registered")
            return

        if not context.stream_config.enabled:
            return

        # Pull liquidity before settlement to protect capital
        if context.settle_time and self.config.pre_settlement_cutoff > 0:
            seconds_left = context.settle_time - int(time.time())
            if seconds_left <= self.config.pre_settlement_cutoff:
                if context.current_orders:
                    logger.info(
                        f"Market {query_id}: within pre-settlement cutoff "
                        f"({seconds_left}s left). Pulling liquidity."
                    )
                    self.cancel_existing_orders(context)
                return

        try:
            # Fetch current market state
            market_state = self.get_market_state(query_id, outcome)

            # Calculate target prices
            pricing = self.calculate_target_prices(
                market_state, context.stream_config
            )

            # Check if update is needed
            if not self.should_update_orders(context.current_orders, pricing):
                logger.debug(f"No update needed for market {query_id}")
                return

            # Cancel existing orders
            self.cancel_existing_orders(context)

            # Place new orders
            new_orders = self.place_orders(context, pricing, market_state, outcome)
            context.current_orders = new_orders

            logger.info(
                f"Updated market {query_id}: "
                f"bid={pricing.bid_price:.2f}, ask={pricing.ask_price:.2f}, "
                f"mid={pricing.mid_price:.2f}"
            )

        except Exception as e:
            logger.error(f"Error updating market {query_id}: {e}")

    def update_all_markets(self) -> None:
        """Update orders for all registered markets."""
        for query_id in self.markets:
            self.update_market(query_id)

    def run_once(self) -> None:
        """Run a single update cycle for all markets."""
        logger.debug("Running update cycle")
        self.update_all_markets()

        # Write JSON output after updating all markets
        if self.output_file:
            self.write_output()

    def run(self) -> None:
        """
        Run the bot continuously with scheduled updates.

        Checks for updates at the configured interval.
        """
        self.running = True
        mode = "READ-ONLY" if self.read_only else "LIVE"
        logger.info(
            f"Starting LP Bot [{mode}] with {len(self.markets)} markets, "
            f"alpha={self.config.alpha}, method={self.config.pricing_method.value}"
        )

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in update cycle: {e}")

            time.sleep(self.config.check_interval_seconds)

    def stop(self) -> None:
        """Stop the bot and cancel all active orders."""
        self.running = False
        logger.info("Stopping LP Bot...")

        # Cancel all active orders
        for context in self.markets.values():
            self.cancel_existing_orders(context)

        logger.info("LP Bot stopped")


def create_bot_from_config(config: Optional[Config] = None) -> LiquidityProviderBot:
    """
    Create and configure an LP Bot from config.

    Args:
        config: Configuration (defaults to loading from environment)

    Returns:
        Configured LiquidityProviderBot instance
    """
    from .config import load_config_from_env

    if config is None:
        config = load_config_from_env()

    bot = LiquidityProviderBot(config)

    # Note: Markets need to be registered separately since we need
    # the query_id which maps to a stream. This would typically be
    # done by looking up markets by stream_id.

    return bot
