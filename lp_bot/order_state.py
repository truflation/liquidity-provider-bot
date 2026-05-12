"""
Persistent order state for the LP bot.

Without this, every restart loses all in-memory order tracking. The bot
then ignores any orders it placed in the previous session — they sit on
chain unmanaged, never get cancelled or refreshed, and over time the
wallet accumulates orphans across each restart cycle.

This module persists each tracked order to a JSON file. On startup the
bot loads the file and reconciles against the on-chain order book:
orders still on chain are kept, orders missing are dropped (filled or
externally cancelled).

Ported slim from the MM bot's `order_state.py`. LP doesn't have
multi-level orders so `level_idx` is omitted; LP doesn't yet have an
inventory-backed path so `is_inventory_backed` is included for forward
compatibility with Phase 2's inventory-aware ASKs.
"""

import contextlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TrackedOrder:
    """An order placed by the bot."""

    query_id: int
    outcome: bool  # True = YES, False = NO (logical outcome being quoted)
    is_buy: bool
    price: int  # Logical price in cents (1-99). For asks this is the
    #            user-visible ASK price, not the auto-listed leg price.
    amount: int
    created_at: float  # Unix timestamp at track time.
    order_id: Optional[str] = None  # SDK order ID if available
    # True iff this ASK was placed via place_sell_order against held
    # inventory (single-leg, single cancel). False covers buys and the
    # legacy split-mint ASK path (two on-chain legs, two cancels).
    is_inventory_backed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TrackedOrder":
        # Defensive defaults for old state files written before the
        # inventory-backed flag existed.
        if "is_inventory_backed" not in data:
            data["is_inventory_backed"] = False
        return cls(**data)

    @property
    def key(self) -> str:
        """Stable unique key for this order's (market, side, price)."""
        side = "buy" if self.is_buy else "sell"
        outcome_str = "yes" if self.outcome else "no"
        return f"{self.query_id}:{outcome_str}:{side}:{self.price}"


class OrderStateManager:
    """Manages persistent order state in a JSON file.

    The bot saves after every `track_order` / `untrack_order` call so a
    crash mid-cycle does not lose visibility on orders just placed.

    Reconciliation against chain state is done by the caller via
    `reconcile_with_orderbook` (called at startup).
    """

    def __init__(self, state_file: str = "lp_bot_order_state.json") -> None:
        self.state_file = Path(state_file)
        self._orders: Dict[str, TrackedOrder] = {}
        # Batched-save support. When `_batch_depth > 0` (inside a
        # `with manager.batch():` block), track/untrack calls mark the
        # state dirty but defer the actual file write. The batch exit
        # flushes once if anything changed. Used to compress N writes
        # per cancel-all loop into a single fsync.
        self._batch_depth = 0
        self._batch_dirty = False
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_file.exists():
            logger.info(f"No existing state file at {self.state_file}")
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            for entry in data.get("orders", []):
                order = TrackedOrder.from_dict(entry)
                self._orders[order.key] = order
            logger.info(
                f"Loaded {len(self._orders)} tracked orders from {self.state_file}"
            )
        except Exception as e:
            logger.error(f"Failed to load order state from {self.state_file}: {e}")
            self._orders = {}

    @contextlib.contextmanager
    def batch(self) -> Iterator["OrderStateManager"]:
        """Suspend file writes inside this block; flush once at exit.

        Reentrant. Multiple nested `with manager.batch():` blocks
        still flush only once, on the outermost exit. If nothing
        changed inside the block, no flush is performed.

        Use this to wrap loops that call track_order or untrack_order
        many times (cancel-all, reconcile-on-startup, etc.) so the
        file is written once instead of N times.
        """
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0 and self._batch_dirty:
                self._save_state_now()
                self._batch_dirty = False

    def _save_state(self) -> None:
        """Save unless we're inside a batch.

        Inside a batch, marks state dirty and defers the actual write
        to `batch()`'s flush at exit.
        """
        if self._batch_depth > 0:
            self._batch_dirty = True
            return
        self._save_state_now()

    def _save_state_now(self) -> None:
        """Actually write the state file. Atomic via `.tmp` + replace."""
        try:
            data = {
                "orders": [order.to_dict() for order in self._orders.values()],
                "last_updated": time.time(),
            }
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            tmp.replace(self.state_file)
            logger.debug(
                f"Saved {len(self._orders)} tracked orders to {self.state_file}"
            )
        except Exception as e:
            logger.error(f"Failed to save order state: {e}")

    def track_order(
        self,
        query_id: int,
        outcome: bool,
        is_buy: bool,
        price: int,
        amount: int,
        order_id: Optional[str] = None,
        is_inventory_backed: bool = False,
    ) -> TrackedOrder:
        """Record a newly placed order and persist."""
        order = TrackedOrder(
            query_id=query_id,
            outcome=outcome,
            is_buy=is_buy,
            price=price,
            amount=amount,
            created_at=time.time(),
            order_id=order_id,
            is_inventory_backed=is_inventory_backed,
        )
        self._orders[order.key] = order
        self._save_state()
        return order

    def untrack_order(
        self,
        query_id: int,
        outcome: bool,
        is_buy: bool,
        price: int,
    ) -> bool:
        """Remove an order from tracking (cancelled, filled, or
        reconciled-stale). Returns True iff the key existed."""
        side = "buy" if is_buy else "sell"
        outcome_str = "yes" if outcome else "no"
        key = f"{query_id}:{outcome_str}:{side}:{price}"
        if key in self._orders:
            del self._orders[key]
            self._save_state()
            return True
        return False

    def get_market_orders(
        self, query_id: int, outcome: Optional[bool] = None
    ) -> List[TrackedOrder]:
        """Return tracked orders for a market (optionally filtered by
        outcome)."""
        out: List[TrackedOrder] = []
        for o in self._orders.values():
            if o.query_id != query_id:
                continue
            if outcome is not None and o.outcome != outcome:
                continue
            out.append(o)
        return out

    def get_all_orders(self) -> List[TrackedOrder]:
        return list(self._orders.values())

    def reconcile_with_orderbook(
        self,
        query_id: int,
        outcome: bool,
        orderbook_prices: Dict[int, int],  # price -> amount, our wallet only
    ) -> Dict[str, List[TrackedOrder]]:
        """Reconcile tracked orders for (query_id, outcome) with chain.

        For ASK orders the bot tracks the LOGICAL price (not the auto-
        listed leg's price). Cancellation on chain affects both legs of
        the split-mint pair, so an order is considered active iff EITHER
        the logical-side or the opposite-side leg is present at the
        right price. The caller is responsible for passing in the
        orderbook entries that match this outcome AND its mirror leg
        (i.e. caller must merge `get_order_book(qid, True)` and
        `get_order_book(qid, False)` results before calling this).

        Returns a dict with 'active' (still on chain) and 'stale'
        (missing — filled or externally cancelled). Stale orders are
        untracked from local state.
        """
        result: Dict[str, List[TrackedOrder]] = {"active": [], "stale": []}
        for order in self.get_market_orders(query_id, outcome):
            if order.price in orderbook_prices:
                result["active"].append(order)
            else:
                result["stale"].append(order)
                self.untrack_order(
                    order.query_id, order.outcome, order.is_buy, order.price
                )
        return result
