"""Configuration for the LP Bot."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PricingMethod(Enum):
    """Pricing method for bid/ask calculation."""
    EQUAL_WEIGHTED = "equal"
    VOLUME_WEIGHTED = "volume"


@dataclass
class StreamConfig:
    """Configuration for a single stream/market."""
    stream_id: str
    name: str
    bounds_pct: float = 0.15  # Bounds as percentage from mid (e.g., 0.10 = ±10%)
    min_order_size: int = 100  # Minimum order size in shares
    enabled: bool = True
    outcome_mode: str = "yes"  # "yes", "no", or "both"
    # Optional per-market prior fair-YES probability in [0, 1]. When set and
    # the order book is empty (no best_bid AND no best_ask), the bot uses
    # `prior * 100` as the mid price instead of falling back to 50¢. Without
    # this, a 1¢-prior market (e.g. Hormuz outcome 5) would quote YES bids
    # in the [45, 55] range and hemorrhage capital to anyone willing to take
    # them. Leave as None for symmetric markets where 50¢ is a sensible
    # cold-start mid.
    initial_probability: Optional[float] = None

    # Pre-mint inventory target (number of YES+NO pairs to hold at startup).
    # When set AND the bot's `pre_mint_max_total_collateral_usd` cap is also
    # set, the bot will split-mint up to (target - paired_inventory()) fresh
    # pairs per market at startup so subsequent ASKs can be inventory-backed
    # instead of fresh-minted every cycle. Each pair costs $1 of collateral.
    # Leave as None to keep the bot in lazy-mint mode for this market.
    # Mirrors the MM bot's `initial_mint_pairs` field and the public market-
    # maker-bot#6 opt-in documentation.
    initial_mint_pairs: Optional[int] = None

    def __post_init__(self):
        if not 0 < self.bounds_pct < 1:
            raise ValueError(
                f"Invalid bounds_pct: {self.bounds_pct}. "
                "Must be between 0 and 1 (e.g., 0.10 for ±10%)"
            )
        if self.outcome_mode not in ("yes", "no", "both"):
            raise ValueError(
                f"Invalid outcome_mode: {self.outcome_mode}. "
                "Must be 'yes', 'no', or 'both'"
            )
        if self.initial_probability is not None and not (0.0 <= self.initial_probability <= 1.0):
            raise ValueError(
                f"Invalid initial_probability: {self.initial_probability}. "
                "Must be in [0.0, 1.0] when set."
            )

    def calculate_bounds(self, mid_price: float) -> tuple[float, float]:
        """
        Calculate lower and upper bounds from mid price.

        Args:
            mid_price: Current mid price

        Returns:
            Tuple of (lower_bound, upper_bound)
        """
        spread = mid_price * self.bounds_pct
        lower_bound = max(1, mid_price - spread)
        upper_bound = min(99, mid_price + spread)
        return lower_bound, upper_bound


@dataclass
class Config:
    """Main configuration for the LP Bot."""
    # TRUF Network connection
    node_url: str = "https://gateway.testnet.truf.network"
    api_token: str = ""  # Optional - only needed for placing orders, not for reading data

    # Use sample data instead of real network data (for testing)
    use_sample_data: bool = False

    # Pricing parameters
    pricing_method: PricingMethod = PricingMethod.EQUAL_WEIGHTED
    alpha: float = 0.30  # Risk tolerance: 0=aggressive (near best), 1=passive (near bounds)

    # Volume-weighted specific parameters
    target_depth_pct: float = 0.30  # Depth % for VWAP calculation
    apply_time_decay: bool = False
    half_life_seconds: float = 60.0

    # Order parameters
    default_order_amount: int = 100  # Default shares per order

    # Optional: size each leg by a target DOLLAR notional rather than a
    # fixed share count. When set, the bot computes per-leg shares as
    # `ceil(order_dollar_amount * 100 / price_cents)` so a $2 BID at 1c
    # and a $2 ASK at 99c both end up around $2 notional rather than
    # 100x apart in dollars. Leave as None to keep the fixed-share
    # behavior (`default_order_amount`). Mirrors the MM bot's
    # `order_dollar_amount`.
    #
    # IMPORTANT: `StreamConfig.min_order_size` (default 100) acts as a
    # hard floor AFTER `_compute_base_amount` runs. With the defaults
    # (`min_order_size=100`, `order_dollar_amount=$2`), an ASK at 99c
    # rounds to 3 shares ($2.97 notional) then floors to 100 shares
    # ($99 notional) — 33x more than the operator asked for. When
    # using dollar-sizing on directional markets where extreme prices
    # are routine (e.g. Hormuz outcome 5 at 1c-prior), operators MUST
    # also lower `min_order_size` (e.g. to 1) per-market or the
    # dollar-sizing will be silently overridden by the floor.
    order_dollar_amount: Optional[float] = None

    # Optional: maximum age (in seconds) any tracked order is allowed
    # to live before the bot forces a refresh even if the price has
    # not moved past the `should_update_orders` threshold. Catches
    # stuck-quiet regressions where prices stop refreshing and orders
    # stay glued to a stale book. None disables age-based forcing
    # (default — preserves the pre-existing behavior).
    max_order_age: Optional[float] = None

    # Scheduler parameters
    block_interval_seconds: float = 2.0  # Approximate block time
    check_interval_seconds: float = 5.0  # How often to check for new blocks

    # Pre-settlement cutoff: pull all liquidity this many seconds before settle_time
    pre_settlement_cutoff: float = 900.0  # Default 15 minutes

    # Persistent order state file. Lets the bot recover its tracked
    # orders across restarts and reconcile against chain truth on
    # startup. Skipped in read-only mode.
    order_state_file: str = "lp_bot_order_state.json"

    # Inventory refresh cadence. The bot calls `get_user_positions` and
    # rebuilds per-market inventory every N update cycles. Lower values
    # detect fills faster but cost more RPC bandwidth; higher values let
    # the inventory cache go stale between refreshes. Mirrors the MM
    # bot's 30s inventory refresh interval at a similar default cadence.
    inventory_refresh_interval_cycles: int = 6

    # === Pre-mint (OPTIONAL, disabled by default) ===
    # Hard wallet cap on the summed pre-mint collateral across all
    # markets. REQUIRED to enable pre-mint (set to None to keep
    # pre-mint disabled even if individual markets have
    # `initial_mint_pairs` set). Sized in dollars; each pair = $1
    # collateral. Pre-mint aborts startup if the summed deficit would
    # exceed this. None disables pre-mint entirely.
    pre_mint_max_total_collateral_usd: Optional[float] = None

    # `true_price` passed to place_split_limit_order during pre-mint.
    # The SDK auto-lists the NO leg at `100 - true_price`, so
    # `true_price=1` parks the auto-listed NO at 99c — significantly
    # less likely to fill than the bot's intended price. Not an
    # absolute guarantee (a counterparty willing to pay 99c can still
    # take it) but reduces leak risk if the follow-up cancel fails.
    pre_mint_listing_price_yes_cents: int = 1

    # Pre-mint mint-cushion multiplier. Applied when sizing per-market
    # `initial_mint_pairs` from your planned book. 1.0 = mint exactly
    # what you plan to ASK; >1.0 = oversize for refill headroom.
    # Currently informational only (caller sets `initial_mint_pairs`
    # directly); reserved for future config_generator integration.
    mint_cushion_multiplier: float = 1.0

    # Safety buffer added to `pre_settlement_cutoff` when deciding
    # whether to skip pre-mint for a market near settlement. Mints
    # within `pre_settlement_cutoff + pre_mint_settlement_buffer`
    # seconds of settling are skipped so the bot doesn't burn pair
    # collateral on a market it's about to pull liquidity from.
    pre_mint_settlement_buffer: float = 300.0

    # Pricing source for mid-price determination
    # "black_scholes" = always use B-S fair value from underlying stream data
    #                    (recommended when there are few market participants)
    # "order_book" = use order book mid price when available, fallback to mid=50
    pricing_source: str = "black_scholes"

    # Configured streams with rewards-eligible bounds
    streams: list[StreamConfig] = field(default_factory=list)

    def __post_init__(self):
        if not 0 <= self.alpha <= 1:
            raise ValueError(f"alpha must be in [0, 1], got {self.alpha}")

        # Pre-mint park price must be in (0, 100). 0 and 100 produce
        # auto-listed legs at 100 and 0 respectively, both nonsensical.
        if not (1 <= self.pre_mint_listing_price_yes_cents <= 99):
            raise ValueError(
                f"pre_mint_listing_price_yes_cents must be in [1, 99], "
                f"got {self.pre_mint_listing_price_yes_cents}"
            )

        if self.order_dollar_amount is not None and self.order_dollar_amount <= 0:
            raise ValueError(
                f"order_dollar_amount must be > 0 when set, "
                f"got {self.order_dollar_amount}"
            )

        if self.max_order_age is not None and self.max_order_age <= 0:
            raise ValueError(
                f"max_order_age must be > 0 when set, got {self.max_order_age}"
            )

        # The pricing_source flag is documented as choosing between
        # B-S fair value and order-book mid, but no code consumes it
        # today (only pricing_method is wired through). Reject any
        # non-default value so users don't believe a setting they
        # change is taking effect.
        if self.pricing_source != "black_scholes":
            raise ValueError(
                f"pricing_source={self.pricing_source!r} is not implemented "
                f"yet. Only 'black_scholes' is supported. Tracked as a "
                f"deferred refactor; remove this check once pricing.py "
                f"actually branches on the flag."
            )

        # Initialize default streams if empty
        if not self.streams:
            self.streams = get_default_streams()


def get_default_streams() -> list[StreamConfig]:
    """
    Get default stream configurations with bounds as percentage from mid.

    Testnet streams (matching market creation bot config):
    - st9058219c3c3247faf2b0a738de7027 - Testnet BTC-like Price
    - st5cda3b42dc3db0e49af57d7bf14905 - Testnet Mid-Cap Price
    - st361547d8b439502d3828d74ca679b5 - Testnet Low-Cap Price
    - st26e6f725c82630d2c5bd542883453f - Testnet Rate A
    - stf826b74de25bcae10dcde294c25e87 - Testnet Rate B
    - stde38e5fd701194ef8da203c8fb012b - Testnet Mid-Range Price

    bounds_pct: Percentage from mid price for rewards-eligible bounds.
    E.g., 0.10 means bounds are at mid ± 10%
    If mid=50 and bounds_pct=0.10, bounds are [45, 55]
    """
    return [
        StreamConfig(
            stream_id="st9058219c3c3247faf2b0a738de7027",
            name="Testnet BTC-like Price",
            bounds_pct=0.10,  # ±10% from mid
        ),
        StreamConfig(
            stream_id="st5cda3b42dc3db0e49af57d7bf14905",
            name="Testnet Mid-Cap Price",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="st361547d8b439502d3828d74ca679b5",
            name="Testnet Low-Cap Price",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="st26e6f725c82630d2c5bd542883453f",
            name="Testnet Rate A",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="stf826b74de25bcae10dcde294c25e87",
            name="Testnet Rate B",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="stde38e5fd701194ef8da203c8fb012b",
            name="Testnet Mid-Range Price",
            bounds_pct=0.15,  # ±15% from mid (more volatile)
        ),
    ]


def load_config_from_env() -> Config:
    """Load configuration from environment variables."""
    import os

    config = Config(
        node_url=os.getenv("TRUF_NODE_URL", "https://gateway.testnet.truf.network"),
        api_token=os.getenv("TRUF_API_TOKEN", ""),
        alpha=float(os.getenv("LP_BOT_ALPHA", "0.30")),
        default_order_amount=int(os.getenv("LP_BOT_ORDER_AMOUNT", "100")),
    )

    config.pricing_source = os.getenv("LP_BOT_PRICING_SOURCE", "black_scholes")

    method = os.getenv("LP_BOT_PRICING_METHOD", "equal").lower()
    if method == "volume":
        config.pricing_method = PricingMethod.VOLUME_WEIGHTED
        config.apply_time_decay = os.getenv("LP_BOT_TIME_DECAY", "false").lower() == "true"
        config.half_life_seconds = float(os.getenv("LP_BOT_HALF_LIFE", "60.0"))
        config.target_depth_pct = float(os.getenv("LP_BOT_TARGET_DEPTH", "0.30"))

    return config
