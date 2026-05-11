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
from .inventory import InventoryManager
from .order_state import OrderStateManager


logger = logging.getLogger(__name__)


# Protocol minimum: orders below this notional (price_cents * amount) are
# silently reverted on chain even though the SDK returns a tx hash. Guard
# every place call with `_meets_min_notional()` to avoid tracking phantom
# orders. Mirrors the MM bot's MIN_ORDER_NOTIONAL_CENT_SHARES constant.
MIN_ORDER_NOTIONAL_CENT_SHARES = 100


def _meets_min_notional(price: int, amount: int) -> bool:
    """True if `price (cents) * amount (shares) >= protocol minimum`."""
    return price * amount >= MIN_ORDER_NOTIONAL_CENT_SHARES


def _derive_wallet_address(private_key: str) -> str:
    """Return the 0x-prefixed checksum address for the given hex private key.

    Used at client init to log which wallet the bot will sign as. A wrong
    env-file deploy is otherwise silent — the SDK just signs against
    whatever key it loaded.
    """
    from eth_account import Account
    key = private_key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key).address


def _compute_base_amount(
    price_cents: int,
    order_dollar_amount: Optional[float],
    default_amount: int,
) -> int:
    """Per-leg share count.

    With `order_dollar_amount` set, returns `ceil(dollars * 100 / price)`
    so a $2 BID at 1c is 200 shares and a $2 BID at 99c is ~3 shares —
    both around $2 notional. Without it, returns `default_amount` (the
    pre-Phase-2b behavior). Clamped to >=1.
    """
    if order_dollar_amount is None or order_dollar_amount <= 0:
        return max(1, default_amount)
    if price_cents <= 0:
        return max(1, default_amount)
    import math
    return max(1, math.ceil(order_dollar_amount * 100.0 / price_cents))


