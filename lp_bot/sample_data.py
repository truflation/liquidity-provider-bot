"""
Sample order book data based on orderbook_positions screenshot.

This data can be used for testing when real markets are not available.

Data structure from positions table:
- query_id: market identifier
- participant_id: user wallet ID
- outcome: boolean (true=YES, false=NO)
- price: -99 to +99 (negative=bid, positive=ask, 0=holding)
- amount: token quantity
- last_updated: timestamp
"""

# Sample data extracted from orderbook_positions.jpg screenshot
# Format matches SDK's get_order_book() return structure
SAMPLE_ORDER_BOOKS = {
    # Market 1 - YES outcome (outcome=True)
    (1, True): [
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -32, "amount": 100, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": 38, "amount": 300, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -60, "amount": 1000, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -61, "amount": 1000, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -71, "amount": 500, "last_updated": 1764386995},
    ],
    # Market 1 - NO outcome (outcome=False)
    (1, False): [
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -68, "amount": 100, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -31, "amount": 700, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -69, "amount": 700, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -62, "amount": 300, "last_updated": 1764386995},
    ],
    # Market 2 - YES outcome (outcome=True)
    (2, True): [
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -71, "amount": 500, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x12", "price": -70, "amount": 200, "last_updated": 1764386995},
    ],
    # Market 2 - NO outcome (outcome=False)
    (2, False): [],
}


def get_sample_order_book(query_id: int, outcome: bool) -> list[dict]:
    """
    Get sample order book data for testing.

    Args:
        query_id: Market ID
        outcome: True for YES, False for NO

    Returns:
        List of order book entries in SDK format
    """
    return SAMPLE_ORDER_BOOKS.get((query_id, outcome), [])


# Additional sample data with tighter spreads for testing pricing logic
# Orders are centered around mid=50, within ±10% bounds [45, 55]
# Note: Bids use negative prices. When sorted by price descending (reverse=True),
# -49 > -50 > -51, so -49 is "best" (highest willingness to pay = 49 cents)
SAMPLE_ORDER_BOOKS_WITH_SPREAD = {
    # Market 1 - YES outcome with both bids and asks
    # Best bid = 49, Best ask = 51, Mid = 50, Bounds at ±10% = [45, 55]
    (1, True): [
        # Bids (negative prices) - after sort: -49, -48, -47, -46, -45
        # Best bid = 49 cents (well within upper bound)
        {"wallet_address": b"\x00" * 19 + b"\x01", "price": -49, "amount": 200, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x02", "price": -48, "amount": 500, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x03", "price": -47, "amount": 1000, "last_updated": 1764386980},
        {"wallet_address": b"\x00" * 19 + b"\x04", "price": -46, "amount": 800, "last_updated": 1764386970},
        {"wallet_address": b"\x00" * 19 + b"\x05", "price": -45, "amount": 500, "last_updated": 1764386960},
        # Asks (positive prices) - after sort: 51, 52, 53, 54, 55
        # Best ask = 51 cents (well within bounds)
        {"wallet_address": b"\x00" * 19 + b"\x06", "price": 51, "amount": 300, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x07", "price": 52, "amount": 600, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x08", "price": 53, "amount": 900, "last_updated": 1764386980},
        {"wallet_address": b"\x00" * 19 + b"\x09", "price": 54, "amount": 700, "last_updated": 1764386970},
        {"wallet_address": b"\x00" * 19 + b"\x0a", "price": 55, "amount": 400, "last_updated": 1764386960},
    ],
    # Market 1 - NO outcome (complementary to YES)
    (1, False): [
        # Best bid = 49, Best ask = 51
        {"wallet_address": b"\x00" * 19 + b"\x01", "price": -49, "amount": 300, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x02", "price": -48, "amount": 600, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x03", "price": -47, "amount": 900, "last_updated": 1764386980},
        {"wallet_address": b"\x00" * 19 + b"\x06", "price": 51, "amount": 200, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x07", "price": 52, "amount": 500, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x08", "price": 53, "amount": 1000, "last_updated": 1764386980},
    ],
    # Market 2 - YES outcome
    # Best bid = 48, Best ask = 52, Mid = 50
    (2, True): [
        {"wallet_address": b"\x00" * 19 + b"\x01", "price": -48, "amount": 400, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x02", "price": -47, "amount": 600, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x03", "price": -46, "amount": 800, "last_updated": 1764386980},
        {"wallet_address": b"\x00" * 19 + b"\x06", "price": 52, "amount": 350, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x07", "price": 53, "amount": 550, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x08", "price": 54, "amount": 750, "last_updated": 1764386980},
    ],
    # Market 2 - NO outcome
    (2, False): [
        {"wallet_address": b"\x00" * 19 + b"\x01", "price": -48, "amount": 350, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x02", "price": -47, "amount": 550, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x03", "price": -46, "amount": 750, "last_updated": 1764386980},
        {"wallet_address": b"\x00" * 19 + b"\x06", "price": 52, "amount": 400, "last_updated": 1764386995},
        {"wallet_address": b"\x00" * 19 + b"\x07", "price": 53, "amount": 600, "last_updated": 1764386990},
        {"wallet_address": b"\x00" * 19 + b"\x08", "price": 54, "amount": 800, "last_updated": 1764386980},
    ],
}


def get_sample_order_book_with_spread(query_id: int, outcome: bool) -> list[dict]:
    """
    Get sample order book data with realistic bid/ask spread.

    Args:
        query_id: Market ID
        outcome: True for YES, False for NO

    Returns:
        List of order book entries in SDK format
    """
    return SAMPLE_ORDER_BOOKS_WITH_SPREAD.get((query_id, outcome), [])
