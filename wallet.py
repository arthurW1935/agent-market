"""Coins ledger + escrow state machine.

Legal escrow transitions ONLY: locked -> released, locked -> refunded.
Money is integer coins; take = bounty * 5 // 100 so net + take == bounty.
"""

from __future__ import annotations

import config
import stores
from models import EscrowState


def balance(owner: str) -> int:
    return stores.wallets.get(owner, 0)


def deposit(owner: str, amount: int) -> int:
    stores.wallets[owner] = balance(owner) + amount
    return stores.wallets[owner]


def lock_escrow(task_id: str, owner: str, amount: int) -> None:
    """Move coins wallet -> escrow. Caller must have checked the balance."""
    if task_id in stores.escrow:
        raise ValueError(f"escrow already exists for {task_id}")
    stores.wallets[owner] = balance(owner) - amount
    stores.escrow[task_id] = {"amount": amount, "state": EscrowState.locked}


def release_escrow(task_id: str, agent_id: str) -> tuple[int, int, int]:
    """locked -> released. Returns (gross, take, net)."""
    entry = stores.escrow[task_id]
    if entry["state"] != EscrowState.locked:
        raise ValueError(f"illegal escrow transition from {entry['state']}")
    gross = entry["amount"]
    take = gross * config.TAKE_RATE_PERCENT // 100
    net = gross - take
    entry["state"] = EscrowState.released
    stores.wallets[agent_id] = balance(agent_id) + net
    stores.wallets["platform"] = balance("platform") + take
    return gross, take, net


def refund_escrow(task_id: str, owner: str = "buyer") -> int:
    """locked -> refunded. Returns the refunded amount."""
    entry = stores.escrow[task_id]
    if entry["state"] != EscrowState.locked:
        raise ValueError(f"illegal escrow transition from {entry['state']}")
    amount = entry["amount"]
    entry["state"] = EscrowState.refunded
    stores.wallets[owner] = balance(owner) + amount
    return amount
