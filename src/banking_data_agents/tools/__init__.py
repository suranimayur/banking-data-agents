"""The agent tool layer.

Three concerns, deliberately separated:

``impl``
    Plain Python functions over a :class:`ToolContext`. All the behaviour.
``registry``
    Strands tool definitions — names, descriptions and JSON schemas — that
    delegate to ``impl``.
``sqlguard``
    The allowlist that decides what SQL may run at all.

Splitting them means the tool surface is testable without a model, the prompts
seen by the model are reviewable in one file, and the guardrail cannot be
bypassed by a tool that forgets to call it.
"""

from banking_data_agents.tools.context import ToolContext, data_anchor, get_cached_engine, reset_engines
from banking_data_agents.tools.registry import READ_ONLY_TOOLS, TOOL_NAMES, build_tools
from banking_data_agents.tools.sqlguard import allowed_datasets, validate_sql

__all__ = [
    "READ_ONLY_TOOLS",
    "TOOL_NAMES",
    "ToolContext",
    "allowed_datasets",
    "build_tools",
    "data_anchor",
    "get_cached_engine",
    "reset_engines",
    "validate_sql",
]
