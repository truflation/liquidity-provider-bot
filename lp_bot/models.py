"""Data models for the LP Bot."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderLevel:
    """Represents a single order level in the order book."""
    price: int  # -99 to 99 (negative = bid, positive = ask, 0 = holding)
    quantity: int
    age_seconds: float = 0.0
    wallet_address: Optional[bytes] = None


@dataclass
class PricingResult:
    """Result from price calculation."""
    bid_price: float
    ask_price: float
    mid_price: float
    lower_bound: float
    upper_bound: float
    bid_anchor: float  # best bid or VWAP
    ask_anchor: float  # best ask or VWAP

    def to_int_prices(self) -> tuple[int, int]:
        """Convert float prices to integer cents (1-99 range)."""
        bid_int = max(1, min(99, int(round(self.bid_price))))
        ask_int = max(1, min(99, int(round(self.ask_price))))
        return bid_int, ask_int


@dataclass
class MarketState:
    """Current state of a market's order book."""
    query_id: int
    outcome: bool
    best_bid: Optional[int]
    best_ask: Optional[int]
    bid_levels: list[OrderLevel]
    ask_levels: list[OrderLevel]

    @property
    def mid_price(self) -> Optional[float]:
        """Calculate mid price if both sides have orders."""
        if self.best_bid is not None and self.best_ask is not None:
            # Convert from internal encoding: bids are negative
            return (abs(self.best_bid) + self.best_ask) / 2
        return None

    @property
    def has_liquidity(self) -> bool:
        """Check if market has orders on both sides."""
        return self.best_bid is not None and self.best_ask is not None


@dataclass
class BotOrder:
    """An order the bot intends to place or has placed."""
    query_id: int
    outcome: bool
    side: str  # "bid" or "ask"
    price: int  # 1-99 for display, will be converted for placement
    amount: int
    tx_hash: Optional[str] = None
