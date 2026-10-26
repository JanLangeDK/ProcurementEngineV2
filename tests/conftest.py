from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ["PEV2_CLOCK"] = "2026-10-20T08:00:00Z"
os.environ["PEV2_LLM_PROVIDER"] = "fixture"
os.environ["PEV2_RETRY_SLEEP"] = "0"


@pytest.fixture()
def world(tmp_path) -> Iterator[None]:
    """A fresh SQLite world per test: fixtures loaded, runtime empty."""
    os.environ["PEV2_DB"] = str(tmp_path / "pev2.db")
    from pev2 import store
    from pev2.app import ready
    from pev2.tools import registry

    store.init()
    store.reset()
    ready()
    registry.outages = set()
    yield
    registry.outages = set()


@pytest.fixture()
def rt(world):
    from pev2.app import runtime

    return runtime


def approve_all(runtime, by: str = "test") -> int:
    n = 0
    for ap in runtime.approvals():
        runtime.decide_approval(ap["id"], "approved", by)
        n += 1
    return n