@dataclass
class ActiveOrder:
    """Tracks an active order placed by the bot.

    For both bids and asks, `outcome` and `tracked_outcome` are the logical
    outcome being quoted (YES or NO). The local record stores the LOGICAL
    price so should_update_orders compares against the right number on the
    next cycle.

    Two on-chain shapes are possible for an ASK:

    - **Inventory-backed** (`is_inventory_backed=True`): single sell on
      `outcome` at `price`. No mirror leg on the opposite book.
    - **Split-mint** (`is_inventory_backed=False`): the pair pattern. A
      sell on `outcome` at `price` plus an auto-listed sell on the
      opposite outcome at `100 - price`.

    Cancel and reconcile paths must consult this flag because the two
    shapes have different on-chain footprints. Cancelling a non-existent
    mirror leg on the inventory-backed path is harmless RPC waste at best
    and a same-price collision (bot cancels its own bid at the mirror
    price) at worst.
    """
    query_id: int
    outcome: bool  # Logical outcome this order quotes (YES or NO)
    side: str  # "bid" or "ask"
    price: int  # Logical price in cents. Bids stored negative (SDK convention); asks stored positive.
    amount: int
    tracked_outcome: bool = True  # Kept for backwards compat; equals `outcome` for all new orders.
    # True iff this ASK was placed via inventory-backed `place_sell_order`
    # (single leg). False for bids and for split-mint ASKs (two legs).
    is_inventory_backed: bool = False
    # Unix timestamp at placement. Drives `max_order_age` forced refresh.
    # Defaults to 0.0 (treated as "very old", forces a refresh) so any
    # ActiveOrder constructed without an explicit timestamp — including
    # those recovered on startup before chain truth confirms — gets
    # rotated through the place pipeline on the next cycle.
    created_at: float = 0.0


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

        # Per-market inventory accounting. Lets the bot back ASKs with
        # held shares (single-leg place_sell_order) instead of fresh
        # split-mints every cycle. Updated from chain via
        # `_refresh_inventory` which calls `get_user_positions`.
        self._inventory = InventoryManager()

        # Persistent order state for restart recovery. In read-only mode
        # we skip the file entirely so dry runs don't litter the cwd.
        if read_only:
            self._order_state: Optional[OrderStateManager] = None
        else:
            self._order_state = OrderStateManager(config.order_state_file)

        # Cycles since the last inventory refresh. Refreshes are
        # heavyweight (`get_user_positions` over many markets), so we
        # gate to every N cycles rather than every cycle.
        self._cycles_since_inventory_refresh = 0

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
            # Derive and log the wallet address before any broadcast. A
            # wrong-env-file deploy is otherwise silent: the bot just
            # signs against whatever key is loaded. Last-line confirmation.
            wallet_addr = _derive_wallet_address(token)
            logger.info(
                f"Initializing TNClient for {self.config.node_url} "
                f"as wallet {wallet_addr} (read_only={self.read_only})"
            )
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

    def _refresh_inventory(self) -> None:
        """Pull `get_user_positions` and rebuild per-market inventory.

        Held + listed counts are overwritten from chain truth. Bot-side
        reservations are left alone — they represent in-flight intent
        that hasn't yet landed on chain.

        Skipped in read-only mode and when the bot has no client
        (sample-data mode).
        """
        if self.read_only or self.config.use_sample_data:
            return
        try:
            positions = self.client.get_user_positions()
            # SDK returns a list of dicts in v0.7.x. Accept both dict
            # and object styles to stay defensive across versions.
            normalized = []
            for p in positions:
                if isinstance(p, dict):
                    normalized.append(p)
                else:
                    normalized.append({
                        "query_id": getattr(p, "query_id", None),
                        "outcome": getattr(p, "outcome", True),
                        "price": getattr(p, "price", 0),
                        "amount": getattr(p, "amount", 0),
                    })
            self._inventory.update_from_user_positions(normalized)
        except Exception as e:
            logger.warning(f"Failed to refresh inventory from chain: {e}")

    def _reconcile_orders_on_startup(self) -> None:
        """Match persisted local order state against chain truth.

        For each registered market we pull BOTH YES and NO order books,
        filter to the bot's wallet, and bucket entries by `(outcome,
        side)` into four maps: `bids[True]`, `bids[False]`, `asks[True]`,
        `asks[False]`.

        Then per tracked order:
        - **BID**: active iff `price` is in `bids[outcome]`.
        - **ASK, inventory-backed**: active iff `price` is in
          `asks[outcome]` (single on-chain leg, no mirror).
        - **ASK, split-mint**: active iff `price` is in `asks[outcome]`
          OR `100 - price` is in `asks[not outcome]` (either leg
          surviving means the pair is not fully gone).

        Side-and-outcome-aware matching avoids the price-collision
        bug where a YES BID at 30 falsely matched a YES ASK at 70 (whose
        split-mint mirror sits at 30).

        Recovered inventory-backed ASKs also re-apply their
        `MarketInventory.reserve_pair` reservation so the next cycle
        doesn't double-claim the same shares.

        Skipped in read-only mode (no order state manager) and in
        sample-data mode (no live SDK to query).
        """
        if self._order_state is None:
            return
        if self.config.use_sample_data:
            logger.info("Reconcile skipped: sample-data mode has no chain")
            return

        wallet_addr: Optional[str] = None
        try:
            token = self.config.api_token.strip()
            if token.startswith("0x"):
                token = token[2:]
            if len(token) == 64:
                wallet_addr = _derive_wallet_address(token).lower()
        except Exception:
            wallet_addr = None
        if wallet_addr is None:
            logger.warning(
                "Reconcile: could not derive wallet address; aborting "
                "rather than over-matching against other participants' "
                "orders. Check api_token configuration."
            )
            return

        for query_id, context in self.markets.items():
            # Bucket entries by (outcome, side). Side is determined by
            # the SDK's signed-price convention (negative=bid, positive=ask).
            # We keep these maps separate per (outcome, side) so a BID
            # at price 30 cannot accidentally match an ASK at 30, and
            # a YES-side entry cannot match a NO-side tracked order.
            bids: dict[bool, dict[int, int]] = {True: {}, False: {}}
            asks: dict[bool, dict[int, int]] = {True: {}, False: {}}

            for ob_outcome in (True, False):
                try:
                    entries = self.client.get_order_book(query_id, ob_outcome)
                except Exception as exc:
                    logger.warning(
                        f"Reconcile: get_order_book({query_id}, {ob_outcome}) "
                        f"failed: {exc}. Treating outcome as empty."
                    )
                    continue
                for entry in entries:
                    owner = entry.get("wallet_address")
                    if owner is None:
                        continue
                    if isinstance(owner, (bytes, bytearray)):
                        owner_hex = "0x" + owner.hex()
                    else:
                        owner_hex = str(owner)
                    if owner_hex.lower() != wallet_addr:
                        continue
                    raw_price = entry.get("price")
                    amount = entry.get("amount", 0)
                    if raw_price is None:
                        continue
                    if raw_price < 0:
                        bids[ob_outcome][abs(raw_price)] = (
                            bids[ob_outcome].get(abs(raw_price), 0) + amount
                        )
                    elif raw_price > 0:
                        asks[ob_outcome][raw_price] = (
                            asks[ob_outcome].get(raw_price, 0) + amount
                        )

            # Walk tracked orders for this market and decide active vs
            # stale per side+outcome+is_inventory_backed semantics.
            recovered_bids = 0
            recovered_asks = 0
            stale = 0
            for tracked in self._order_state.get_market_orders(query_id):
                if tracked.is_buy:
                    is_active = tracked.price in bids[tracked.outcome]
                elif tracked.is_inventory_backed:
                    is_active = tracked.price in asks[tracked.outcome]
                else:
                    # Split-mint: either leg surviving means the pair
                    # is still on chain. We cancelled BOTH legs on
                    # success last session, so finding just one means
                    # a partial cancel — caller will retry on the next
                    # cycle's update.
                    is_active = (
                        tracked.price in asks[tracked.outcome]
                        or (100 - tracked.price) in asks[not tracked.outcome]
                    )

                if not is_active:
                    stale += 1
                    self._order_state.untrack_order(
                        tracked.query_id,
                        tracked.outcome,
                        tracked.is_buy,
                        tracked.price,
                    )
                    continue

                # Recover into MarketContext so the main loop manages it.
                if tracked.is_buy:
                    order = ActiveOrder(
                        query_id=query_id,
                        outcome=tracked.outcome,
                        side="bid",
                        price=-tracked.price,
                        amount=tracked.amount,
                        tracked_outcome=tracked.outcome,
                        is_inventory_backed=False,
                    )
                    recovered_bids += 1
                else:
                    order = ActiveOrder(
                        query_id=query_id,
                        outcome=tracked.outcome,
                        side="ask",
                        price=tracked.price,
                        amount=tracked.amount,
                        tracked_outcome=tracked.outcome,
                        is_inventory_backed=tracked.is_inventory_backed,
                    )
                    recovered_asks += 1
                    # Seed the reservation for inventory-backed asks.
                    # The held count was just refreshed from chain truth
                    # via `_refresh_inventory()`; without seeding the
                    # reservation, `available_for_sell` would over-count
                    # by `tracked.amount` and the next cycle would
                    # double-list against the same shares.
                    if tracked.is_inventory_backed:
                        inv = self._inventory.get_market_inventory(query_id)
                        inv.reserve_pair(tracked.outcome, tracked.amount)
                context.current_orders.append(order)

            if recovered_bids or recovered_asks or stale:
                logger.info(
                    f"Reconcile market {query_id}: "
                    f"{recovered_bids} bids + {recovered_asks} asks recovered, "
                    f"{stale} stale dropped"
                )

    def _pre_mint_all_markets(self) -> None:
        """One-time split-mint pass to bring each market up to its
        configured `initial_mint_pairs` target.

        Pre-mint is **opt-in**. It does nothing unless ALL of:
          - `config.pre_mint_max_total_collateral_usd` is set (the
            wallet circuit breaker); AND
          - at least one registered market has `initial_mint_pairs`
            on its `StreamConfig`; AND
          - the bot is not in read-only / sample-data mode.

        For each opt-in market the deficit is `target - paired_inventory()`,
        clamped at zero. Markets within `pre_settlement_cutoff +
        pre_mint_settlement_buffer` of settling are skipped so collateral
        isn't burned on a market that's about to be liquidated.

        The SDK's `place_split_limit_order(true_price=X)` mints `amount`
        YES+NO pairs and auto-lists the NO leg at `100 - X`. Setting
        `pre_mint_listing_price_yes_cents=1` (the default) parks the
        auto-listed NO at 99c. The bot then cancels the auto-listed
        leg in the next refresh cycle, leaving both YES and NO as
        held inventory; `available_for_sell` reports them and the
        inventory-aware ASK path can back asks against them without
        a fresh mint.

        Honors SIGTERM (`self.running`) between markets so a shutdown
        request during a long pre-mint pass exits cleanly.
        """
        cap = self.config.pre_mint_max_total_collateral_usd
        if cap is None:
            logger.info(
                "Pre-mint disabled: pre_mint_max_total_collateral_usd "
                "is not set. Leave it unset to keep lazy-mint behavior."
            )
            return
        if self.read_only or self.config.use_sample_data:
            logger.info(
                "Pre-mint skipped: %s",
                "read-only mode" if self.read_only else "sample-data mode",
            )
            return

        cutoff_buffer = (
            self.config.pre_settlement_cutoff
            + self.config.pre_mint_settlement_buffer
        )
        now_ts = int(time.time())

        deficits: dict[int, int] = {}
        total_deficit_pairs = 0
        for query_id, context in self.markets.items():
            target = context.stream_config.initial_mint_pairs
            if not target or target <= 0:
                continue
            settle_time = context.settle_time
            if settle_time is not None and (settle_time - now_ts) <= cutoff_buffer:
                logger.info(
                    f"Pre-mint skip market {query_id}: settle_time={settle_time} "
                    f"within {cutoff_buffer}s of cutoff"
                )
                continue
            inv = self._inventory.get_market_inventory(query_id)
            paired = inv.paired_inventory()
            deficit = max(0, int(target) - int(paired))
            if deficit > 0:
                deficits[query_id] = deficit
                total_deficit_pairs += deficit

        if not deficits:
            logger.info(
                f"Pre-mint: no deficit across {len(self.markets)} markets"
            )
            return

        if total_deficit_pairs > cap:
            logger.error(
                f"Pre-mint aborted: total deficit {total_deficit_pairs} "
                f"pairs (${total_deficit_pairs}) exceeds "
                f"pre_mint_max_total_collateral_usd={cap:.2f}. Lower the "
                f"per-market initial_mint_pairs or raise the cap if "
                f"intentional."
            )
            raise RuntimeError("pre_mint_max_total_collateral_usd exceeded")

        park_price = int(self.config.pre_mint_listing_price_yes_cents)
        logger.info(
            f"Pre-mint pre-flight: gateway={self.config.node_url}, "
            f"deficit={total_deficit_pairs} pairs (${total_deficit_pairs} "
            f"collateral) across {len(deficits)} markets, park "
            f"true_price={park_price} (auto-lists NO at {100 - park_price}c)"
        )

        broadcast_count = 0
        for query_id, deficit in deficits.items():
            if not self.running:
                logger.info(
                    f"Pre-mint interrupted by shutdown request after "
                    f"broadcasting {broadcast_count}/{len(deficits)} markets"
                )
                return
            try:
                self.client.place_split_limit_order(
                    query_id=query_id,
                    true_price=park_price,
                    amount=deficit,
                    wait=True,
                )
                broadcast_count += 1
                logger.info(
                    f"Pre-mint market {query_id}: minted {deficit} pairs "
                    f"(NO auto-listed at {100 - park_price}c)"
                )
            except Exception as e:
                logger.error(
                    f"Pre-mint failed for market {query_id} "
                    f"(deficit={deficit} pairs): {e}. Bot will fall back "
                    f"to per-cycle split-mint for this market."
                )

        logger.info(
            f"Pre-mint complete: {broadcast_count}/{len(deficits)} markets minted, "
            f"~${total_deficit_pairs} collateral committed. Inventory will be "
            f"refreshed on the next cycle's `_refresh_inventory` tick."
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
        elif stream_config.initial_probability is not None:
            # No order book yet. Use the per-market prior so we don't
            # quote a 1¢-prior market (Hormuz outcome 5 etc.) around 50¢.
            mid = stream_config.initial_probability * 100.0
            logger.info(
                f"Market {market_state.query_id}: no order book, using "
                f"initial_probability prior {mid:.2f}¢ as mid"
            )
        else:
            # Symmetric cold-start. Only safe for markets whose true fair
            # value is genuinely near 50¢. For directional markets, set
            # `initial_probability` on the StreamConfig.
            mid = 50.0
            logger.warning(
                f"Market {market_state.query_id}: no order book and no "
                f"initial_probability set; falling back to mid=50.0. This "
                f"is unsafe for directional markets (e.g. low-prior outcomes)."
            )

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

    def cancel_existing_orders(
        self, context: MarketContext, outcome: Optional[bool] = None,
        wait: bool = True,
    ) -> None:
        """
        Cancel existing orders for a market.

        Args:
            context: Market context with current orders
            outcome: If specified, only cancel orders where `tracked_outcome` matches.
                     If None, cancel all orders.
            wait: If True, block until each cancel confirms on chain. Set to
                  False on shutdown so a slow RPC can't hang stop() forever.
        """
        if outcome is None:
            orders_to_cancel = list(context.current_orders)
        else:
            orders_to_cancel = [
                o for o in context.current_orders
                if getattr(o, "tracked_outcome", o.outcome) == outcome
            ]

        if self.read_only:
            logger.debug(f"[READ-ONLY] Would cancel {len(orders_to_cancel)} orders")
            for o in orders_to_cancel:
                context.current_orders.remove(o)
            return

        for order in orders_to_cancel:
            # Track whether we can safely drop this from local state. A real
            # RPC error must keep the order tracked so we don't lose chain
            # visibility (the next reconcile/restart can sync state). "Order
            # not found" is treated as success because chain has already lost
            # it (settled, externally cancelled, etc.).
            cancel_succeeded = True
            if order.side == "ask" and order.is_inventory_backed:
                # Inventory-backed ASK is a single sell on `outcome` at
                # `price`. There is NO mirror leg on the opposite book —
                # cancelling at (not outcome, 100-price) would either hit
                # nothing (harmless waste) or accidentally cancel an
                # unrelated bot order at that exact price (a same-price
                # collision risk that's plausible on tight-bound markets).
                try:
                    self.client.cancel_order(
                        query_id=order.query_id,
                        outcome=order.outcome,
                        price=order.price,
                        wait=wait,
                    )
                except Exception as e:
                    err_str = str(e).lower()
                    if "order not found" in err_str or "old order not found" in err_str:
                        logger.debug(f"Order already gone for market {order.query_id}")
                    else:
                        logger.warning(f"Failed to cancel inventory-backed ask: {e}")
                        cancel_succeeded = False
            elif order.side == "ask":
                # Split-mint ASK: the on-chain pair is at (outcome, price)
                # AND (not outcome, 100 - price). Cancel both legs.
                for cancel_outcome, cancel_price in [
                    (order.outcome, order.price),
                    (not order.outcome, 100 - order.price),
                ]:
                    try:
                        self.client.cancel_order(
                            query_id=order.query_id,
                            outcome=cancel_outcome,
                            price=cancel_price,
                            wait=wait,
                        )
                    except Exception as e:
                        err_str = str(e).lower()
                        if "order not found" in err_str or "old order not found" in err_str:
                            logger.debug(f"Order already gone for market {order.query_id}")
                        else:
                            logger.warning(f"Failed to cancel split-mint ask leg: {e}")
                            cancel_succeeded = False
            else:
                # Bid: single cancel at (outcome, price).
                try:
                    self.client.cancel_order(
                        query_id=order.query_id,
                        outcome=order.outcome,
                        price=order.price,
                        wait=wait,
                    )
                except Exception as e:
                    err_str = str(e).lower()
                    if "order not found" in err_str or "old order not found" in err_str:
                        logger.debug(f"Order already gone for market {order.query_id}")
                    else:
                        logger.warning(f"Failed to cancel bid order: {e}")
                        cancel_succeeded = False

            if cancel_succeeded:
                context.current_orders.remove(order)
                # Mirror in persistent state and inventory accounting.
                if self._order_state is not None:
                    self._order_state.untrack_order(
                        query_id=order.query_id,
                        outcome=order.outcome,
                        is_buy=(order.side == "bid"),
                        price=abs(order.price),
                    )
                if order.side == "ask" and order.is_inventory_backed:
                    # An inventory-backed ASK reserved `amount` shares
                    # against held inventory; release them back to
                    # available now that the cancel landed. Skip the
                    # release for split-mint ASKs (they reserved
                    # nothing) so a stray double-release on shutdown
                    # can't corrupt accounting.
                    inv = self._inventory.get_market_inventory(order.query_id)
                    inv.release_pair(order.outcome, order.amount)
            else:
                logger.warning(
                    f"Keeping order in local state after cancel failure: "
                    f"market={order.query_id} side={order.side} "
                    f"outcome={order.outcome} price={order.price} "
                    f"inv_backed={order.is_inventory_backed}"
                )

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

        # Per-leg sizing. When `order_dollar_amount` is set, the BID at
        # 1c and the ASK at 99c can be wildly different in shares but
        # comparable in dollars; otherwise fall back to fixed shares.
        bid_amount = max(
            _compute_base_amount(
                bid_price_int, self.config.order_dollar_amount,
                self.config.default_order_amount,
            ),
            stream_config.min_order_size,
        )
        ask_amount = max(
            _compute_base_amount(
                ask_price_int, self.config.order_dollar_amount,
                self.config.default_order_amount,
            ),
            stream_config.min_order_size,
        )

        # Build recommendations
        recommendations = [
            OrderRecommendation(
                query_id=context.query_id,
                stream_name=stream_config.name,
                outcome=outcome,
                side="bid",
                price=bid_price_int,
                amount=bid_amount,
            ),
            OrderRecommendation(
                query_id=context.query_id,
                stream_name=stream_config.name,
                outcome=outcome,
                side="ask",
                price=ask_price_int,
                amount=ask_amount,
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
                f"BID {bid_price_int}c x{bid_amount} / ASK {ask_price_int}c x{ask_amount}"
            )
            return []

        # Place bid order on the specified outcome
        if not _meets_min_notional(bid_price_int, bid_amount):
            logger.info(
                f"Market {context.query_id}: skip {'YES' if outcome else 'NO'} bid "
                f"@{bid_price_int}c x{bid_amount} (notional {bid_price_int * bid_amount} < "
                f"min {MIN_ORDER_NOTIONAL_CENT_SHARES}). Order would silently revert."
            )
        else:
            try:
                tx_hash = self.client.place_buy_order(
                    query_id=context.query_id,
                    outcome=outcome,
                    price=bid_price_int,
                    amount=bid_amount,
                    wait=True,
                )
                bid_order = ActiveOrder(
                    query_id=context.query_id,
                    outcome=outcome,
                    side="bid",
                    price=-bid_price_int,  # SDK format
                    amount=bid_amount,
                    tracked_outcome=outcome,
                    created_at=time.time(),
                )
                placed_orders.append(bid_order)
                if self._order_state is not None:
                    self._order_state.track_order(
                        query_id=context.query_id,
                        outcome=outcome,
                        is_buy=True,
                        price=bid_price_int,
                        amount=bid_amount,
                        is_inventory_backed=False,
                    )
                logger.info(
                    f"Placed {'YES' if outcome else 'NO'} bid at {bid_price_int} "
                    f"for {bid_amount} shares on market {context.query_id} (tx: {tx_hash[:16]}...)"
                )
            except Exception as e:
                logger.error(f"Failed to place bid order: {e}")

        # Place ask. Prefer inventory-backed (single-leg `place_sell_order`
        # against held shares) when enough inventory is available; fall
        # back to the legacy split-mint pattern when not. Both paths track
        # the LOGICAL ask `(outcome, ask_price_int)` so should_update_orders
        # compares against the right price next cycle and the cancel path
        # iterates both legs correctly.
        inv = self._inventory.get_market_inventory(context.query_id)
        available = inv.available_for_sell(outcome)

        if available >= ask_amount and _meets_min_notional(ask_price_int, ask_amount):
            # Inventory-backed path. Single-leg sell on the logical outcome
            # at the logical price. No fresh pair mint, no NO-side leg.
            try:
                tx_hash = self.client.place_sell_order(
                    query_id=context.query_id,
                    outcome=outcome,
                    price=ask_price_int,
                    amount=ask_amount,
                    wait=True,
                )
            except Exception as e:
                logger.error(
                    f"Inventory-backed sell failed (qid={context.query_id} "
                    f"outcome={'YES' if outcome else 'NO'} {ask_price_int}c "
                    f"x{ask_amount}): {e}"
                )
            else:
                inv.reserve_pair(outcome, ask_amount)
                ask_order = ActiveOrder(
                    query_id=context.query_id,
                    outcome=outcome,
                    side="ask",
                    price=ask_price_int,
                    amount=ask_amount,
                    tracked_outcome=outcome,
                    is_inventory_backed=True,
                    created_at=time.time(),
                )
                placed_orders.append(ask_order)
                if self._order_state is not None:
                    self._order_state.track_order(
                        query_id=context.query_id,
                        outcome=outcome,
                        is_buy=False,
                        price=ask_price_int,
                        amount=ask_amount,
                        is_inventory_backed=True,
                    )
                logger.info(
                    f"Inventory-backed {'YES' if outcome else 'NO'} ask "
                    f"@{ask_price_int}c x{ask_amount} on market {context.query_id} "
                    f"(avail before/after: {available}/{available - ask_amount}, "
                    f"tx: {tx_hash[:16]}...)"
                )
        else:
            # Split-mint fallback. Mints fresh pair from collateral and
            # lists both legs. Same shape as the pre-Phase-2 bot.
            split_price = ask_price_int if outcome else (100 - ask_price_int)
            ask_legs_ok = (
                _meets_min_notional(split_price, ask_amount)
                and _meets_min_notional(100 - split_price, ask_amount)
            )
            if not ask_legs_ok:
                logger.info(
                    f"Market {context.query_id}: skip {'YES' if outcome else 'NO'} ask "
                    f"@{ask_price_int}c x{ask_amount} (split_price={split_price}, one leg "
                    f"falls below min notional {MIN_ORDER_NOTIONAL_CENT_SHARES}). "
                    f"Order would silently revert."
                )
            else:
                try:
                    self.client.place_split_limit_order(
                        query_id=context.query_id,
                        true_price=split_price,
                        amount=ask_amount,
                        wait=True,
                    )
                    tx_hash = self.client.place_sell_order(
                        query_id=context.query_id,
                        outcome=True,
                        price=split_price,
                        amount=ask_amount,
                        wait=True,
                    )
                    ask_order = ActiveOrder(
                        query_id=context.query_id,
                        outcome=outcome,
                        side="ask",
                        price=ask_price_int,  # logical ASK price
                        amount=ask_amount,
                        tracked_outcome=outcome,
                        created_at=time.time(),
                    )
                    placed_orders.append(ask_order)
                    if self._order_state is not None:
                        self._order_state.track_order(
                            query_id=context.query_id,
                            outcome=outcome,
                            is_buy=False,
                            price=ask_price_int,
                            amount=ask_amount,
                            is_inventory_backed=False,
                        )
                    logger.info(
                        f"Placed {'YES' if outcome else 'NO'} ask at {ask_price_int} "
                        f"for {ask_amount} shares on market {context.query_id} "
                        f"(split-mint, tx: {tx_hash[:16]}...)"
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
        Determine if orders should be updated based on price change OR age.

        Returns True iff at least one tracked order has either:
          - moved enough that the new bid/ask differs from the recorded
            price by >= `threshold` cents, OR
          - aged past `config.max_order_age` seconds (when set).

        The age branch catches stuck-quiet regressions where prices
        stop refreshing and orders stay glued to a stale book. Without
        it, a bot that stops getting fills and observes no price moves
        would never refresh.
        """
        if not current_orders:
            return True

        new_bid, new_ask = new_pricing.to_int_prices()
        max_age = self.config.max_order_age
        now = time.time() if max_age is not None else 0.0

        for order in current_orders:
            current_price = abs(order.price)
            if order.side == "bid" and abs(current_price - new_bid) >= threshold:
                return True
            if order.side == "ask" and abs(current_price - new_ask) >= threshold:
                return True
            if max_age is not None and order.created_at > 0:
                age = now - order.created_at
                if age >= max_age:
                    logger.info(
                        f"Forcing refresh: {order.side} on market "
                        f"{order.query_id} outcome={order.outcome} "
                        f"price={current_price} aged {age:.0f}s "
                        f"(>= max_order_age={max_age:.0f}s)"
                    )
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
            # Fetch current market state for this outcome
            market_state = self.get_market_state(query_id, outcome)

            # Calculate target prices
            pricing = self.calculate_target_prices(
                market_state, context.stream_config
            )

            # Filter orders to this outcome only for update check
            outcome_orders = [
                o for o in context.current_orders
                if getattr(o, "tracked_outcome", o.outcome) == outcome
            ]

            # Check if update is needed
            if not self.should_update_orders(outcome_orders, pricing):
                logger.debug(f"No update needed for market {query_id} outcome={outcome}")
                return

            # Cancel existing orders for this outcome only
            self.cancel_existing_orders(context, outcome=outcome)

            # Re-check shutdown after cancel. cancel_existing_orders runs
            # several blocking `wait=True` RPCs; a SIGTERM that arrives
            # during cancel should NOT lead to placing fresh orders that
            # the supervisor is about to SIGKILL anyway.
            if not self.running:
                logger.info(
                    f"Shutdown requested during cancel; skipping place "
                    f"for market {query_id} outcome={outcome}"
                )
                return

            # Place new orders for this outcome
            new_orders = self.place_orders(context, pricing, market_state, outcome)
            context.current_orders.extend(new_orders)

            logger.info(
                f"Updated market {query_id} {'YES' if outcome else 'NO'}: "
                f"bid={pricing.bid_price:.2f}, ask={pricing.ask_price:.2f}, "
                f"mid={pricing.mid_price:.2f}"
            )

        except Exception as e:
            logger.error(f"Error updating market {query_id}: {e}")

    def update_all_markets(self) -> None:
        """Update orders for all registered markets.

        Honors a cooperative shutdown request: between each market we
        check `self.running` so a SIGTERM received mid-cycle exits within
        one in-progress market's RPC duration (worst case ~10-30s on a
        slow gateway with several `wait=True` calls per market) rather
        than running the full pass to completion.
        """
        for query_id, context in self.markets.items():
            if not self.running:
                logger.info(
                    f"Shutdown requested mid-cycle; aborting before "
                    f"market {query_id}"
                )
                return
            mode = context.stream_config.outcome_mode
            outcomes = []
            if mode in ("yes", "both"):
                outcomes.append(True)
            if mode in ("no", "both"):
                outcomes.append(False)
            for outcome in outcomes:
                self.update_market(query_id, outcome=outcome)

    def run_once(self) -> None:
        """Run a single update cycle for all markets."""
        logger.debug("Running update cycle")
        # Periodic inventory refresh from chain truth. Cheap enough to
        # run on a coarse cadence; the bot tolerates a few cycles of
        # stale inventory (overcounted reserved sells produce an empty
        # available_for_sell, which just falls through to split-mint).
        if (
            self._cycles_since_inventory_refresh
            >= max(1, self.config.inventory_refresh_interval_cycles)
        ):
            self._refresh_inventory()
            self._cycles_since_inventory_refresh = 0
        else:
            self._cycles_since_inventory_refresh += 1

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

        # Bootstrap from chain truth before the first update cycle:
        # 1) Pull positions to seed per-market inventory so the first
        #    cycle's ASKs can prefer inventory-backed over fresh mints.
        # 2) Reconcile persisted order state against chain so orders
        #    placed in the previous session are re-managed and orphans
        #    are dropped.
        # Skipped entirely in read-only mode (no chain calls beyond
        # what `update_all_markets` already does for snapshots).
        if not self.read_only:
            self._refresh_inventory()
            self._reconcile_orders_on_startup()
            # Pre-mint after reconcile so deficits are computed against
            # recovered held + listed inventory, not zero. Pre-mint is
            # opt-in: short-circuits when no markets configure
            # `initial_mint_pairs` or the wallet cap is unset.
            self._pre_mint_all_markets()

        while self.running:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in update cycle: {e}")

            time.sleep(self.config.check_interval_seconds)

    def stop(self) -> None:
        """Stop the bot and cancel all active orders.

        Uses non-blocking cancels (wait=False). Blocking shutdowns are how
        bot processes get SIGKILLed mid-cleanup by their supervisor; the
        sister MM bot wedged the orchestrator main thread for 32h on
        2026-05-01 by blowing past its SIGTERM timeout in this exact path.
        """
        self.running = False
        logger.info("Stopping LP Bot...")

        for context in self.markets.values():
            self.cancel_existing_orders(context, wait=False)

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
