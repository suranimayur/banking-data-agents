"""Environment bootstrap.

Verifies the toolchain, materialises ``.env`` from the template, and reports
exactly what is missing. A developer (or a CI runner) should be able to go from
a fresh clone to a passing test suite with one command.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from banking_data_agents.config import get_settings

MIN_PYTHON = (3, 12)
MAX_PYTHON_EXCLUSIVE = (3, 13)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _check(name: str, ok: bool, detail: str, required: bool = True) -> bool:
    mark = "OK  " if ok else ("FAIL" if required else "WARN")
    stream = sys.stdout if ok or not required else sys.stderr
    print(f"  [{mark}] {name:<14} {detail}", file=stream)
    return ok or not required


def check_toolchain() -> bool:
    """Verify runtime prerequisites. Returns True when all required ones pass."""
    print("[bootstrap] toolchain")
    all_ok = True

    version = sys.version_info
    py_ok = MIN_PYTHON <= (version.major, version.minor) < MAX_PYTHON_EXCLUSIVE
    all_ok &= _check(
        "python",
        py_ok,
        f"{version.major}.{version.minor}.{version.micro} (need >={MIN_PYTHON[0]}.{MIN_PYTHON[1]},<{MAX_PYTHON_EXCLUSIVE[1]})",
    )

    uv = shutil.which("uv")
    all_ok &= _check("uv", uv is not None, uv or "not found — https://docs.astral.sh/uv/")

    from banking_data_agents.floci import docker_available, is_running

    docker_bin = shutil.which("docker")
    if docker_bin is None:
        all_ok &= _check("docker", False, "not found — needed for Floci", required=False)
    else:
        daemon = docker_available()
        all_ok &= _check(
            "docker",
            daemon,
            "daemon running" if daemon else "binary found but the daemon is NOT running — start Docker Desktop",
            required=False,
        )

    floci = shutil.which("floci")
    all_ok &= _check("floci", floci is not None, floci or "not found — https://floci.io (optional if using docker)")

    running = is_running()
    all_ok &= _check(
        "emulator",
        running,
        "Floci answering on :4566" if running else "not running — start it with `make floci-up`",
        required=False,
    )

    settings = get_settings()
    print("[bootstrap] configuration")
    print(f"  [INFO] env            {settings.env}")
    print(f"  [INFO] llm provider   {settings.llm_provider}")
    print(f"  [INFO] aws endpoint   {settings.aws_endpoint_url or '(real AWS)'}")
    return bool(all_ok)


def ensure_env_file() -> bool:
    """Create ``.env`` from ``.env.example`` when absent. Never overwrites."""
    root = _project_root()
    env = root / ".env"
    example = root / ".env.example"

    if env.exists():
        print(f"[bootstrap] .env exists ({env}) - left untouched")
        return True
    if not example.exists():
        print("[bootstrap] .env.example missing; cannot create .env", file=sys.stderr)
        return False

    env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print("[bootstrap] created .env from .env.example")
    return True


def run_bootstrap(*, check_only: bool = False) -> int:
    """Full bootstrap. Returns a process exit code."""
    print("=" * 72)
    print("BDA bootstrap")
    print("=" * 72)

    ok = check_toolchain()

    if not check_only:
        ok &= ensure_env_file()

    print()
    if ok:
        print("[bootstrap] ready. Next:  make floci-up && make test")
        return 0
    print("[bootstrap] prerequisites missing - see FAIL lines above.", file=sys.stderr)
    return 1
