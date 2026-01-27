"""
Order book data fetching and parsing.

Handles conversion between SDK data structures and internal models,
including price encoding (negative=bid, positive=ask, zero=holding).
"""

import time
from typing import Optional

from .models import OrderLevel, MarketState


def parse_order_book_entries(
    entries: list[dict],
    current_time: Optional[float] = None,
) -> tuple[list[OrderLevel], list[OrderLevel]]:
    """
    Parse SDK order book entries into bid and ask levels.

    The SDK returns entries with price encoding:
    - Negative price = open buy order (bid)
    - Positive price = open sell order (ask)
    - Zero = holding tokens (not in order book)

    Args:
        entries: List of OrderBookEntry dicts from SDK
        current_time: Current timestamp for age calculation (defaults to now)

    Returns:
        Tuple of (bids, asks) sorted best to worst:
        - Bids: highest to lowest (best bid first)
        - Asks: lowest to highest (best ask first)
    """
    if current_time is None:
        current_time = time.time()

    bids = []
    asks = []

    for entry in entries:
        price = entry["price"]
        amount = entry["amount"]
        last_updated = entry.get("last_updated", current_time)

        # Calculate age in seconds
        age_seconds = max(0, current_time - last_updated)

        if price < 0:
            # Bid (buy order) - store with original negative price
            bids.append(
                OrderLevel(
                    price=price,
                    quantity=amount,
                    age_seconds=age_seconds,
                    wallet_address=entry.get("wallet_address"),
                )
            )
        elif price > 0:
            # Ask (sell order)
            asks.append(
                OrderLevel(
                    price=price,
                    quantity=amount,
                    age_seconds=age_seconds,
                    wallet_address=entry.get("wallet_address"),
                )
            )
        # price == 0 means holding, skip for order book

    # Sort bids by absolute price value, highest first (best bid = highest willingness to pay)
    # e.g., -49, -48, -47 -> best bid is -49 (willing to pay 49 cents)
    bids.sort(key=lambda x: abs(x.price), reverse=True)

    # Sort asks: lowest to highest
    # e.g., 56, 60, 65 -> best ask is 56
    asks.sort(key=lambda x: x.price)

    return bids, asks


def parse_depth_levels(
    depth_data: list[dict],
) -> tuple[list[OrderLevel], list[OrderLevel]]:
    """
    Parse aggregated depth levels into bid and ask levels.

    Args:
        depth_data: List of DepthLevel dicts from SDK's get_market_depth()

    Returns:
        Tuple of (bids, asks) sorted best to worst
    """
    bids = []
    asks = []

    for level in depth_data:
        price = level["price"]
        total_amount = level["total_amount"]

        order_level = OrderLevel(
            price=price,
            quantity=total_amount,
            age_seconds=0.0,  # Aggregated data doesn't have age
        )

        if price < 0:
            bids.append(order_level)
        elif price > 0:
            asks.append(order_level)

    # Sort: bids high to low, asks low to high
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return bids, asks


def get_best_prices_from_levels(
    bids: list[OrderLevel],
    asks: list[OrderLevel],
) -> tuple[Optional[int], Optional[int]]:
    """
    Extract best bid/ask from sorted level lists.

    Args:
        bids: Bid levels sorted best (highest) to worst
        asks: Ask levels sorted best (lowest) to worst

    Returns:
        Tuple of (best_bid, best_ask) in absolute positive values,
        or None if no orders on that side.
    """
    best_bid = abs(bids[0].price) if bids else None
    best_ask = asks[0].price if asks else None
    return best_bid, best_ask


def build_market_state(
    query_id: int,
    outcome: bool,
    order_book_entries: list[dict],
    current_time: Optional[float] = None,
) -> MarketState:
    """
    Build a MarketState from SDK order book data.

    Args:
        query_id: Market ID
        outcome: True for YES shares, False for NO shares
        order_book_entries: Raw entries from SDK's get_order_book()
        current_time: Current timestamp for age calculation

    Returns:
        MarketState with parsed bids, asks, and best prices
    """
    bids, asks = parse_order_book_entries(order_book_entries, current_time)
    best_bid, best_ask = get_best_prices_from_levels(bids, asks)

    return MarketState(
        query_id=query_id,
        outcome=outcome,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_levels=bids,
        ask_levels=asks,
    )


def convert_price_for_order(price: int, side: str) -> int:
    """
    Convert a display price (1-99) to SDK order price format.

    For buy orders (bids): SDK expects negative price
    For sell orders (asks): SDK expects positive price

    Args:
        price: Display price in cents (1-99)
        side: "bid" for buy orders, "ask" for sell orders

    Returns:
        Price in SDK format (negative for bids, positive for asks)
    """
    if not 1 <= price <= 99:
        raise ValueError(f"price must be 1-99, got {price}")

    if side == "bid":
        return -price
    elif side == "ask":
        return price
    else:
        raise ValueError(f"side must be 'bid' or 'ask', got {side}")


def levels_to_positive_prices(levels: list[OrderLevel]) -> list[OrderLevel]:
    """
    Convert order levels to use positive prices for VWAP calculation.

    The pricing module expects positive prices. This converts bid levels
    (which have negative prices in SDK format) to positive.

    Args:
        levels: Order levels with SDK price format

    Returns:
        New list with absolute price values
    """
    return [
        OrderLevel(
            price=abs(level.price),
            quantity=level.quantity,
            age_seconds=level.age_seconds,
            wallet_address=level.wallet_address,
        )
        for level in levels
    ]
