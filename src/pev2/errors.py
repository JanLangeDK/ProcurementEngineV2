"""Error types the runtime distinguishes. Section 7 of docs/architecture.md decides what happens to each."""


class TransientError(Exception):
    """An integration hiccup: retried per the tool's retry policy, then the instance is parked."""


class BusinessException(Exception):
    """A modelled exception (missing PO, duplicate invoice, unknown material...). The workflow either
    follows its modelled exception path or the instance is parked for a human."""

    def __init__(self, kind: str, detail: str = "", data: dict[str, object] | None = None) -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail
        self.data = data or {}


class LimitError(Exception):
    """A refusal by the authority matrix: the caller may not do this. Never retried."""


class GateFailed(Exception):
    """A validation gate rejected; the instance goes to human review."""

    def __init__(self, gate: str, reasons: list[str]) -> None:
        super().__init__(f"{gate}: " + "; ".join(reasons))
        self.gate = gate
        self.reasons = reasons
