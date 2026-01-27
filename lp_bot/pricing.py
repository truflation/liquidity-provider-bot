"""
Market Making Bid/Ask Pricing Logic.

Two approaches supported:
1. Equal-Weighted: Linear interpolation from best bid/ask toward eligible bounds
2. Volume-Weighted: Interpolation from cumulative VWAP anchor toward eligible bounds

Based on bid_pricing_logic.md specification.
"""

import math
from typing import Optional

from .models import OrderLevel, PricingResult
from .config import PricingMethod


def time_decay_weight(age_seconds: float, half_life_seconds: float = 60.0) -> float:
    """
    Compute exponential decay weight based on order age.

    Args:
        age_seconds: Age of the order in seconds
        half_life_seconds: Time at which weight decays to 0.5

    Returns:
        Weight in range (0, 1] where 1 = fresh, approaching 0 = stale

    Formula:
        w = e^(-lambda*t) where lambda = ln(2) / half_life
    """
    if age_seconds < 0:
        raise ValueError("age_seconds cannot be negative")

    decay_rate = math.log(2) / half_life_seconds
    return math.exp(-decay_rate * age_seconds)


def equal_weighted_pricing(
    best_bid: float,
    best_ask: float,
    lower_bound: float,
    upper_bound: float,
    alpha: float,
) -> PricingResult:
    """
    Calculate bid/ask prices using linear interpolation from best bid/ask.

    Args:
        best_bid: Current best bid price (positive, e.g., 45 for 45 cents)
        best_ask: Current best ask price (positive, e.g., 55 for 55 cents)
        lower_bound: Lower eligible bound price
        upper_bound: Upper eligible bound price
        alpha: Fraction of range to move from best bid/ask toward bounds (0-1)
               0 = stay at best bid/ask (aggressive)
               1 = move all the way to bounds (passive)

    Returns:
        PricingResult with calculated prices

    Formula:
        bid_price = best_bid - alpha * (best_bid - lower_bound)
        ask_price = best_ask + alpha * (upper_bound - best_ask)
    """
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be in [0, 1]")

    mid_price = (best_bid + best_ask) / 2

    bid_price = best_bid - alpha * (best_bid - lower_bound)
    ask_price = best_ask + alpha * (upper_bound - best_ask)

    return PricingResult(
        bid_price=bid_price,
        ask_price=ask_price,
        mid_price=mid_price,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        bid_anchor=best_bid,
        ask_anchor=best_ask,
    )


def cumulative_vwap(
    levels: list[OrderLevel],
    target_depth_pct: float,
    side: str,
    apply_time_decay: bool = False,
    half_life_seconds: float = 60.0,
) -> float:
    """
    Compute cumulative VWAP at a target depth percentile.

    Args:
        levels: Order levels sorted best to worst
                - Bids: highest to lowest (e.g., 55, 50, 45)
                - Asks: lowest to highest (e.g., 56, 60, 65)
        target_depth_pct: Fraction of total volume to sweep (0-1)
        side: "bid" or "ask" (for documentation)
        apply_time_decay: Whether to decay stale order quantities
        half_life_seconds: Half-life for decay calculation

    Returns:
        VWAP at the target depth

    Note:
        For bids, this represents the avg price to SELL into the book.
        For asks, this represents the avg price to BUY from the book.
    """
    if not levels:
        raise ValueError("No levels provided")

    if not 0 < target_depth_pct <= 1:
        raise ValueError("target_depth_pct must be in (0, 1]")

    # Compute effective quantities with optional time decay
    if apply_time_decay:
        effective_qtys = [
            lvl.quantity * time_decay_weight(lvl.age_seconds, half_life_seconds)
            for lvl in levels
        ]
    else:
        effective_qtys = [float(lvl.quantity) for lvl in levels]

    total_qty = sum(effective_qtys)
    if total_qty == 0:
        raise ValueError("Total quantity is zero")

    target_qty = target_depth_pct * total_qty

    filled_qty = 0.0
    cost = 0.0

    for lvl, eff_qty in zip(levels, effective_qtys):
        take = min(eff_qty, target_qty - filled_qty)
        # Use absolute price value for cost calculation
        cost += abs(lvl.price) * take
        filled_qty += take

        if filled_qty >= target_qty - 1e-9:
            break

    if filled_qty == 0:
        raise ValueError("Could not fill any quantity")

    return cost / filled_qty


