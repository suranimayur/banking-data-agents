"""Shared state for the agent tools.

One :class:`ToolContext` per conversation. It owns the things a tool needs and
nothing else: the catalog, the query engine, and an append-only trace of every
tool call. The trace is what makes an answer auditable — you can replay exactly
which products were inspected, which SQL was generated, and which rows came back.

The query engine is cached per process because registering the medallion views is
the expensive part of a query and the lake does not change underneath a running
process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any

from banking_data_agents.catalog.registry import Catalog, get_catalog
from banking_data_agents.config import Settings, get_settings
from banking_data_agents.logging_setup import get_logger
from banking_data_agents.pipeline.engine import QueryEngine, get_engine
from banking_data_agents.pipeline.lake import LocalLake, get_lake

logger = get_logger(__name__)


@lru_cache(maxsize=2)
def _engine_for(kind: str) -> QueryEngine:
    lake = LocalLake(get_settings().data_dir / "lake") if kind == "local" else get_lake()
    engine = get_engine(lake)
    engine.register_all()
    logger.info("query_engine_ready", engine=engine.name, lake=kind)
    return engine


def get_cached_engine() -> QueryEngine:
    """A process-wide engine, registered once."""
    lake = get_lake()
    kind = "local" if isinstance(lake, LocalLake) else "remote"
    return _engine_for(kind)


def reset_engines() -> None:
    """Drop cached engines (tests, and after a pipeline run republishes tables)."""
    _engine_for.cache_clear()
    _anchor_cache.cache_clear()


@lru_cache(maxsize=1)
def _anchor_cache(kind: str) -> date:
    """Latest day present in the transaction product.

    Relative windows ("last 3 months") are resolved against the data, not against
    the wall clock, so a demo dataset generated last quarter still answers
    questions about its own most recent quarter.
    """
    engine = get_cached_engine()
    try:
        frame = engine.query("SELECT MAX(transaction_date) AS d FROM gold.transaction")
        value = frame["d"][0]
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])
    except Exception:
        logger.warning("anchor_date_unavailable", fallback="today")
        return date.today()


def data_anchor() -> date:
    return _anchor_cache("global")


@dataclass
class ToolContext:
    """Everything a tool call needs, plus the audit trail of the conversation."""

    settings: Settings = field(default_factory=get_settings)
    catalog: Catalog = field(default_factory=get_catalog)
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: Set by :class:`~banking_data_agents.agents.copilot.Copilot` so every trace
    #: entry can be joined back to the request that caused it.
    trace_id: str = ""
    #: Full, untruncated tool results keyed by tool name, in call order. The trace
    #: above is deliberately lossy (it is a log); the evidence envelope is not (it
    #: is the answer's provenance), so it reads from here.
    outputs: dict[str, list[Any]] = field(default_factory=dict)
    #: Columns this conversation may not group or rank by. Set by the agent, so a
    #: specialist's restriction is a property of the tool context and therefore
    #: enforced by the guardrail on every query it runs.
    forbidden_dimensions: frozenset[str] = frozenset()

    _engine: QueryEngine | None = field(default=None, init=False, repr=False)
    _anchor: date | None = field(default=None, init=False, repr=False)

    @property
    def engine(self) -> QueryEngine:
        if self._engine is None:
            self._engine = get_cached_engine()
        return self._engine

    @property
    def anchor(self) -> date:
        if self._anchor is None:
            self._anchor = data_anchor()
        return self._anchor

    # -- audit --------------------------------------------------------------
    def record(self, tool: str, arguments: dict[str, Any], result: Any) -> None:
        """Append a tool invocation to the trace. Never raises."""
        try:
            summary = result if isinstance(result, (str, int, float, bool, type(None))) else _summarise(result)
        except Exception:
            summary = "<unserialisable>"
        self.trace.append({"tool": tool, "arguments": arguments, "result_summary": summary, "trace_id": self.trace_id})
        self.outputs.setdefault(tool, []).append(result)
        logger.debug("tool_called", tool=tool, trace_id=self.trace_id)

    def called_tools(self) -> list[str]:
        return [entry["tool"] for entry in self.trace]

    def last(self, tool: str) -> Any:
        """The most recent full result for a tool, or ``None`` if it never ran."""
        results = self.outputs.get(tool) or []
        return results[-1] if results else None

    def clear(self) -> None:
        """Forget the conversation's tool history (a new question starts clean)."""
        self.trace.clear()
        self.outputs.clear()


def _summarise(result: Any) -> Any:
    """Keep traces small: dictionaries are truncated, long lists are counted."""
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for key, value in result.items():
            if isinstance(value, list):
                out[key] = f"<{len(value)} item(s)>" if len(value) > 8 else value
            elif isinstance(value, dict):
                out[key] = _summarise(value)
            else:
                out[key] = value
        return out
    if isinstance(result, list):
        return f"<{len(result)} item(s)>"
    return result


__all__ = ["ToolContext", "data_anchor", "get_cached_engine", "reset_engines"]
