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

    # Minimum interval (seconds) between consecutive "missing-side"
    # refreshes for the same (market, outcome). After a place failure
    # leaves only one side tracked, `should_update_orders` triggers a
    # cancel+replace burst to recover the missing leg; this cooldown
    # caps the burst rate so a chronically failing leg (gateway flap,
    # `_meets_min_notional` skip, persistent insufficient balance)
    # does not churn at every `check_interval_seconds`. 0.0 disables
    # the cooldown (refresh every cycle when a side is missing).
    missing_side_retry_cooldown: float = 60.0

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

    # Periodic on-chain orphan reconcile cadence, measured in main-loop
    # cycles. 1 cycle = 1 full sweep through all configured markets
    # (`run_once`), which on 35 markets at the default 5s `check_interval_seconds`
    # plus ~5s per market work takes roughly 5-7 minutes. So
    # reconcile_interval_cycles=1 means "reconcile once per sweep".
    # When > 0, the reconcile detects both directions of drift between
    # local tracked state and the on-chain order book:
    #   - tracked-but-not-on-chain  -> untrack locally (same as the
    #     existing startup reconcile)
    #   - on-chain-but-not-tracked  -> CANCEL the orphan
    # The orphan-cancel direction is the fix for the known cancel-then-
    # place silent-failure race in the update path. 0 disables (default,
    # opt-in for safety so existing deployments do not change behavior).
    # Recommended starting value: 1 (every sweep) for sustained deploy.
    reconcile_interval_cycles: int = 0

    # Minimum bid/ask spread floor in cents, enforced after pricing
    # logic. Default 0.0 keeps the legacy behavior (only the 1c
    # uncrossed-spread floor inside `ensure_uncrossed_spread`).
    # Operators on markets where tight bounds can collapse the spread
    # (low-mid markets where `bounds_pct * mid` is narrow) should set
    # this to a more conservative value such as 3-6c. When the
    # calculated spread is below the floor, the bot widens it
    # symmetrically around the mid before placing. <= 0 disables.
    min_spread_cents: float = 0.0

    # Optional path to a heartbeat file the bot rewrites with the
    # current unix timestamp on every main-loop iteration. A
    # supervisor process (systemd, the MM orchestrator, etc.) can
    # watch the file's mtime to detect a stuck-quiet bot. None
    # disables heartbeat writes entirely (default — preserves the
    # pre-Phase-3 behavior). The file's parent directory must exist
    # and be writable; the bot logs a warning on the first write
    # failure and stops retrying so a misconfigured path doesn't
    # spam the log.
    heartbeat_file: Optional[str] = None

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

        if self.missing_side_retry_cooldown < 0:
            raise ValueError(
                f"missing_side_retry_cooldown must be >= 0, "
                f"got {self.missing_side_retry_cooldown}"
            )

        if self.min_spread_cents < 0:
            raise ValueError(
                f"min_spread_cents must be >= 0, got {self.min_spread_cents}"
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


@dataclass
class MarketEntry:
    """A (query_id, StreamConfig) pair as loaded from a YAML deploy file.

    The bot's `register_market(query_id, stream_config)` API takes the two
    separately; bundling them here lets a YAML-driven deploy carry the
    full registration in a single list entry rather than two parallel
    lists. Mirrors the MM bot's `MarketConfig` shape.
    """
    query_id: int
    stream: StreamConfig


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


# Whitelist of top-level YAML keys consumed by `load_config_from_dict`.
# Kept separate from the `Config` dataclass field list because (a) `markets`
# lives at the top level of YAML but is returned alongside Config rather
# than stored on it, and (b) we want explicit failure on typos like
# `node_ul:` rather than silently dropping them. Update this set when
# adding a new YAML-tunable Config field.
_KNOWN_TOP_LEVEL_KEYS = {
    "node_url",
    "api_token",
    "use_sample_data",
    "pricing_method",
    "pricing_source",
    "alpha",
    "target_depth_pct",
    "apply_time_decay",
    "half_life_seconds",
    "default_order_amount",
    "order_dollar_amount",
    "max_order_age",
    "missing_side_retry_cooldown",
    "block_interval_seconds",
    "check_interval_seconds",
    "pre_settlement_cutoff",
    "order_state_file",
    "inventory_refresh_interval_cycles",
    "reconcile_interval_cycles",
    "min_spread_cents",
    "heartbeat_file",
    "pre_mint_max_total_collateral_usd",
    "pre_mint_listing_price_yes_cents",
    "mint_cushion_multiplier",
    "pre_mint_settlement_buffer",
    "markets",
}

# Whitelist of per-market YAML keys consumed by `load_config_from_dict`.
# `query_id` is plucked into `MarketEntry`; the rest are passed to
# StreamConfig. Update this set when adding a new YAML-tunable
# StreamConfig field.
_KNOWN_MARKET_KEYS = {
    "query_id",
    "stream_id",
    "name",
    "bounds_pct",
    "min_order_size",
    "enabled",
    "outcome_mode",
    "initial_probability",
    "initial_mint_pairs",
}


def load_config_from_dict(data: dict) -> tuple[Config, list[MarketEntry]]:
    """Load Config + registered markets from a parsed YAML/JSON dict.

    Returns (config, markets). The caller is responsible for iterating
    `markets` and invoking `bot.register_market(entry.query_id,
    entry.stream)` once the bot is constructed. We keep them separate
    rather than stuffing the entries onto `Config.streams` so the
    distinction between "schema-default fallback streams" (kept on
    Config.streams) and "operator-requested registrations" (returned
    here) stays clear.

    Unknown keys at the top level OR inside a market entry raise
    `ValueError` rather than being silently dropped. A typo like
    `nod_url:` should fail loudly, not run the bot on the wrong URL.
    `api_token`, when blank in the YAML, falls back to
    `TRUF_PRIVATE_KEY` and then `TRUF_API_TOKEN` (MM-bot-style) so
    the deployable YAML can stay free of secrets.
    """
    import os

    unknown_top = set(data.keys()) - _KNOWN_TOP_LEVEL_KEYS
    if unknown_top:
        raise ValueError(
            f"Unknown top-level config key(s): {sorted(unknown_top)}. "
            f"Allowed keys: {sorted(_KNOWN_TOP_LEVEL_KEYS)}"
        )

    # Map pricing_method string -> enum; accept None / missing.
    method_raw = data.get("pricing_method")
    if method_raw is None:
        pricing_method = PricingMethod.EQUAL_WEIGHTED
    else:
        try:
            pricing_method = PricingMethod(method_raw)
        except ValueError as exc:
            raise ValueError(
                f"Invalid pricing_method {method_raw!r}. Must be one of "
                f"{[m.value for m in PricingMethod]}"
            ) from exc

    raw_api_token = data.get("api_token", "")
    if raw_api_token is not None and not isinstance(raw_api_token, str):
        raise ValueError(
            f"api_token must be a string (or null/missing for env fallback), "
            f"got {type(raw_api_token).__name__}: {raw_api_token!r}"
        )
    api_token = raw_api_token or ""
    if not api_token:
        api_token = (
            os.environ.get("TRUF_PRIVATE_KEY")
            or os.environ.get("TRUF_API_TOKEN")
            or ""
        )

    def _typed(key: str, default, caster):
        """Pull `key` from `data`, coerce via `caster`, or return default.

        Wraps the per-field cast so a YAML mistyping (`alpha: "0.3"`
        with quotes, `default_order_amount: 100.0` as float, etc.)
        surfaces as a typed `ValueError` naming the offending key
        rather than a downstream `TypeError` from a comparison or
        an int-vs-float type leak into the bot.
        """
        if key not in data or data[key] is None:
            return default
        value = data[key]
        try:
            return caster(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"config key {key!r}={value!r}: expected "
                f"{caster.__name__}, got {type(value).__name__} ({e})"
            )

    # Build Config from explicit keys. Each numeric/bool/path field is
    # coerced through `_typed` so a YAML mistyping (e.g. `alpha: "0.3"`)
    # raises a typed `ValueError` naming the offending key instead of
    # leaking a `TypeError` from a comparison three frames downstream.
    # `str` fields are pulled with `.get` directly because PyYAML's
    # safe_load yields `str` for unquoted bareword values too.
    config = Config(
        node_url=data.get("node_url", "https://gateway.testnet.truf.network"),
        api_token=api_token,
        use_sample_data=_typed("use_sample_data", False, bool),
        pricing_method=pricing_method,
        alpha=_typed("alpha", 0.30, float),
        target_depth_pct=_typed("target_depth_pct", 0.30, float),
        apply_time_decay=_typed("apply_time_decay", False, bool),
        half_life_seconds=_typed("half_life_seconds", 60.0, float),
        default_order_amount=_typed("default_order_amount", 100, int),
        order_dollar_amount=_typed("order_dollar_amount", None, float),
        max_order_age=_typed("max_order_age", None, float),
        missing_side_retry_cooldown=_typed(
            "missing_side_retry_cooldown", 60.0, float
        ),
        block_interval_seconds=_typed("block_interval_seconds", 2.0, float),
        check_interval_seconds=_typed("check_interval_seconds", 5.0, float),
        pre_settlement_cutoff=_typed("pre_settlement_cutoff", 900.0, float),
        order_state_file=data.get("order_state_file", "lp_bot_order_state.json"),
        inventory_refresh_interval_cycles=_typed(
            "inventory_refresh_interval_cycles", 6, int
        ),
        reconcile_interval_cycles=_typed("reconcile_interval_cycles", 0, int),
        min_spread_cents=_typed("min_spread_cents", 0.0, float),
        heartbeat_file=data.get("heartbeat_file"),
        pre_mint_max_total_collateral_usd=_typed(
            "pre_mint_max_total_collateral_usd", None, float
        ),
        pre_mint_listing_price_yes_cents=_typed(
            "pre_mint_listing_price_yes_cents", 1, int
        ),
        mint_cushion_multiplier=_typed("mint_cushion_multiplier", 1.0, float),
        pre_mint_settlement_buffer=_typed(
            "pre_mint_settlement_buffer", 300.0, float
        ),
        pricing_source=data.get("pricing_source", "black_scholes"),
    )
    # Suppress the default-streams fallback. Config.__post_init__ replaces
    # an empty `streams=` with `get_default_streams()` (six testnet entries)
    # so passing `streams=[]` to the constructor would NOT keep the list
    # empty. Wipe it AFTER __post_init__ runs so YAML-driven deploys do
    # not carry a phantom testnet-stream list. Market registration goes
    # through the returned MarketEntry list, not Config.streams.
    config.streams = []

    markets: list[MarketEntry] = []
    for idx, raw in enumerate(data.get("markets") or []):
        if not isinstance(raw, dict):
            raise ValueError(
                f"markets[{idx}] must be a mapping, got {type(raw).__name__}"
            )
        unknown_market = set(raw.keys()) - _KNOWN_MARKET_KEYS
        if unknown_market:
            raise ValueError(
                f"markets[{idx}]: unknown key(s) {sorted(unknown_market)}. "
                f"Allowed keys: {sorted(_KNOWN_MARKET_KEYS)}"
            )
        if "query_id" not in raw:
            raise ValueError(f"markets[{idx}]: missing required key 'query_id'")
        if "stream_id" not in raw or "name" not in raw:
            raise ValueError(
                f"markets[{idx}]: missing required key(s) "
                f"(need at least 'stream_id' and 'name')"
            )
        query_id = int(raw["query_id"])
        stream_kwargs = {k: v for k, v in raw.items() if k != "query_id"}
        try:
            stream = StreamConfig(**stream_kwargs)
        except TypeError as exc:
            raise ValueError(
                f"markets[{idx}] (query_id={query_id}): StreamConfig "
                f"rejected the supplied keys: {exc}"
            ) from exc
        markets.append(MarketEntry(query_id=query_id, stream=stream))

    return config, markets


def load_config_from_yaml(path: str) -> tuple[Config, list[MarketEntry]]:
    """Load Config + market entries from a YAML file path.

    Thin wrapper around `load_config_from_dict`: opens the file with
    `yaml.safe_load` (no arbitrary tag execution), then delegates. Raises
    `FileNotFoundError` if the path does not exist and `ValueError` if
    the YAML parses but is structurally invalid.
    """
    import yaml
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"LP config file not found: {path}")
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"LP config at {path} must parse to a mapping, got {type(data).__name__}"
        )
    return load_config_from_dict(data)
