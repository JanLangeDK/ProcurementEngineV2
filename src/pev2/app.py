"""Import side: tools, services (allow-lists), agents and workflows registered; the runtime ready."""

import pev2.services  # noqa: F401 - allow-lists
import pev2.workflows  # noqa: F401 - workflow registry
from pev2 import store
from pev2.engine import runtime


def ready() -> None:
    store.init()
    if store.first("select count(*) as n from sap_supplier")["n"] == 0:
        store.load_fixtures()


__all__ = ["ready", "runtime"]
