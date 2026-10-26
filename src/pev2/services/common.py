"""What every deterministic service gets: a tool caller bound to its component id, role and instance."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pev2.tools import registry

CENT = Decimal("0.01")
DKK_PER_EUR = Decimal("7.45")  # fixed conversion for limits stated in EUR (section 6); a real system reads the rate


def money(value: Decimal | str | int) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def to_eur(amount: Decimal, currency: str) -> Decimal:
    return money(amount) if currency == "EUR" else money(Decimal(str(amount)) / DKK_PER_EUR)


@dataclass
class Tools:
    caller: str
    role: str
    instance_id: str | None = None

    def __call__(
        self, tool: str, idempotency_key: str | None = None, approval_ref: str | None = None, **args: Any
    ) -> dict[str, Any]:
        return registry.call(
            tool,
            args,
            caller=self.caller,
            instance_id=self.instance_id,
            idempotency_key=idempotency_key,
            role=self.role,
            approval_ref=approval_ref,
        )
