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

from .config import (
    Config,
    StreamConfig,
    MarketEntry,
    PricingMethod,
    load_config_from_env,
    load_config_from_yaml,
)


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
        default=None,
        help="Half-life in seconds for time decay. Default: 60.0 (None = use YAML/config default)",
    )
    parser.add_argument(
        "--target-depth",
        type=float,
        default=None,
        help="Target depth %% for VWAP calculation. Default: 0.30 (None = use YAML/config default)",
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
        default=None,
        help="Check interval in seconds. Default: 5.0 (None = use YAML/config default)",
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
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help=(
            "Path to YAML config file. When supplied, takes precedence over "
            "--query-ids and --discover-markets for market selection, and "
            "supplies all Config fields. CLI overrides (--alpha, --debug, "
            "--dry-run, etc.) still apply on top of the YAML values."
        ),
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


def create_config_from_args(
    args: argparse.Namespace,
) -> tuple[Config, list[MarketEntry]]:
    """Create configuration from command line arguments and environment.

    Returns (config, market_entries). `market_entries` is non-empty
    only when --config is supplied AND its YAML contains a `markets:`
    list; otherwise the caller falls back to --query-ids /
    --discover-markets handling.

    Precedence:
      1. YAML file (when --config is supplied) supplies all Config
         fields and may carry market entries.
      2. Environment variables fill the Config when no --config.
      3. CLI flags layer on top of either (1) or (2).
    """
    market_entries: list[MarketEntry] = []
    if getattr(args, "config", None):
        config, market_entries = load_config_from_yaml(args.config)
    else:
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

    if args.half_life is not None:
        config.half_life_seconds = args.half_life

    if args.target_depth is not None:
        config.target_depth_pct = args.target_depth

    if args.order_amount is not None:
        config.default_order_amount = args.order_amount

    # All numeric overrides use `default=None` in argparse so that
    # passing the same value as the Config default still registers as
    # an explicit override (an operator running `--interval 5.0` to be
    # explicit will not be silently ignored).
    if args.interval is not None:
        config.check_interval_seconds = args.interval
    # `--sample-data` (action="store_true") can only set True; there is
    # no symmetric way to force False from the CLI. Operators who need
    # to override a YAML-set `use_sample_data: true` must edit the
    # YAML or unset the field there.
    if args.sample_data:
        config.use_sample_data = True

    return config, market_entries


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


def register_market_entries(bot, entries: list[MarketEntry]) -> None:
    """
    Register markets parsed from a YAML config.

    Each entry carries a query_id and a fully-populated StreamConfig
    (bounds_pct, min_order_size, outcome_mode, initial_probability,
    initial_mint_pairs, etc.). No defaults are applied here so the
    YAML stays the single source of truth for per-market knobs.

    Entries with `enabled: false` are skipped (logged at INFO) so an
    operator can keep a market in the YAML for documentation but pause
    quoting on it without deleting the entry.
    """
    for entry in entries:
        if not entry.stream.enabled:
            logger.info(
                f"Skipping market {entry.query_id} ({entry.stream.name!r}): "
                f"enabled=false in YAML"
            )
            continue
        logger.info(
            f"Registering market {entry.query_id} "
            f"(stream_id={entry.stream.stream_id!r}, "
            f"outcome_mode={entry.stream.outcome_mode!r}, "
            f"bounds_pct={entry.stream.bounds_pct})"
        )
        bot.register_market(entry.query_id, entry.stream)


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
    config, market_entries = create_config_from_args(args)

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

    # Register markets. Precedence: YAML `markets:` > --query-ids >
    # --discover-markets > the empty-list warning. YAML wins so an
    # operator running `lp_bot.main --config <path>` does not have to
    # also pass --query-ids and then risk drift between the two.
    if market_entries:
        register_market_entries(bot, market_entries)
    elif args.query_ids:
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
