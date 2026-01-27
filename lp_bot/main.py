#!/usr/bin/env python3
"""
Main entry point for the Liquidity Provider Bot.

Usage:
    # Using environment variables:
    export TRUF_NODE_URL="https://gateway.mainnet.truf.network"
    export TRUF_API_TOKEN="your-api-token"
    export LP_BOT_ALPHA="0.30"
    export LP_BOT_PRICING_METHOD="equal"  # or "volume"
    python -m lp_bot.main

    # Or run directly with configuration in code.
"""

import argparse
import logging
import signal
import sys
from typing import Optional

from .config import Config, StreamConfig, PricingMethod, load_config_from_env


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="TRUF Network Liquidity Provider Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run with default configuration from environment:
    python -m lp_bot.main

    # Run with custom alpha (risk tolerance):
    python -m lp_bot.main --alpha 0.5

    # Run with volume-weighted pricing and time decay:
    python -m lp_bot.main --method volume --time-decay --half-life 30

    # Run in debug mode:
    python -m lp_bot.main --debug
        """,
    )

    parser.add_argument(
        "--alpha",
        type=float,
        default=None,
        help="Risk tolerance (0=aggressive, 1=passive). Default: 0.30",
    )
    parser.add_argument(
        "--method",
        choices=["equal", "volume"],
        default=None,
        help="Pricing method: 'equal' or 'volume'. Default: equal",
    )
    parser.add_argument(
        "--time-decay",
        action="store_true",
        help="Enable time decay for volume-weighted pricing",
    )
    parser.add_argument(
        "--half-life",
        type=float,
        default=60.0,
        help="Half-life in seconds for time decay. Default: 60",
    )
    parser.add_argument(
        "--target-depth",
        type=float,
        default=0.30,
        help="Target depth %% for VWAP calculation. Default: 0.30",
    )
    parser.add_argument(
        "--order-amount",
        type=int,
        default=None,
        help="Default order size in shares. Default: 100",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Check interval in seconds. Default: 5.0",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="lp_bot_orders.json",
        help="Output JSON file for order recommendations. Default: lp_bot_orders.json",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without placing actual orders (simulation mode)",
    )
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="Generate a new Ethereum private key and exit",
    )
    parser.add_argument(
        "--sample-data",
        action="store_true",
        help="Use sample order book data instead of fetching from network",
    )

    return parser.parse_args()


def generate_key() -> None:
    """Generate a new Ethereum private key."""
    try:
        from eth_account import Account
        acct = Account.create()
        # Ensure key is zero-padded to 64 hex chars (32 bytes)
        private_key = acct.key.hex()
        if private_key.startswith("0x"):
            private_key = private_key[2:]
        # Pad with leading zeros if needed
        private_key = private_key.zfill(64)
        print("Generated new Ethereum account:")
        print(f"  Address:     {acct.address}")
        print(f"  Private Key: {private_key}")
        print()
        print("To use this key, run:")
        print(f"  export TRUF_API_TOKEN='{private_key}'")
    except ImportError:
        print("eth_account not installed. Install with:")
        print("  pip install eth-account")
        print()
        print("Or generate manually:")
        import secrets
        key = secrets.token_hex(32)
        print(f"  Private Key: {key}")
        print()
        print("To use this key, run:")
        print(f"  export TRUF_API_TOKEN='{key}'")


def create_config_from_args(args: argparse.Namespace) -> Config:
    """Create configuration from command line arguments and environment."""
    config = load_config_from_env()

    # Override with command line arguments
    if args.alpha is not None:
        config.alpha = args.alpha

    if args.method is not None:
        config.pricing_method = (
            PricingMethod.VOLUME_WEIGHTED
            if args.method == "volume"
            else PricingMethod.EQUAL_WEIGHTED
        )

    if args.time_decay:
        config.apply_time_decay = True

    if args.half_life != 60.0:
        config.half_life_seconds = args.half_life

    if args.target_depth != 0.30:
        config.target_depth_pct = args.target_depth

    if args.order_amount is not None:
        config.default_order_amount = args.order_amount

    config.check_interval_seconds = args.interval
    config.use_sample_data = args.sample_data

    return config


def setup_signal_handlers(bot) -> None:
    """Setup graceful shutdown on SIGINT/SIGTERM."""

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def register_example_markets(bot) -> None:
    """
    Register example markets for demonstration.

    In production, you would:
    1. Fetch available markets from the network
    2. Match stream_ids to query_ids
    3. Register markets with appropriate bounds
    """
    # Example: Register a market with query_id=1 using US Inflation config
    # The query_id needs to be looked up based on the stream/market
    example_markets = [
        (1, bot.config.streams[0]),  # query_id=1 -> US Inflation YoY
    ]

    for query_id, stream_config in example_markets:
        if stream_config.enabled:
            bot.register_market(query_id, stream_config)


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Handle --generate-key
    if args.generate_key:
        generate_key()
        return

    # Configure logging level
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("lp_bot").setLevel(logging.DEBUG)

    # Create configuration
    config = create_config_from_args(args)

    logger.info("=" * 60)
    logger.info("TRUF Network Liquidity Provider Bot")
    logger.info("=" * 60)
    logger.info(f"Node URL: {config.node_url}")
    logger.info(f"Pricing Method: {config.pricing_method.value}")
    logger.info(f"Alpha (risk tolerance): {config.alpha}")
    logger.info(f"Order Amount: {config.default_order_amount}")
    logger.info(f"Check Interval: {config.check_interval_seconds}s")

    if config.pricing_method == PricingMethod.VOLUME_WEIGHTED:
        logger.info(f"Target Depth: {config.target_depth_pct * 100}%")
        logger.info(f"Time Decay: {config.apply_time_decay}")
        if config.apply_time_decay:
            logger.info(f"Half-life: {config.half_life_seconds}s")

    logger.info("=" * 60)

    # Validate token - SDK requires private key even for read operations
    # (unless using sample data)
    if not config.api_token and not config.use_sample_data:
        logger.error(
            "No API token (private key) provided.\n"
            "The TRUF Network SDK requires a private key for initialization,\n"
            "even for read-only operations.\n\n"
            "Set TRUF_API_TOKEN environment variable:\n"
            "  export TRUF_API_TOKEN='your-private-key'\n\n"
            "Generate a new key with:\n"
            "  python -m lp_bot.main --generate-key\n\n"
            "Or use sample data for testing:\n"
            "  python -m lp_bot.main --sample-data --dry-run"
        )
        sys.exit(1)

    if config.use_sample_data:
        logger.info("SAMPLE DATA MODE - Using sample order book data instead of network")

    # Check for dry-run mode (has token but don't place orders)
    read_only = args.dry_run

    if read_only:
        logger.info("DRY RUN MODE - No actual orders will be placed")
        logger.info(f"Order recommendations will be written to: {args.output}")

    # Import bot here to avoid loading Go bindings until needed
    from .bot import LiquidityProviderBot

    # Create bot (always write JSON output, useful for monitoring)
    bot = LiquidityProviderBot(
        config,
        read_only=read_only,
        output_file=args.output,
    )

    # Setup signal handlers for graceful shutdown
    setup_signal_handlers(bot)

    # Register markets
    # NOTE: In production, you would dynamically discover markets
    # and map stream_ids to query_ids
    register_example_markets(bot)

    if not bot.markets:
        logger.warning("No markets registered. Add markets to start providing liquidity.")
        logger.info("Example: bot.register_market(query_id=1, stream_config=config.streams[0])")
        return

    # Start the bot
    logger.info("Starting bot...")
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        bot.stop()


if __name__ == "__main__":
    main()
