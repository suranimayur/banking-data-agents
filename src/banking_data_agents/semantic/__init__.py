"""The governed semantic layer.

The single most important governance decision in this platform: agents do not
write free-form SQL against tables. They *select a versioned metric* and fill a
template. Numerical invention becomes structurally difficult rather than merely
discouraged, and when a figure is wrong the blame is locatable to a metric
version.
"""

from banking_data_agents.semantic.metrics import (
    Metric,
    ResolutionResult,
    load_metrics,
    resolve_metric,
)
from banking_data_agents.semantic.planner import (
    Plan,
    compile_plan,
    explain_plan,
    plan_and_compile,
    plan_question,
)

__all__ = [
    "Metric",
    "Plan",
    "ResolutionResult",
    "compile_plan",
    "explain_plan",
    "load_metrics",
    "plan_and_compile",
    "plan_question",
    "resolve_metric",
]
