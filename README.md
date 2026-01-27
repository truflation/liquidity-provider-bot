# Liquidity Provider Bot

Automated liquidity provider for TRUF Network prediction markets.

## Features

- Provides liquidity by placing bid/ask orders within rewards-eligible bounds
- Two pricing strategies: equal-weighted and volume-weighted
- Configurable risk tolerance (alpha parameter)
- Dynamic bounds based on percentage from mid price
- Time decay for stale order weighting
- Sample data mode for testing without network access
- JSON output for order recommendations

## Supported Streams

| Stream ID | Name | Bounds |
|-----------|------|--------|
| st1e321de22ece39a258bc2588dd2871 | US Inflation YoY | ±10% |
| st8f1e62d3a130572ec468dda082f889 | US CPI Index | ±10% |
| st1d6d41423cd9746a81ea6063b1345e | US CPI Index Alt | ±10% |
| ste03c2844c591a10d8a524d14d23066 | EU Inflation YoY | ±10% |
| ste909219dce3f693c61a0f187758fb0 | EU CPI Index | ±10% |
| stf6584cf470744723c90130130cb7db | Egg Price | ±15% |

## Requirements

- Python 3.12 (from python.org for macOS)
- TRUF Network SDK

## Installation

1. Create a virtual environment:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Install TRUF Network SDK:
```bash
pip install git+https://github.com/trufnetwork/sdk-py.git
```

4. Set up environment variables:
```bash
cp .env.example .env
# Edit .env and add your private key (without 0x prefix)
```

## Usage

### Run with sample data (no network required)
```bash
python -m lp_bot.main --sample-data --dry-run
```

### Run interactive demo
```bash
python interactive_demo.py
```

### Generate a new private key
```bash
python -m lp_bot.main --generate-key
```

### Run with equal-weighted pricing
```bash
python -m lp_bot.main --alpha 0.3 --method equal --dry-run
```

### Run with volume-weighted pricing and time decay
```bash
python -m lp_bot.main --alpha 0.3 --method volume --time-decay --half-life 30
```

### View order recommendations
```bash
cat lp_bot_orders.json
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| TRUF_NODE_URL | TRUF Network gateway URL | https://gateway.mainnet.truf.network |
| TRUF_API_TOKEN | Private key (64 hex chars) | - |
| LP_BOT_ALPHA | Risk tolerance (0-1) | 0.30 |
| LP_BOT_PRICING_METHOD | "equal" or "volume" | equal |
| LP_BOT_ORDER_AMOUNT | Default order size | 100 |

### Command Line Options

| Option | Description |
|--------|-------------|
| --alpha | Risk tolerance: 0=aggressive, 1=passive |
| --method | Pricing method: "equal" or "volume" |
| --time-decay | Enable time decay for volume-weighted |
| --half-life | Half-life in seconds for decay |
| --target-depth | Depth % for VWAP calculation |
| --order-amount | Default order size in shares |
| --interval | Check interval in seconds |
| --output, -o | JSON output file path |
| --dry-run | Simulation mode (no orders placed) |
| --sample-data | Use sample data instead of network |
| --debug | Enable debug logging |

### Using a Different Node (Testnet)

To connect to a different node (e.g., testnet instead of mainnet), set the `TRUF_NODE_URL` environment variable:

```bash
# Use testnet
export TRUF_NODE_URL="https://gateway.testnet.truf.network"

# Or set in .env file
echo 'TRUF_NODE_URL=https://gateway.testnet.truf.network' >> .env
```

### Adding Custom Streams

The default streams are configured in `lp_bot/config.py`. To add custom streams or modify existing ones, edit the `get_default_streams()` function:

```python
# In lp_bot/config.py

def get_default_streams() -> list[StreamConfig]:
    return [
        # Existing streams...
        StreamConfig(
            stream_id="st1e321de22ece39a258bc2588dd2871",
            name="US Inflation YoY",
            bounds_pct=0.10,  # ±10% from mid price
        ),
        # Add your custom stream:
        StreamConfig(
            stream_id="your_custom_stream_id_here",
            name="Your Custom Stream Name",
            bounds_pct=0.15,  # ±15% from mid price
            min_order_size=100,
        ),
    ]
```

**StreamConfig parameters:**
- `stream_id`: The TRUF Network stream identifier
- `name`: Human-readable name for logging
- `bounds_pct`: Rewards-eligible bounds as percentage from mid (e.g., 0.10 = ±10%)
- `min_order_size`: Minimum order size in shares (default: 100)
- `enabled`: Set to `False` to disable a stream (default: `True`)

## Pricing Logic

### Equal-Weighted Pricing

Places orders at a fixed percentage between best bid/ask and bounds:

```
bid_price = best_bid - alpha * (best_bid - lower_bound)
ask_price = best_ask + alpha * (upper_bound - best_ask)
```

### Volume-Weighted Pricing

Uses VWAP anchors instead of best bid/ask:

```
bid_vwap = volume-weighted average of top X% bids
ask_vwap = volume-weighted average of top X% asks
bid_price = bid_vwap - alpha * (bid_vwap - lower_bound)
ask_price = ask_vwap + alpha * (upper_bound - ask_vwap)
```

### Dynamic Bounds

Bounds are calculated as a percentage from the current mid price:

```
lower_bound = mid_price * (1 - bounds_pct)
upper_bound = mid_price * (1 + bounds_pct)
```

## Output

Order recommendations are saved to JSON (default: `lp_bot_orders.json`):

```json
{
  "generated_at": "2024-01-23T12:00:00Z",
  "config": {
    "pricing_method": "equal",
    "alpha": 0.3,
    "default_order_amount": 100
  },
  "markets": [
    {
      "query_id": 1,
      "stream_name": "US Inflation YoY",
      "market_state": {
        "best_bid": 45,
        "best_ask": 55,
        "mid_price": 50.0
      },
      "bounds": {
        "lower": 45,
        "upper": 55
      },
      "calculated_prices": {
        "bid": 43.5,
        "ask": 56.5
      },
      "recommendations": [
        {"side": "bid", "price": 44, "amount": 100},
        {"side": "ask", "price": 57, "amount": 100}
      ]
    }
  ]
}
```

## File Structure

```
liquidity-provider-bot/
├── lp_bot/
│   ├── __init__.py         # Package exports
│   ├── bot.py              # Main bot implementation
│   ├── config.py           # Configuration classes
│   ├── main.py             # CLI entry point
│   ├── models.py           # Data models
│   ├── order_book.py       # Order book parsing
│   ├── pricing.py          # Pricing calculations
│   └── sample_data.py      # Sample order book data
├── interactive_demo.py     # Step-by-step demo
├── requirements.txt        # Python dependencies
├── .env.example            # Environment template
├── .gitignore              # Git ignore rules
└── README.md               # This file
```