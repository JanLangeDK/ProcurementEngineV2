"""`pev2 evals run <agent...>`: each agent's eval set in evals/<agent>/cases.json, run with the fixture provider in
CI and on demand against the real model (`--provider openai_compatible`). A case has inputs and the fields the output
must contain; thresholds are per set."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pev2 import config, store
from pev2.app import ready

EVALS = config.ROOT / "evals"


def _matches(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    problems = []
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, dict) and "min" in want:
            if got is None or float(got) < float(want["min"]):
                problems.append(f"{key}: {got} < {want['min']}")
        elif isinstance(want, dict) and "contains" in want:
            if want["contains"].lower() not in str(got).lower():
                problems.append(f"{key}: {str(got)[:60]!r} does not contain {want['contains']!r}")
        elif isinstance(want, list):
            if got not in want:
                problems.append(f"{key}: {got!r} not in {want}")
        elif got != want:
            problems.append(f"{key}: {got!r} != {want!r}")
    return problems


def run(agents: list[str] | None = None, provider: str | None = None) -> bool:
    from pev2.agents import AGENTS
    from pev2.gateway import run_agent

    if provider:
        os.environ["PEV2_LLM_PROVIDER"] = provider
    os.environ.setdefault("PEV2_CLOCK", "2026-10-20T08:00:00Z")
    os.environ.setdefault("PEV2_DB", str(config.ROOT / "data" / "runtime" / "evals.db"))
    store.init()
    store.reset()
    ready()
    chosen = agents or [p.name for p in sorted(EVALS.iterdir()) if p.is_dir()]
    all_ok = True
    for name in chosen:
        path = EVALS / name / "cases.json"
        if not path.exists():
            print(f"{name}: no eval set")
            continue
        spec = json.loads(Path(path).read_text())
        agent_id = spec["agent"]
        assert agent_id in AGENTS, agent_id
        passed = 0
        for case in spec["cases"]:
            try:
                out = run_agent(agent_id, case["inputs"], None, case.get("objective", ""))
                problems = _matches(case["expect"], out)
            except Exception as exc:  # noqa: BLE001 - an eval failure is a result, not a crash
                problems = [f"error: {exc}"]
            status = "PASS" if not problems else "FAIL"
            passed += status == "PASS"
            print(f"  {status} {name}/{case['name']}" + (f" — {'; '.join(problems)}" if problems else ""))
        rate = passed / max(len(spec["cases"]), 1)
        ok = rate >= float(spec.get("threshold", 1.0))
        all_ok &= ok
        print(
            f"{name}: {passed}/{len(spec['cases'])} ({rate:.0%}) threshold {spec.get('threshold', 1.0):.0%} → {'PASS' if ok else 'FAIL'}"
        )
    return all_ok
