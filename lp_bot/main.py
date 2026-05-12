#!/usr/bin/env python3
"""
Main entry point for the Liquidity Provider Bot.

Usage:
    # Using environment variables:
    export TRUF_NODE_URL="https://gateway.testnet.truf.network"
    export TRUF_API_TOKEN="your-private-key"
    python -m lp_bot.main --query-ids 38,39,40 --dry-run

    # Auto-discover active markets:
    python -m lp_bot.main --discover-markets --dry-run

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
    # Provide liquidity to specific markets (dry run):
    python -m lp_bot.main --query-ids 38,39,40 --dry-run

    # Auto-discover active markets:
    python -m lp_bot.main --discover-markets --dry-run

    # Run with custom alpha (risk tolerance):
    python -m lp_bot.main --query-ids 38 --alpha 0.5

    # Run with volume-weighted pricing and time decay:
    python -m lp_bot.main --query-ids 38 --method volume --time-decay --half-life 30

    # Run in debug mode:
    python -m lp_bot.main --query-ids 38 --debug
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
    parser.add_argument(
        "--query-ids",
        type=str,
        default=None,
        help="Comma-separated list of market query IDs to provide liquidity for (e.g. 38,39,40)",
    )
    parser.add_argument(
        "--discover-markets",
        action="store_true",
        help="Auto-discover active (non-settled) markets from the network",
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
    """Setup graceful shutdown on SIGINT/SIGTERM.

    Cooperative: the handler only flips `bot.running` to False. The main
    loop notices on its next iteration boundary (or between markets via
    the in-cycle check) and exits cleanly. Cleanup runs from main()'s
    `finally: bot.stop()`. Calling `bot.stop()` directly from a signal
    handler is brittle: long cancel loops can be interrupted mid-RPC,
    `sys.exit(0)` may never fire if a cancel raises, and re-entrancy on
    SIGTERM-during-cancel can leave the supervisor waiting on a SIGKILL.
    """

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, requesting shutdown...")
        bot.running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def register_example_markets(bot) -> None:
    """
    Register example markets for demonstration.

    NOTE: This fallback registers no markets by default.
    Use --query-ids or --discover-markets to specify markets.
    """
    logger.warning(
        "No markets specified. Use --query-ids or --discover-markets to target markets.\n"
        "  Example: python -m lp_bot.main --query-ids 38,39,40 --dry-run\n"
        "  Example: python -m lp_bot.main --discover-markets --dry-run"
    )


def register_query_ids(bot, query_ids: list[int]) -> None:
    """
    Register explicit query IDs with default stream config.

    Args:
        bot: LiquidityProviderBot instance
        query_ids: List of query IDs to register
    """
    for qid in query_ids:
        stream_config = StreamConfig(
            stream_id="",  # Not needed for order placement — SDK uses query_id
            name=f"Market #{qid}",
            bounds_pct=0.10,
            min_order_size=1,  # Let --order-amount control the size
        )
        bot.register_market(qid, stream_config)


def register_discovered_markets(bot) -> None:
    """
    Auto-discover active markets from the network and register them.

    Args:
        bot: LiquidityProviderBot instance
    """
    discovered = bot.discover_markets()
    if not discovered:
        logger.warning("No active markets discovered on the network")
        return
    logger.info(f"Discovered {len(discovered)} markets")
    for m in discovered:
        stream_config = StreamConfig(
            stream_id="",
            name=f"Market #{m['id']}",
            bounds_pct=0.10,
            min_order_size=1,
        )
        bot.register_market(m["id"], stream_config, settle_time=m.get("settle_time"))


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

    # Register markets based on CLI args
    if args.query_ids:
        try:
            query_ids = [int(x.strip()) for x in args.query_ids.split(",")]
        except ValueError:
            logger.error(f"Invalid --query-ids format: {args.query_ids!r}. Expected comma-separated integers.")
            sys.exit(1)
        register_query_ids(bot, query_ids)
    elif args.discover_markets:
        register_discovered_markets(bot)
    else:
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
