# Truflation Liquidity Provider Bot

An open-source liquidity provider bot for [Truflation](https://truflation.com) prediction markets on the [TRUF.NETWORK](https://truf.network). Simpler than a full market maker, this bot places two-sided orders within a configurable bounds range around the mid price to earn LP rewards.

If you want a friendly way to provide liquidity to TRUF.NETWORK prediction markets and earn a share of the 2% settlement fees, this is the bot for you.

## Background

### What is TRUF.NETWORK?

[TRUF.NETWORK](https://truf.network) is a decentralized oracle network built by [Truflation](https://truflation.com) for publishing and consuming real-world economic data (CPI, inflation, commodity prices, etc.). It powers prediction markets that let users trade on the future value of these data streams.

- **Website**: https://truf.network
- **Documentation**: https://docs.truf.network
- **Token & Governance**: https://docs.truf.network/token-governance/tokenomics
- **Data explorer**: https://trufscan.io
- **GitHub**: https://github.com/trufnetwork

### What are TT and TT2?

**TT** and **TT2** are the testnet tokens used for prediction markets on TRUF.NETWORK testnet. They have no monetary value and are only used for testing the protocol before mainnet. On mainnet, the corresponding collateral token is USDC and the utility token is $TRUF.

- **TT2** is the testnet collateral token (used by the current bot deployment)
- **TT** is the testnet utility token. The utility token is only necessary for the creation of new order books. *Placing orders does not require TT.*
- To run this bot on testnet, you'll need to [acquire the testnet tokens](https://github.com/trufnetwork/node/blob/main/docs/testnet-wallet-funding.md)
- To run this bot on mainnet, you'll need both USDC and [$TRUF tokens instead](https://docs.truf.network/token-governance/get-truf-token)

### Prediction Markets on TRUF.NETWORK

Each market is a binary prediction on a data stream outcome (e.g., "Will US CPI YoY be between 1.3% and 1.5% on April 10?"). Markets have two sides: YES and NO. Share prices range from 1c to 99c, where `YES price + NO price = 100c`. Liquidity providers earn a portion of the 2% settlement fee based on how long their orders stayed within the rewards-eligible spread.

## What It Does

- Places bid and ask orders on both YES and NO outcomes for configured markets
- Positions orders within a dynamic bounds range (default +/-10% from mid price)
- Continuously monitors the order book and updates orders as the market moves
- Pulls all liquidity 15 minutes before settlement to protect capital
- Supports two pricing strategies: equal-weighted and volume-weighted (VWAP)
- Handles stale orders gracefully

## How LPs Earn Rewards

TRUF.NETWORK charges a 2% fee on market settlements. A portion of that fee is distributed to liquidity providers based on:

- **Eligibility**: Orders must be paired (buy on one outcome + sell on the opposite at complementary prices summing to 100c)
- **Spread tightness**: Tighter spreads (closer to mid) earn higher scores via dynamic spread tiers
- **Duration**: Rewards are proportional to "liquidity-hours" - how long your eligible orders stay on the book
- **Size**: Larger orders above the minimum size earn proportionally more

Per-block snapshots track LP positions while the market is live. Rewards are calculated and distributed atomically at settlement.

To qualify for rewards, your orders must sit **within the rewards-eligible range** (the `bounds_pct` percentage around the mid price). Orders placed outside this range will not earn rewards, even if they get filled.

## Features

- **Dynamic bounds**: Rewards-eligible range recalculated every cycle based on current mid price
- **Two pricing strategies**:
  - **Equal-weighted**: Anchors orders to the best bid/ask, then shifts toward bounds
  - **Volume-weighted (VWAP)**: Anchors to depth-weighted average with optional time decay for stale orders
- **Configurable risk tolerance** via alpha parameter (0 = aggressive, 1 = passive)
- **Pre-settlement cutoff** (default 15 min) pulls liquidity before market settlement
- **Dual-sided asks**: places YES asks via `place_sell_order` after minting share pairs
- **Stale order detection**: handles "order not found" errors gracefully
- **Dry-run mode** and **sample data mode** for testing without network calls
- **JSON output** of all order recommendations for monitoring
- **Auto-discovery** of active markets on the network
- **Read-only mode**: calculate and log without placing orders

## Requirements

- Python 3.12+
- A TRUF.NETWORK private key (generate one with `--generate-key`)
- TT2 tokens to collateralize orders

## Installation

```bash
git clone https://github.com/truflation/liquidity-provider-bot.git
cd liquidity-provider-bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install git+https://github.com/trufnetwork/sdk-py.git
```

## Quick Start

1. Set up environment:
   ```bash
   cp .env.example .env
   # Edit .env and add your private key (without 0x prefix)
   ```

2. Generate a new private key (if you don't have one):
   ```bash
   python -m lp_bot.main --generate-key
   ```

3. Run in sample data mode (no network or funds needed):
   ```bash
   python -m lp_bot.main --sample-data --dry-run
   ```

4. Try the interactive demo:
   ```bash
   python interactive_demo.py
   ```

5. Dry run against the live network (reads real order books, doesn't place orders):
   ```bash
   python -m lp_bot.main --discover-markets --dry-run
   ```

6. Run live on specific markets:
   ```bash
   python -m lp_bot.main --query-ids 38,39,40 --alpha 0.3
   ```

7. Auto-discover and provide liquidity on all active markets:
   ```bash
   python -m lp_bot.main --discover-markets
   ```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TRUF_NODE_URL` | TRUF.NETWORK gateway URL | `https://gateway.mainnet.truf.network` |
| `TRUF_API_TOKEN` | Private key (64 hex chars, no `0x` prefix) | (required) |
| `LP_BOT_ALPHA` | Risk tolerance 0-1 | 0.30 |
| `LP_BOT_PRICING_METHOD` | `equal` or `volume` | equal |
| `LP_BOT_ORDER_AMOUNT` | Default order size in shares | 100 |
| `LP_BOT_TIME_DECAY` | Enable time decay for VWAP | false |
| `LP_BOT_HALF_LIFE` | Time decay half-life in seconds | 60.0 |
| `LP_BOT_TARGET_DEPTH` | VWAP depth as fraction | 0.30 |

### Command-Line Options

| Option | Description |
|--------|-------------|
| `--query-ids <list>` | Comma-separated market IDs (e.g., `38,39,40`) |
| `--discover-markets` | Auto-discover active markets on the network |
| `--alpha <0-1>` | Risk tolerance: 0=aggressive, 1=passive |
| `--method <equal\|volume>` | Pricing method |
| `--time-decay` | Enable time decay for VWAP |
| `--half-life <seconds>` | Decay half-life |
| `--target-depth <0-1>` | VWAP depth percentage |
| `--order-amount <n>` | Order size in shares |
| `--interval <seconds>` | Update interval (default 5s) |
| `--output, -o <path>` | JSON output file (default `lp_bot_orders.json`) |
| `--dry-run` | Calculate but don't place orders |
| `--sample-data` | Use sample data (no network calls) |
| `--generate-key` | Generate a new private key and exit |
| `--debug` | Verbose logging |

### Pre-Configured Streams

Default streams in `lp_bot/config.py`:

| Stream ID | Name | Bounds |
|-----------|------|--------|
| `st1e321de22ece39a258bc2588dd2871` | US Inflation YoY | +/-10% |
| `st8f1e62d3a130572ec468dda082f889` | US CPI Index | +/-10% |
| `ste03c2844c591a10d8a524d14d23066` | EU Inflation YoY | +/-10% |
| `ste909219dce3f693c61a0f187758fb0` | EU CPI Index | +/-10% |
| `stf6584cf470744723c90130130cb7db` | Egg Price | +/-15% |

### Adding Custom Streams

Edit `get_default_streams()` in `lp_bot/config.py`:

```python
def get_default_streams() -> list[StreamConfig]:
    return [
        StreamConfig(
            stream_id="your_stream_id_here",
            name="Your Custom Stream",
            bounds_pct=0.10,       # +/-10% from mid
            min_order_size=100,
        ),
    ]
```

**StreamConfig parameters:**
- `stream_id`: TRUF.NETWORK stream identifier
- `name`: Human-readable name
- `bounds_pct`: Rewards-eligible bounds as fraction (0.10 = +/-10%)
- `min_order_size`: Minimum order size in shares
- `enabled`: Set to `False` to disable

## How It Works

### The Alpha Parameter

Alpha controls where your orders are placed within the rewards-eligible range:

```
alpha = 0    : aggressive (orders at best bid/ask, tight spread)
alpha = 0.3  : slightly aggressive (default)
alpha = 0.5  : balanced
alpha = 1    : passive (orders at bounds edges, wide spread)
```

Lower alpha = tighter quotes = more fills but more adverse selection risk.
Higher alpha = wider quotes = fewer fills but safer.

### Equal-Weighted Pricing

```
bid_price = best_bid - alpha * (best_bid - lower_bound)
ask_price = best_ask + alpha * (upper_bound - best_ask)
```

Simple and predictable. Recommended for most users.

### Volume-Weighted Pricing (VWAP)

Uses depth-weighted averages instead of best bid/ask as anchors:

```
bid_vwap = volume-weighted average of top X% bids
ask_vwap = volume-weighted average of top X% asks
bid_price = bid_vwap - alpha * (bid_vwap - lower_bound)
ask_price = ask_vwap + alpha * (upper_bound - ask_vwap)
```

More sophisticated - reacts to order book depth, not just the top of book. Optional time decay reduces the influence of stale orders.

### Dynamic Bounds

Bounds are recalculated every update cycle as a percentage from the current mid price:

```
lower_bound = mid_price * (1 - bounds_pct)
upper_bound = mid_price * (1 + bounds_pct)
```

Example: if mid is 50c and bounds_pct is 0.10, the eligible range is 45c-55c.

### Order Placement

The bot places 2 orders per market:

1. **Bid (YES)**: `place_buy_order(outcome=True, price=bid_price)`
2. **Ask**: two-step process
   - `place_split_limit_order(true_price=ask_price)` mints YES+NO share pairs and lists NO at `100 - ask_price`
   - `place_sell_order(outcome=True, price=ask_price)` lists the retained YES shares as a YES ask
   - This ensures asks appear on **both sides** of the order book

### Pre-Settlement Cutoff

15 minutes before a market's settle time, the bot:
1. Cancels all orders for that market
2. Stops placing new orders
3. Skips the market on subsequent cycles

This protects your capital from oracle/settlement risk.

## Output JSON

Order recommendations are written to `lp_bot_orders.json`:

```json
{
  "generated_at": "2026-04-07T12:00:00Z",
  "config": {
    "pricing_method": "equal",
    "alpha": 0.3,
    "default_order_amount": 100
  },
  "markets": [
    {
      "query_id": 1,
      "stream_name": "US Inflation YoY",
      "best_bid": 45,
      "best_ask": 55,
      "mid_price": 50.0,
      "lower_bound": 45,
      "upper_bound": 55,
      "calculated_bid": 43.5,
      "calculated_ask": 56.5,
      "recommendations": [
        {"side": "bid", "price": 44, "amount": 100},
        {"side": "ask", "price": 57, "amount": 100}
      ]
    }
  ]
}
```

Use this for monitoring, dashboards, or integration with other tools.

## Testnet vs Mainnet

Default is mainnet. To use testnet:

```bash
export TRUF_NODE_URL="https://gateway.testnet.truf.network"
```

## File Structure

```
liquidity-provider-bot/
├── lp_bot/
│   ├── __init__.py
│   ├── bot.py              # Main bot
│   ├── config.py           # Configuration
│   ├── main.py             # CLI entry point
│   ├── models.py           # Data models
│   ├── order_book.py       # Order book parsing
│   ├── pricing.py          # Pricing strategies
│   └── sample_data.py      # Sample order books (testing)
├── interactive_demo.py     # Step-by-step visualization
├── requirements.txt
├── .env.example
└── README.md
```

## Risk Disclaimer

This software is provided as-is with no warranty. Providing liquidity involves financial risk, including:

- **Adverse selection**: informed traders may pick off stale quotes
- **Inventory risk**: holding shares exposes you to settlement outcomes
- **Oracle risk**: data provider issues can affect settlement
- **Smart contract risk**: protocol bugs or exploits
- **Gas/fee costs**: transaction fees can eat into profits

The pre-settlement cutoff is a risk mitigation but does not eliminate all risks. Only provide liquidity with capital you can afford to lose. Understand the protocol mechanics before running live.

## Related Tools

- [Market Maker Bot](https://github.com/truflation/market-maker-bot) - Advanced Avellaneda-Stoikov market maker with multi-level orders and Black-Scholes pricing

## License

MIT