def volume_weighted_pricing(
    bids: list[OrderLevel],
    asks: list[OrderLevel],
    lower_bound: float,
    upper_bound: float,
    alpha: float,
    target_depth_pct: float = 0.30,
    apply_time_decay: bool = False,
    half_life_seconds: float = 60.0,
) -> PricingResult:
    """
    Calculate bid/ask prices using VWAP anchors instead of best bid/ask.

    Args:
        bids: Bid levels sorted best (highest) to worst (lowest)
        asks: Ask levels sorted best (lowest) to worst (highest)
        lower_bound: Lower eligible bound price
        upper_bound: Upper eligible bound price
        alpha: Fraction of range to move from VWAP anchor toward bounds
        target_depth_pct: Depth % for VWAP calculation
        apply_time_decay: Whether to apply time decay to quantities
        half_life_seconds: Half-life for decay

    Returns:
        PricingResult with calculated prices

    Logic:
        Bid side:
            - Compute bid_vwap = avg price to sell into top X% of bids
            - bid_vwap will be <= best_bid (deeper = worse for seller)
            - Interpolate from bid_vwap toward lower_bound

        Ask side:
            - Compute ask_vwap = avg price to buy from top X% of asks
            - ask_vwap will be >= best_ask (deeper = worse for buyer)
            - Interpolate from ask_vwap toward upper_bound
    """
    if not bids or not asks:
        raise ValueError("Both bids and asks required")

    if not 0 <= alpha <= 1:
        raise ValueError("alpha must be in [0, 1]")

    # Best prices (use absolute values for calculation)
    best_bid = abs(bids[0].price)
    best_ask = abs(asks[0].price)
    mid_price = (best_bid + best_ask) / 2

    # Calculate VWAP anchors
    bid_vwap = cumulative_vwap(
        bids,
        target_depth_pct,
        side="bid",
        apply_time_decay=apply_time_decay,
        half_life_seconds=half_life_seconds,
    )
    ask_vwap = cumulative_vwap(
        asks,
        target_depth_pct,
        side="ask",
        apply_time_decay=apply_time_decay,
        half_life_seconds=half_life_seconds,
    )

    # Interpolate from VWAP toward bounds
    # Bid: move DOWN from bid_vwap toward lower_bound
    bid_price = bid_vwap - alpha * (bid_vwap - lower_bound)

    # Ask: move UP from ask_vwap toward upper_bound
    ask_price = ask_vwap + alpha * (upper_bound - ask_vwap)

    return PricingResult(
        bid_price=bid_price,
        ask_price=ask_price,
        mid_price=mid_price,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        bid_anchor=bid_vwap,
        ask_anchor=ask_vwap,
    )


def calculate_mm_prices(
    best_bid: Optional[float],
    best_ask: Optional[float],
    lower_bound: float,
    upper_bound: float,
    alpha: float,
    method: PricingMethod = PricingMethod.EQUAL_WEIGHTED,
    bids: Optional[list[OrderLevel]] = None,
    asks: Optional[list[OrderLevel]] = None,
    target_depth_pct: float = 0.30,
    apply_time_decay: bool = False,
    half_life_seconds: float = 60.0,
) -> PricingResult:
    """
    Unified interface for market making price calculation.

    Args:
        best_bid: Best bid price (positive value)
        best_ask: Best ask price (positive value)
        lower_bound: Lower eligible bound price
        upper_bound: Upper eligible bound price
        alpha: Position within range (0=aggressive, 1=passive)
        method: EQUAL_WEIGHTED or VOLUME_WEIGHTED
        bids: For volume weighted - bid levels sorted best to worst
        asks: For volume weighted - ask levels sorted best to worst
        target_depth_pct: For volume weighted - depth for VWAP
        apply_time_decay: For volume weighted - decay stale orders
        half_life_seconds: For volume weighted - decay half-life

    Returns:
        PricingResult with bid/ask prices and metadata

    Raises:
        ValueError: If required inputs are missing or invalid
    """
    # Handle empty/missing book scenario
    if best_bid is None or best_ask is None:
        # Fallback: use midpoint of bounds
        mid = (lower_bound + upper_bound) / 2
        return PricingResult(
            bid_price=lower_bound + alpha * (mid - lower_bound),
            ask_price=upper_bound - alpha * (upper_bound - mid),
            mid_price=mid,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            bid_anchor=mid,
            ask_anchor=mid,
        )

    if method == PricingMethod.EQUAL_WEIGHTED:
        return equal_weighted_pricing(
            best_bid=best_bid,
            best_ask=best_ask,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            alpha=alpha,
        )
    else:
        if not bids or not asks:
            raise ValueError("bids and asks required for volume-weighted pricing")

        return volume_weighted_pricing(
            bids=bids,
            asks=asks,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            alpha=alpha,
            target_depth_pct=target_depth_pct,
            apply_time_decay=apply_time_decay,
            half_life_seconds=half_life_seconds,
        )


def ensure_uncrossed_spread(result: PricingResult, min_spread: float = 1.0) -> PricingResult:
    """
    Ensure bid < ask with minimum spread.

    If the calculated prices result in a crossed market (bid >= ask),
    widen the spread symmetrically around the midpoint.

    Args:
        result: Original pricing result
        min_spread: Minimum required spread between bid and ask

    Returns:
        PricingResult with guaranteed uncrossed spread
    """
    if result.bid_price >= result.ask_price:
        # Crossed market - widen symmetrically
        mid = result.mid_price
        half_spread = min_spread / 2
        return PricingResult(
            bid_price=mid - half_spread,
            ask_price=mid + half_spread,
            mid_price=mid,
            lower_bound=result.lower_bound,
            upper_bound=result.upper_bound,
            bid_anchor=result.bid_anchor,
            ask_anchor=result.ask_anchor,
        )

    if result.ask_price - result.bid_price < min_spread:
        # Spread too tight - widen
        mid = (result.bid_price + result.ask_price) / 2
        half_spread = min_spread / 2
        return PricingResult(
            bid_price=mid - half_spread,
            ask_price=mid + half_spread,
            mid_price=result.mid_price,
            lower_bound=result.lower_bound,
            upper_bound=result.upper_bound,
            bid_anchor=result.bid_anchor,
            ask_anchor=result.ask_anchor,
        )

    return result
