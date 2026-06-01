"""Tests for the LP bot's periodic on-chain orphan reconcile path.

Targets `_periodic_reconcile_against_chain`, the new opt-in method
that detects two directions of drift between local tracked state and
the on-chain order book:
  - tracked-but-not-on-chain  -> untrack locally
  - on-chain-but-not-tracked  -> CANCEL the orphan

Same shape as the MM bot's test_reconcile.py but adapted for the LP's
context shape and order-tracking semantics (no level_idx, plus split-
mint two-leg ASKs).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eth_account import Account as _Account

from lp_bot.bot import LiquidityProviderBot
from lp_bot.order_state import TrackedOrder


# A deterministic 64-char hex private key + the address it derives to.
TEST_PRIVATE_KEY = "1" * 64
TEST_WALLET = _Account.from_key("0x" + TEST_PRIVATE_KEY).address.lower()


def _book_entry(wallet: str, price: int, amount: int = 10) -> dict:
    return {"wallet_address": wallet, "price": price, "amount": amount}


def _tracked(qid: int, outcome: bool, is_buy: bool, price: int,
             is_inventory_backed: bool = False) -> TrackedOrder:
    return TrackedOrder(
        query_id=qid, outcome=outcome, is_buy=is_buy, price=price,
        amount=10, created_at=0.0, order_id="",
        is_inventory_backed=is_inventory_backed,
    )


class _NullBatch:
    """Stand-in for OrderStateManager.batch()'s context manager."""
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _bot_mock(tracked_orders: list[TrackedOrder],
              order_books: dict[tuple[int, bool], list[dict]],
              use_sample_data: bool = False) -> MagicMock:
    bot = MagicMock()
    bot.config.use_sample_data = use_sample_data
    bot.config.api_token = TEST_PRIVATE_KEY
    qids = sorted({qid for qid, _ in order_books.keys()})
    bot.markets = {qid: MagicMock() for qid in qids}

    state = MagicMock()
    state.batch.return_value = _NullBatch()
    by_market: dict[int, list[TrackedOrder]] = {}
    for t in tracked_orders:
        by_market.setdefault(t.query_id, []).append(t)
    state.get_market_orders.side_effect = lambda qid: by_market.get(qid, [])
    bot._order_state = state

    def _get_ob(qid: int, outcome: bool) -> list[dict]:
        return order_books.get((qid, outcome), [])
    bot.client.get_order_book.side_effect = _get_ob

    return bot


def test_reconcile_cancels_chain_order_not_in_local_state():
    """ORPHAN-PRESENT: chain has a bid the bot does not track => cancel."""
    qid = 100
    bot = _bot_mock(
        tracked_orders=[],
        order_books={
            (qid, True): [_book_entry(TEST_WALLET, -50)],
            (qid, False): [],
        },
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_called_once_with(
        query_id=qid, outcome=True, price=-50, wait=False,
    )


def test_reconcile_skips_tracked_bid():
    qid = 100
    bot = _bot_mock(
        tracked_orders=[_tracked(qid, outcome=True, is_buy=True, price=50)],
        order_books={
            (qid, True): [_book_entry(TEST_WALLET, -50)],
            (qid, False): [],
        },
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()


def test_reconcile_skips_tracked_inventory_backed_ask():
    qid = 100
    bot = _bot_mock(
        tracked_orders=[_tracked(qid, outcome=True, is_buy=False, price=60,
                                  is_inventory_backed=True)],
        order_books={
            (qid, True): [_book_entry(TEST_WALLET, 60)],
            (qid, False): [],
        },
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()


def test_reconcile_split_mint_ask_both_legs_claimed():
    """For a split-mint ASK at price 60 on outcome=True, the mirror
    leg lives at 100-60=40 on outcome=False. Both legs must be claimed
    by the same tracked entry so NEITHER is treated as an orphan."""
    qid = 100
    bot = _bot_mock(
        tracked_orders=[_tracked(qid, outcome=True, is_buy=False, price=60,
                                  is_inventory_backed=False)],
        order_books={
            (qid, True): [_book_entry(TEST_WALLET, 60)],
            (qid, False): [_book_entry(TEST_WALLET, 40)],
        },
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()


def test_reconcile_filters_other_wallets():
    qid = 100
    other = "0xabc" + "1" * 37
    bot = _bot_mock(
        tracked_orders=[],
        order_books={
            (qid, True): [_book_entry(other, -50)],
            (qid, False): [],
        },
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()


def test_reconcile_aborts_when_wallet_derivation_fails():
    bot = _bot_mock(
        tracked_orders=[],
        order_books={(100, True): [_book_entry(TEST_WALLET, -50)]},
    )
    bot.config.api_token = "not-a-hex-key"

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()
    bot.client.get_order_book.assert_not_called()


def test_reconcile_skipped_in_sample_data_mode():
    bot = _bot_mock(
        tracked_orders=[],
        order_books={(100, True): [_book_entry(TEST_WALLET, -50)]},
        use_sample_data=True,
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()
    bot.client.get_order_book.assert_not_called()


def test_reconcile_cap_caps_first_pass_at_20():
    qid = 100
    bot = _bot_mock(
        tracked_orders=[],
        order_books={
            (qid, True): [_book_entry(TEST_WALLET, -p) for p in range(1, 51)],
            (qid, False): [],
        },
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    assert bot.client.cancel_order.call_count == 20


def test_reconcile_continues_on_get_order_book_rpc_failure():
    """A gateway RPC failure on get_order_book for one (qid, outcome)
    must NOT abort the whole pass. The catch-and-continue contract
    keeps reconcile from going dark on a single transient flap."""
    qid = 100
    bot = _bot_mock(
        tracked_orders=[],
        order_books={(qid, True): [], (qid, False): []},
    )
    bot.client.get_order_book.side_effect = RuntimeError("gateway 503")

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot.client.cancel_order.assert_not_called()


def test_reconcile_untracks_stale_local_entries():
    qid = 100
    bot = _bot_mock(
        tracked_orders=[_tracked(qid, outcome=True, is_buy=True, price=50)],
        order_books={(qid, True): [], (qid, False): []},
    )

    LiquidityProviderBot._periodic_reconcile_against_chain(bot)

    bot._order_state.untrack_order.assert_called_once_with(qid, True, True, 50)
    bot.client.cancel_order.assert_not_called()
