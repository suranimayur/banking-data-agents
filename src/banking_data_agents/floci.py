"""Floci lifecycle management.

Floci is a free, MIT-licensed local AWS emulator exposed on a single port
(4566) that speaks the AWS wire protocol, so the platform's boto3 clients work
against it unchanged. This module starts/stops it and reports what is running.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from banking_data_agents.config import get_settings

FLOCI_CONTAINER = "bda-floci"
DEFAULT_PORT = 4566


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _compose_file() -> Path:
    return _project_root() / "compose.yaml"


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=_project_root())


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _run(["docker", "info"], check=False).returncode == 0
    except Exception:
        return False


def is_running(port: int = DEFAULT_PORT, timeout: float = 1.5) -> bool:
    """True when something answers the emulator's health endpoint."""
    for path in ("/_floci/health", "/"):
        try:
            if httpx.get(f"http://localhost:{port}{path}", timeout=timeout).status_code < 500:
                return True
        except Exception:
            continue
    return False


def wait_until_ready(port: int = DEFAULT_PORT, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_running(port):
            return True
        time.sleep(1.0)
    return False


def up(port: int = DEFAULT_PORT, *, wait: bool = True) -> int:
    """Start the emulator. Idempotent: a healthy emulator is left alone."""
    if is_running(port):
        print(f"[floci] already running on :{port}")
        return 0

    if not docker_available():
        print(
            "[floci] docker is not available.\n"
            "        Install Docker Desktop, or start Floci directly:\n"
            "            docker run -d --name bda-floci -p 4566:4566 \\\n"
            "              -v /var/run/docker.sock:/var/run/docker.sock floci/floci:latest",
            file=sys.stderr,
        )
        return 1

    (_project_root() / "data" / "floci").mkdir(parents=True, exist_ok=True)

    print(f"[floci] starting emulator on :{port} ...")
    result = _run(
        ["docker", "compose", "-f", str(_compose_file()), "up", "-d"],
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return result.returncode

    if wait and not wait_until_ready(port):
        print("[floci] container started but the health endpoint never answered.", file=sys.stderr)
        return 1

    print(f"[floci] ready at http://localhost:{port}   (console: http://localhost:{port}/_floci/ui)")
    return 0


def down(port: int = DEFAULT_PORT) -> int:
    """Stop the emulator. Keeps the persisted volume."""
    if not _compose_file().exists():
        return 0
    result = _run(["docker", "compose", "-f", str(_compose_file()), "down"], check=False)
    if result.returncode == 0:
        print("[floci] stopped.")
    else:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def status(port: int = DEFAULT_PORT) -> int:
    """Print emulator health and the BDA resources that exist inside it."""
    settings = get_settings()
    running = is_running(port)
    print(f"[floci] emulator      : {'RUNNING' if running else 'NOT RUNNING'} (http://localhost:{port})")
    print(f"[floci] configured URI: {settings.aws_endpoint_url or '(real AWS)'}")
    print(f"[floci] docker        : {'available' if docker_available() else 'NOT available'}")

    if not running:
        print("[floci] start it with: make floci-up")
        return 1

    if settings.aws_endpoint_url:
        try:
            from banking_data_agents.aws import clear_client_cache, glue, s3

            clear_client_cache()
            buckets = [b["Name"] for b in s3().list_buckets().get("Buckets", [])]
            print(f"[floci] s3 buckets    : {len(buckets)}")
            for name in sorted(buckets):
                print(f"          - {name}")
            dbs = [d["Name"] for d in glue().get_databases().get("DatabaseList", [])]
            print(f"[floci] glue databases: {len(dbs)}")
            for name in sorted(dbs):
                print(f"          - {name}")
        except Exception as exc:
            print(f"[floci] could not list resources: {exc}", file=sys.stderr)
            return 1
    return 0
