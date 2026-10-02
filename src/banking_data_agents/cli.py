"""BDA command line.

Every pipeline target in the Makefile delegates here so that CI never depends on
`make` being installed. Subcommand handlers import their implementation lazily,
which keeps `--help` fast and lets the CLI remain usable while a component is
still being built.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from banking_data_agents import __version__
from banking_data_agents.aws import describe_endpoint
from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import configure_logging


def _not_implemented(feature: str) -> int:
    print(f"[bda] '{feature}' is not implemented in this build yet.", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def cmd_bootstrap(args: argparse.Namespace) -> int:
    from banking_data_agents.bootstrap import run_bootstrap

    return run_bootstrap(check_only=args.check_only)


def cmd_floci(args: argparse.Namespace) -> int:
    from banking_data_agents.floci import down, status, up

    if args.action == "up":
        return up()
    if args.action == "down":
        return down()
    return status()


def cmd_data(args: argparse.Namespace) -> int:
    from banking_data_agents.datagen.generate import generate_all

    return generate_all(seed=args.seed, scale=args.scale, verbose=True)


def cmd_pipeline(args: argparse.Namespace) -> int:
    from banking_data_agents.pipeline.runner import run_pipeline

    if args.action == "dq":
        return run_pipeline(stages=["dq"], verbose=True)
    return run_pipeline(stages=args.stages, verbose=True)


def cmd_catalog(args: argparse.Namespace) -> int:
    from banking_data_agents.catalog.cli import catalog_list, catalog_show

    if args.product:
        return catalog_show(args.product)
    return catalog_list()


def cmd_ask(args: argparse.Namespace) -> int:
    from banking_data_agents.cli_ask import ask_once

    return ask_once(args.question, show_sql=not args.no_sql, json_out=args.json)


def cmd_eval(args: argparse.Namespace) -> int:
    from banking_data_agents.evals.runner import run_evals

    return run_evals(suite=args.suite, fail_under=args.fail_under, report=not args.no_report)


def cmd_answers(args: argparse.Namespace) -> int:
    from banking_data_agents.cli_answers import answers_get, answers_list

    if args.action == "list":
        return answers_list(limit=args.limit)
    if not args.trace_id:
        print("[bda] `bda answers get` needs a trace id, e.g. `bda answers get 4f2c...`", file=sys.stderr)
        return 2
    return answers_get(args.trace_id, as_json=args.json)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("banking_data_agents.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    import subprocess
    from pathlib import Path

    app = Path(__file__).parent / "ui" / "console.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port)])


def cmd_infra(args: argparse.Namespace) -> int:
    from banking_data_agents.infra_cli import deploy, destroy, synth

    if args.action == "synth":
        return synth(env=args.env, image_tag=args.image_tag)
    if args.action == "deploy":
        return deploy(
            env=args.env,
            require_approval=not args.yes,
            image_tag=args.image_tag,
            stacks=args.stacks or None,
        )
    return destroy(env=args.env, force=args.yes)


def cmd_demo(args: argparse.Namespace) -> int:
    from banking_data_agents.demo import run_demo

    return run_demo(skip_data=args.skip_data)


def cmd_clean(args: argparse.Namespace) -> int:
    import shutil

    settings = get_settings()
    removed: list[str] = []
    for name in (".pytest_cache", ".ruff_cache", ".mypy_cache", ".coverage", "htmlcov", "cdk.out"):
        path = settings.data_dir.parent / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    if not removed:
        print("[bda] nothing to clean.")
    else:
        print(f"[bda] removed: {', '.join(removed)}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bda",
        description="Banking Data Product Agent Platform",
    )
    parser.add_argument("--version", action="version", version=f"banking_data_agents {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap", help="prepare a working environment")
    p.add_argument("--check-only", action="store_true", help="verify without writing .env")
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("floci", help="control the local AWS emulator")
    p.add_argument("action", choices=["up", "down", "status"])
    p.set_defaults(func=cmd_floci)

    p = sub.add_parser("data", help="synthetic source data")
    p.add_argument("action", choices=["generate"])
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--scale", type=float, default=1.0, help="multiplier on row counts")
    p.set_defaults(func=cmd_data)

    p = sub.add_parser("pipeline", help="run the medallion pipeline")
    p.add_argument("action", choices=["run", "dq"], nargs="?", default="run")
    p.add_argument(
        "--stages",
        nargs="*",
        default=["bronze", "silver", "gold", "publish"],
        choices=["bronze", "silver", "gold", "publish", "dq"],
    )
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("catalog", help="inspect published data products")
    p.add_argument("action", choices=["list"], nargs="?", default="list")
    p.add_argument("product", nargs="?", default=None)
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("ask", help="ask the Data Product Copilot a question")
    p.add_argument("question", help="natural language question")
    p.add_argument("--no-sql", action="store_true", help="hide the generated SQL")
    p.add_argument("--json", action="store_true", help="emit the full evidence envelope as JSON")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("answers", help="read back a retained agent answer")
    p.add_argument("action", choices=["get", "list"])
    p.add_argument("trace_id", nargs="?", default=None, help="the trace id shown by `bda ask`")
    p.add_argument("--json", action="store_true", help="emit the retained envelope as JSON")
    p.add_argument("--limit", type=int, default=20, help="how many answers `list` shows")
    p.set_defaults(func=cmd_answers)

    p = sub.add_parser("eval", help="run the agent evaluation harness")
    p.add_argument("action", choices=["run"], nargs="?", default="run")
    p.add_argument("--suite", default="deterministic")
    p.add_argument("--fail-under", type=float, default=0.0, help="exit non-zero below this score")
    p.add_argument("--no-report", action="store_true")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("serve", help="run the Copilot HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ui", help="run the analyst console")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("infra", help="CDK infrastructure")
    p.add_argument("action", choices=["synth", "deploy", "destroy"])
    p.add_argument(
        "--env",
        default="dev",
        choices=["bootstrap", "local", "dev", "staging", "prod"],
        help="'bootstrap' is the one-time per-account stack; the rest are environments",
    )
    p.add_argument("--yes", action="store_true", help="skip the CDK approval prompt")
    p.add_argument(
        "--image-tag",
        default=None,
        help="immutable image tag (git SHA) to deploy; required for non-dev environments",
    )
    p.add_argument(
        "--stacks",
        nargs="*",
        default=[],
        help="deploy only these stacks instead of every stack",
    )
    p.set_defaults(func=cmd_infra)

    p = sub.add_parser("demo", help="end-to-end demonstration")
    p.add_argument("--skip-data", action="store_true")
    p.set_defaults(func=cmd_demo)

    sub.add_parser("clean", help="remove caches").set_defaults(func=cmd_clean)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command in {"pipeline", "data", "ask", "eval", "floci", "catalog", "demo"}:
        print(f"[bda] env={settings.env} · {describe_endpoint()}", file=sys.stderr)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n[bda] interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
