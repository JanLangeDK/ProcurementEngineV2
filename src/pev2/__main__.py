"""CLI: pev2 ui | api | demo | reset | smoke | evals."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="pev2", description="Procurement Engine V2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ui", help="the Gradio window on :7860").add_argument("--port", type=int, default=7860)
    sub.add_parser("api", help="the HTTP API on :8000").add_argument("--port", type=int, default=8000)
    sub.add_parser("reset", help="empty the runtime and reload the mocks")
    sub.add_parser("demo", help="reset and play the four stories against the mocks, unattended")
    sm = sub.add_parser("smoke", help="foundation smoke test: one workflow, one restart")
    sm.add_argument("--engine", action="store_true")
    ev = sub.add_parser("evals", help="run the agent eval sets")
    ev.add_argument("agents", nargs="*")
    ev.add_argument("--provider", default=None)
    args = parser.parse_args()

    if args.cmd == "ui":
        from pev2.ui.app import main as ui_main

        ui_main(args.port)
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run("pev2.api:app", host="0.0.0.0", port=args.port)
    elif args.cmd == "reset":
        from pev2 import store

        store.init()
        store.reset()
        print("reset done")
    elif args.cmd == "demo":
        from pev2.demo import play

        sys.exit(0 if play() else 1)
    elif args.cmd == "smoke":
        from pev2.demo import smoke

        sys.exit(0 if smoke() else 1)
    elif args.cmd == "evals":
        from pev2.evals import run

        sys.exit(0 if run(args.agents, args.provider) else 1)


if __name__ == "__main__":
    main()
