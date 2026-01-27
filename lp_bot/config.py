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

    def __post_init__(self):
        if not 0 < self.bounds_pct < 1:
            raise ValueError(
                f"Invalid bounds_pct: {self.bounds_pct}. "
                "Must be between 0 and 1 (e.g., 0.10 for ±10%)"
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
    node_url: str = "https://gateway.mainnet.truf.network"
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

    # Scheduler parameters
    block_interval_seconds: float = 2.0  # Approximate block time
    check_interval_seconds: float = 5.0  # How often to check for new blocks

    # Configured streams with rewards-eligible bounds
    streams: list[StreamConfig] = field(default_factory=list)

    def __post_init__(self):
        if not 0 <= self.alpha <= 1:
            raise ValueError(f"alpha must be in [0, 1], got {self.alpha}")

        # Initialize default streams if empty
        if not self.streams:
            self.streams = get_default_streams()


def get_default_streams() -> list[StreamConfig]:
    """
    Get default stream configurations with bounds as percentage from mid.

    Stream IDs provided by user:
    - st1e321de22ece39a258bc2588dd2871 - US Inflation YoY
    - st8f1e62d3a130572ec468dda082f889 - US CPI Index
    - st1d6d41423cd9746a81ea6063b1345e - US CPI Index Alt
    - ste03c2844c591a10d8a524d14d23066 - EU Inflation YoY
    - ste909219dce3f693c61a0f187758fb0 - EU CPI Index
    - stf6584cf470744723c90130130cb7db - Egg Price

    bounds_pct: Percentage from mid price for rewards-eligible bounds.
    E.g., 0.10 means bounds are at mid ± 10%
    If mid=50 and bounds_pct=0.10, bounds are [45, 55]
    """
    return [
        StreamConfig(
            stream_id="st1e321de22ece39a258bc2588dd2871",
            name="US Inflation YoY",
            bounds_pct=0.10,  # ±10% from mid
        ),
        StreamConfig(
            stream_id="st8f1e62d3a130572ec468dda082f889",
            name="US CPI Index",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="st1d6d41423cd9746a81ea6063b1345e",
            name="US CPI Index Alt",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="ste03c2844c591a10d8a524d14d23066",
            name="EU Inflation YoY",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="ste909219dce3f693c61a0f187758fb0",
            name="EU CPI Index",
            bounds_pct=0.10,
        ),
        StreamConfig(
            stream_id="stf6584cf470744723c90130130cb7db",
            name="Egg Price",
            bounds_pct=0.15,  # ±15% from mid (more volatile)
        ),
    ]


def load_config_from_env() -> Config:
    """Load configuration from environment variables."""
    import os

    config = Config(
        node_url=os.getenv("TRUF_NODE_URL", "https://gateway.mainnet.truf.network"),
        api_token=os.getenv("TRUF_API_TOKEN", ""),
        alpha=float(os.getenv("LP_BOT_ALPHA", "0.30")),
        default_order_amount=int(os.getenv("LP_BOT_ORDER_AMOUNT", "100")),
    )

    method = os.getenv("LP_BOT_PRICING_METHOD", "equal").lower()
    if method == "volume":
        config.pricing_method = PricingMethod.VOLUME_WEIGHTED
        config.apply_time_decay = os.getenv("LP_BOT_TIME_DECAY", "false").lower() == "true"
        config.half_life_seconds = float(os.getenv("LP_BOT_HALF_LIFE", "60.0"))
        config.target_depth_pct = float(os.getenv("LP_BOT_TARGET_DEPTH", "0.30"))

    return config
