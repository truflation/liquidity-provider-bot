"""
Per-market inventory tracking for the LP bot.

When the bot lists an ASK, it currently mints fresh YES+NO pairs from
collateral and lists one leg per cycle (`place_split_limit_order` +
`place_sell_order`). On every refresh that pattern locks ~$1 of USDC
per pair into the listed leg until the order is filled or cancelled.

This module lets the bot prefer a single-leg `place_sell_order` against
shares it already holds (from a fill on the bid side, a settled prior
listing, or a pre-mint cold-start). It tracks:

- `yes_shares` / `no_shares`: shares currently held (`price=0` on chain).
- `chain_listed_yes_sells` / `chain_listed_no_sells`: shares currently
  listed as on-chain sells (`price > 0`). Counted in `paired_inventory`
  because the underlying collateral is still owned: cancelling a listed
  sell returns the share to held.
- `reserved_yes_sells` / `reserved_no_sells`: bot-side intent to back an
  ASK with already-held shares. Reserved units are subtracted from
  `available_for_sell` so two ASKs placed in the same cycle don't both
  claim the same share.

The refresh path overwrites held + listed counts from chain truth; the
reservation counters are bot-side state and are NOT touched there.

Ported from the MM bot's `pricing/inventory.py`, with Avellaneda-Stoikov-
specific accounting (target_pct, get_inventory_ratio, get_market_value,
get_net_exposure) stripped since LP uses bounds-based pricing.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass
class MarketInventory:
    """Inventory tracking for a single market."""

    query_id: int

    # Held shares (chain `price=0` entries).
    yes_shares: int = 0
    no_shares: int = 0

    # Bot-side reservations against held shares for upcoming ASKs.
    # Subtracted from available_for_sell so two ASKs in the same cycle
    # don't double-claim the same share. Cleared by release_pair() when
    # the cancel completes or the fill is detected.
    reserved_yes_sells: int = 0
    reserved_no_sells: int = 0

    # Shares CURRENTLY LISTED on chain as sell orders (any source: the
    # bot's own split-mint asks, inventory-backed asks, or orphan auto-
    # listed legs from a partially-completed pre-mint). Populated each
    # refresh from `get_user_positions` entries with `price > 0`.
    # paired_inventory() counts these because the underlying collateral
    # is still owned (a listed sell at 99c is a held share with a
    # pending sale; cancelling returns it to held).
    chain_listed_yes_sells: int = 0
    chain_listed_no_sells: int = 0

    def reserve_pair(self, outcome: bool, n: int) -> None:
        """Reserve n shares for an inventory-backed ASK on `outcome`.

        outcome=True debits YES, outcome=False debits NO. No-op when n<=0.
        """
        if n <= 0:
            return
        if outcome:
            self.reserved_yes_sells += n
        else:
            self.reserved_no_sells += n

    def release_pair(self, outcome: bool, n: int) -> None:
        """Return n reserved shares to available (cancel or fill).

        Clamped at zero so a stray double-release doesn't break the
        invariant.
        """
        if n <= 0:
            return
        if outcome:
            self.reserved_yes_sells = max(0, self.reserved_yes_sells - n)
        else:
            self.reserved_no_sells = max(0, self.reserved_no_sells - n)

    def available_for_sell(self, outcome: bool) -> int:
        """Shares available to back a new inventory-backed ASK.

        Equals held minus reserved, clamped at zero so an inventory
        drift (e.g. partial fill between refresh cycles) does not
        produce nonsense.
        """
        held = self.yes_shares if outcome else self.no_shares
        reserved = self.reserved_yes_sells if outcome else self.reserved_no_sells
        return max(0, held - reserved)

    def paired_inventory(self) -> int:
        """Number of fully-paired (1 YES + 1 NO) units we own.

        Counts held AND listed on both sides — listed sells are still
        owned because cancelling returns them to held. Used by future
        pre-mint deficit math: `target - paired_inventory()` is the
        number of fresh pairs to mint at cold-start. Without counting
        listed, a crash between split-mint broadcast and the auto-leg
        cancel would cause the next pre-mint to double-mint.
        """
        total_yes = self.yes_shares + self.chain_listed_yes_sells
        total_no = self.no_shares + self.chain_listed_no_sells
        return min(total_yes, total_no)

    def update_from_positions(
        self,
        yes_shares: int,
        no_shares: int,
        chain_listed_yes_sells: int = 0,
        chain_listed_no_sells: int = 0,
    ) -> None:
        """Overwrite held + listed counts from chain truth.

        Reservation counters (reserved_*_sells) are bot-side state and
        are NOT touched here, because a fresh chain snapshot does not
        invalidate the bot's in-flight intent to list shares.
        """
        self.yes_shares = yes_shares
        self.no_shares = no_shares
        self.chain_listed_yes_sells = chain_listed_yes_sells
        self.chain_listed_no_sells = chain_listed_no_sells


class InventoryManager:
    """Manages per-market inventory for the LP bot.

    Maintains a `MarketInventory` per query_id and provides a single
    entry point (`update_from_user_positions`) that consumes the SDK's
    `get_user_positions` output.
    """

    def __init__(self) -> None:
        self._inventories: Dict[int, MarketInventory] = {}

    def get_market_inventory(self, query_id: int) -> MarketInventory:
        """Return the inventory tracker for `query_id`, creating one if
        missing."""
        if query_id not in self._inventories:
            self._inventories[query_id] = MarketInventory(query_id=query_id)
        return self._inventories[query_id]

    def update_from_user_positions(self, positions: List[dict]) -> None:
        """Update all per-market inventories from the SDK's positions list.

        Each position dict has `query_id`, `outcome`, `price`, `amount`.
        Signed-price convention from `get_user_positions`:
          - price == 0  -> holding (counts toward yes_shares / no_shares)
          - price <  0  -> open buy (LP doesn't track collateral here)
          - price >  0  -> open sell (counts toward chain_listed_*)
        """
        by_market: Dict[int, Dict[str, int]] = {}

        for pos in positions:
            query_id = pos.get("query_id")
            if query_id is None:
                continue
            outcome = pos.get("outcome", True)
            price = pos.get("price", 0)
            amount = pos.get("amount", 0)

            if query_id not in by_market:
                by_market[query_id] = {
                    "yes_shares": 0,
                    "no_shares": 0,
                    "yes_listed_sells": 0,
                    "no_listed_sells": 0,
                }

            if price == 0:
                if outcome:
                    by_market[query_id]["yes_shares"] += amount
                else:
                    by_market[query_id]["no_shares"] += amount
            elif price > 0:
                if outcome:
                    by_market[query_id]["yes_listed_sells"] += amount
                else:
                    by_market[query_id]["no_listed_sells"] += amount
            # price < 0 (open bids) intentionally not tracked here. LP
            # tracks its own bids via ActiveOrder; we don't need a
            # second source of truth for them in the inventory module.

        for query_id, data in by_market.items():
            inv = self.get_market_inventory(query_id)
            inv.update_from_positions(
                yes_shares=data["yes_shares"],
                no_shares=data["no_shares"],
                chain_listed_yes_sells=data["yes_listed_sells"],
                chain_listed_no_sells=data["no_listed_sells"],
            )

        logger.debug(f"Inventory refresh: {len(by_market)} markets updated")
