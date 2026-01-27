#!/usr/bin/env python3
"""
Interactive step-by-step demonstration of the LP Bot pricing logic.

Run with: python interactive_demo.py
"""

import sys
sys.path.insert(0, '.')

from lp_bot.sample_data import get_sample_order_book_with_spread
from lp_bot.order_book import parse_order_book_entries, get_best_prices_from_levels
from lp_bot.pricing import equal_weighted_pricing, volume_weighted_pricing, time_decay_weight
from lp_bot.config import PricingMethod


def wait_for_enter(msg="Press Enter to continue..."):
    input(f"\n{msg}")
    print()


def print_header(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_order_book_raw(orders):
    """Print raw order book data."""
    print(f"\n{'Wallet':<20} {'Price':>8} {'Amount':>10} {'Last Updated':>15}")
    print("-" * 55)
    for order in orders:
        wallet = order['wallet_address'].hex()[:8] + "..."
        print(f"{wallet:<20} {order['price']:>8} {order['amount']:>10} {order['last_updated']:>15}")


def print_parsed_levels(bids, asks):
    """Print parsed bid and ask levels."""
    print("\n  BIDS (sorted best to worst):")
    print(f"  {'Price':>8} {'Quantity':>10} {'Age (s)':>10}")
    print("  " + "-" * 30)
    for level in bids:
        print(f"  {level.price:>8} {level.quantity:>10} {level.age_seconds:>10.1f}")

    print("\n  ASKS (sorted best to worst):")
    print(f"  {'Price':>8} {'Quantity':>10} {'Age (s)':>10}")
    print("  " + "-" * 30)
    for level in asks:
        print(f"  {level.price:>8} {level.quantity:>10} {level.age_seconds:>10.1f}")


def main():
    print_header("LP Bot Interactive Demo")
    print("""
This demo walks through the pricing calculation step by step.

We'll use:
- Market: query_id=1, outcome=True (YES shares)
- Bounds: ±10% from mid (dynamically calculated)
- Alpha: 0.30 (risk tolerance)
""")
    wait_for_enter()

    # Step 1: Load sample data
    print_header("Step 1: Load Sample Order Book Data")

    query_id = 1
    outcome = True
    orders = get_sample_order_book_with_spread(query_id, outcome)

    print(f"Fetched {len(orders)} orders for market {query_id}, outcome={outcome}")
    print_order_book_raw(orders)

    print("""
Note: Price encoding:
  - Negative = BID (buy order) - e.g., -49 means willing to buy at 49
  - Positive = ASK (sell order) - e.g., 51 means willing to sell at 51
  - Zero = Holding (not in order book)
""")
    wait_for_enter()

    # Step 2: Parse into bids and asks
    print_header("Step 2: Parse Into Bids and Asks")

    bids, asks = parse_order_book_entries(orders, current_time=1764387000)

    print("Parsed and sorted order levels:")
    print_parsed_levels(bids, asks)

    print("""
Bids sorted: highest (best) to lowest (worst)
  Best bid = -49 -> willing to pay 49 cents

Asks sorted: lowest (best) to highest (worst)
  Best ask = 51 -> willing to sell at 51 cents
""")
    wait_for_enter()

    # Step 3: Extract best prices
    print_header("Step 3: Extract Best Bid/Ask")

    best_bid, best_ask = get_best_prices_from_levels(bids, asks)
    mid_price = (best_bid + best_ask) / 2
    spread = best_ask - best_bid

    print(f"""
Best Bid:  {best_bid} cents (absolute value of {bids[0].price})
Best Ask:  {best_ask} cents
Mid Price: {mid_price} cents
Spread:    {spread} cents ({spread/mid_price*100:.1f}%)
""")
    wait_for_enter()

    # Step 4: Define bounds and alpha
    print_header("Step 4: Define Pricing Parameters")

    bounds_pct = 0.10  # ±10% from mid
    alpha = 0.30

    # Calculate dynamic bounds from mid price
    lower_bound = mid_price * (1 - bounds_pct)
    upper_bound = mid_price * (1 + bounds_pct)

    print(f"""
Dynamic Bounds (calculated from mid price):
  Mid Price:   {mid_price} cents
  Bounds %:    ±{bounds_pct*100:.0f}%
  Lower Bound: {mid_price} - {mid_price}*{bounds_pct} = {lower_bound:.1f} cents
  Upper Bound: {mid_price} + {mid_price}*{bounds_pct} = {upper_bound:.1f} cents

Alpha (risk tolerance): {alpha}
  - 0.0 = Aggressive (place orders at best bid/ask)
  - 1.0 = Passive (place orders at bounds)
  - 0.3 = 30% toward bounds from best prices

Note: Bounds are recalculated each block based on current mid price!
""")
    wait_for_enter()

    # Step 5: Equal-weighted pricing calculation
    print_header("Step 5: Equal-Weighted Pricing Calculation")

    print(f"""
Formula:
  bid_price = best_bid - alpha * (best_bid - lower_bound)
  ask_price = best_ask + alpha * (upper_bound - best_ask)

Calculation:
  bid_price = {best_bid} - {alpha} * ({best_bid} - {lower_bound})
            = {best_bid} - {alpha} * {best_bid - lower_bound}
            = {best_bid} - {alpha * (best_bid - lower_bound)}
            = {best_bid - alpha * (best_bid - lower_bound)}

  ask_price = {best_ask} + {alpha} * ({upper_bound} - {best_ask})
            = {best_ask} + {alpha} * {upper_bound - best_ask}
            = {best_ask} + {alpha * (upper_bound - best_ask)}
            = {best_ask + alpha * (upper_bound - best_ask)}
""")

    result = equal_weighted_pricing(
        best_bid=best_bid,
        best_ask=best_ask,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        alpha=alpha,
    )

    print(f"""
Result:
  Calculated Bid: {result.bid_price:.2f} -> rounded to {round(result.bid_price)} cents
  Calculated Ask: {result.ask_price:.2f} -> rounded to {round(result.ask_price)} cents

  Bid Anchor: {result.bid_anchor} (best bid)
  Ask Anchor: {result.ask_anchor} (best ask)
""")
    wait_for_enter()

    # Step 6: Visualize on price ladder
    print_header("Step 6: Visualize Price Ladder")

    print("""
                        PRICE LADDER
                            |
    Upper Bound ────────────┼────── {ub:.0f}   (mid + {pct:.0f}%)
                            |
    Our Ask Order ──────────┼────── {ask:.0f}   <-- We place ask here
                            |
    Best Ask ───────────────┼────── {ba}    (existing sell orders)
                            |
    Mid Price ──────────────┼────── {mid:.0f}
                            |
    Best Bid ───────────────┼────── {bb}    (existing buy orders)
                            |
    Our Bid Order ──────────┼────── {bid:.0f}   <-- We place bid here
                            |
    Lower Bound ────────────┼────── {lb:.0f}   (mid - {pct:.0f}%)
                            |
""".format(
        ub=upper_bound, lb=lower_bound, pct=bounds_pct*100,
        mid=mid_price, bb=best_bid, ba=best_ask,
        bid=result.bid_price, ask=result.ask_price
    ))
    wait_for_enter()

    # Step 7: Volume-weighted pricing
    print_header("Step 7: Volume-Weighted Pricing (Alternative)")

    print("""
Volume-weighted pricing uses VWAP (Volume-Weighted Average Price)
instead of just the best bid/ask.

This accounts for liquidity depth - if there's more volume at
worse prices, the anchor moves away from best bid/ask.
""")

    # Convert bids to positive prices for VWAP
    from lp_bot.order_book import levels_to_positive_prices
    bids_positive = levels_to_positive_prices(bids)
    asks_positive = levels_to_positive_prices(asks)

    print("Bid levels for VWAP (positive prices):")
    for lvl in bids_positive[:3]:
        print(f"  Price: {lvl.price}, Qty: {lvl.quantity}")

    target_depth = 0.30
    print(f"\nTarget depth: {target_depth*100:.0f}% of total volume")

    result_vw = volume_weighted_pricing(
        bids=bids_positive,
        asks=asks_positive,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        alpha=alpha,
        target_depth_pct=target_depth,
        apply_time_decay=False,
    )

    print(f"""
VWAP Calculation:
  Bid VWAP (anchor): {result_vw.bid_anchor:.2f} cents
  Ask VWAP (anchor): {result_vw.ask_anchor:.2f} cents

  (VWAP is typically worse than best price because it
   includes volume at deeper price levels)

Volume-Weighted Result:
  Calculated Bid: {result_vw.bid_price:.2f} cents
  Calculated Ask: {result_vw.ask_price:.2f} cents
""")
    wait_for_enter()

    # Step 8: Time decay effect
    print_header("Step 8: Time Decay (Optional)")

    print("""
Time decay reduces the effective quantity of stale orders.
This makes the VWAP more responsive to fresh quotes.

Decay formula: weight = e^(-lambda * age)
  where lambda = ln(2) / half_life
""")

    half_life = 60.0
    ages = [0, 30, 60, 120]

    print(f"\nWith half_life = {half_life} seconds:")
    print(f"  {'Age (s)':<10} {'Weight':<10} {'Effect'}")
    print("  " + "-" * 35)
    for age in ages:
        weight = time_decay_weight(age, half_life)
        effect = "Full weight" if age == 0 else f"{weight*100:.1f}% of original"
        print(f"  {age:<10} {weight:<10.3f} {effect}")

    print("""
With time decay enabled, a 1000-share order that's 60 seconds old
is treated as only 500 shares for VWAP calculation.
""")
    wait_for_enter()

    # Step 9: Final recommendations
    print_header("Step 9: Final Order Recommendations")

    order_amount = 100
    bid_int = round(result.bid_price)
    ask_int = round(result.ask_price)

    print(f"""
Based on equal-weighted pricing with alpha={alpha}:

RECOMMENDED ORDERS:

  BID ORDER:
    Market:   {query_id}
    Outcome:  YES (True)
    Side:     BID (buy)
    Price:    {bid_int} cents
    Amount:   {order_amount} shares

  ASK ORDER:
    Market:   {query_id}
    Outcome:  YES (True)
    Side:     ASK (sell)
    Price:    {ask_int} cents
    Amount:   {order_amount} shares

These orders would be placed within the rewards-eligible bounds
[{lower_bound}, {upper_bound}] to earn LP rewards.
""")

    print_header("Demo Complete")
    print("""
To run the bot:
  python -m lp_bot.main --alpha 0.3 --dry-run --sample-data

To see JSON output:
  cat lp_bot_orders.json
""")


if __name__ == "__main__":
    main()
