"""Shared pytest configuration.

Tests must be reproducible and free: the LLM provider is forced to the
deterministic stub, the synthetic dataset is small, and any developer ``.env``
is explicitly ignored so a local override cannot change a test result.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

# --- Force a hermetic environment before banking_data_agents.config is imported --------
os.environ["BDA_ENV_FILE"] = ".env.test-nonexistent"
os.environ["ENV"] = "local"
os.environ["LLM_PROVIDER"] = "stub"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
# Integration tests opt in by setting AWS_ENDPOINT_URL themselves.
os.environ.pop("AWS_ENDPOINT_URL", None)

# Smaller, fast synthetic dataset for the test suite.
os.environ["SYNTHETIC_CUSTOMERS"] = "120"
os.environ["SYNTHETIC_ACCOUNTS"] = "200"
os.environ["SYNTHETIC_LOANS"] = "80"
os.environ["SYNTHETIC_CARDS"] = "110"
os.environ["SYNTHETIC_MONTHS"] = "4"
# Hermetic: tests never reach for S3 unless a test opts in explicitly.
os.environ["BDA_LAKE_BACKEND"] = "local"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Filesystem root of the banking_data_agents project."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def settings():
    """Settings loaded with the hermetic test environment."""
    from banking_data_agents.config import get_settings

    return get_settings()


@pytest.fixture(scope="session")
def floci_available() -> bool:
    """True when a Floci emulator answers on the configured endpoint."""
    import httpx

    for port in (4566,):
        try:
            resp = httpx.get(f"http://localhost:{port}/_floci/health", timeout=2.0)
            if resp.status_code < 500:
                return True
        except Exception:
            continue
    return False


@pytest.fixture
def require_floci(floci_available: bool) -> Iterator[None]:
    """Skip a test unless Floci is running."""
    if not floci_available:
        pytest.skip("Floci emulator is not running (run `make floci-up`)")
    yield


@pytest.fixture
def temp_landing(tmp_path: Path) -> Iterator[Path]:
    """Isolated landing directory for generator tests."""
    landing = tmp_path / "landing"
    landing.mkdir(parents=True, exist_ok=True)
    yield landing


def _reset_process_caches() -> None:
    """Drop every process-wide cache that depends on settings.

    Called before and after the shared lake is built. Without it a test module
    that ran earlier leaves an engine (or a catalog) pointing at the real
    ``data/`` directory and the next module reads the wrong lake.
    """
    from banking_data_agents.catalog.registry import reset_catalog
    from banking_data_agents.config import reset_settings_cache
    from banking_data_agents.llm.factory import reset_models
    from banking_data_agents.semantic.metrics import reload_metrics
    from banking_data_agents.tools.context import reset_engines

    reset_settings_cache()
    reset_catalog()
    reset_engines()
    reset_models()
    reload_metrics()


@pytest.fixture(scope="session")
def pipelined_lake(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    """A complete medallion lake, built once for the whole session.

    Session scope rather than module scope because the pipeline takes seconds and
    every tool and agent test needs the same lake; rebuilding it per module would
    triple the suite's runtime for no extra confidence.

    Yields ``(landing_dir, data_dir)``.
    """
    from pytest import MonkeyPatch

    from banking_data_agents.datagen.generate import generate_into
    from banking_data_agents.pipeline.runner import run_pipeline

    monkeypatch = MonkeyPatch()
    data_dir = tmp_path_factory.mktemp("lake")
    landing = tmp_path_factory.mktemp("landing")
    monkeypatch.setenv("BDA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BDA_LANDING_DIR", str(landing))
    # A full 12-month window: the data-quality incidents, the CRM schema drift and
    # the fraud dispute-window boundary all need enough history to contain them.
    monkeypatch.setenv("SYNTHETIC_MONTHS", "12")
    _reset_process_caches()

    try:
        generate_into(landing, seed=99, scale=1.0)
        assert run_pipeline(verbose=False) == 0
        _reset_process_caches()
        yield landing, data_dir
    finally:
        _reset_process_caches()
        monkeypatch.undo()
        _reset_process_caches()


@pytest.fixture
def tool_context(pipelined_lake: tuple[Path, Path]):
    """A ToolContext over the shared lake, with a freshly registered engine."""
    from banking_data_agents.tools.context import ToolContext, reset_engines

    _ = pipelined_lake
    reset_engines()
    return ToolContext()


@pytest.fixture
def agent_env(pipelined_lake: tuple[Path, Path]):
    """The shared lake, ready for an agent (models and engines reset)."""
    from banking_data_agents.llm.factory import reset_models
    from banking_data_agents.tools.context import reset_engines

    _ = pipelined_lake
    reset_engines()
    reset_models()
    return pipelined_lake
