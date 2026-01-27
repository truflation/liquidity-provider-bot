"""
Liquidity Provider Bot for TRUF Network Prediction Markets.

This bot automatically provides liquidity to prediction markets by placing
bid/ask orders within rewards-eligible bounds using either equal-weighted
or volume-weighted pricing strategies.
"""

from .config import Config, StreamConfig, PricingMethod
from .pricing import calculate_mm_prices
from .models import PricingResult

# Lazy import for LiquidityProviderBot to avoid loading Go bindings on import
def __getattr__(name):
    if name == "LiquidityProviderBot":
        from .bot import LiquidityProviderBot
        return LiquidityProviderBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "Config",
    "StreamConfig",
    "PricingMethod",
    "calculate_mm_prices",
    "PricingResult",
    "LiquidityProviderBot",
]
