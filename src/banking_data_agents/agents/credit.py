"""The Credit Risk Analyst.

The third agent over the same substrate. Its distinguishing feature is that the
governance path is mandatory rather than conditional: credit attributes are the
ones most likely to be repurposed for marketing, so this agent keeps
``check_allowed_use`` and its prompt makes the check unconditional before any
question that touches scoring or eligibility.

It is also the agent whose answers are most likely to be read as decisions, which
is why its prompt forbids approving, declining or pricing anything.
"""

from __future__ import annotations

from banking_data_agents.agents.base import BaseAgent
from banking_data_agents.agents.prompts import CREDIT_PROMPT

__all__ = ["CreditAgent"]


class CreditAgent(BaseAgent):
    """Monitors the credit book: exposure, delinquency, utilisation, repayment."""

    name = "credit"
    tier = "reasoner"
    prompt = CREDIT_PROMPT

    #: Keeps `resolve_metric` and `get_contract`, because exposure questions
    #: genuinely hinge on which *basis* is meant (outstanding vs limit vs drawn),
    #: and that is exactly the ambiguity the resolver exists to surface.
    tools_enabled = (
        "search_catalog",
        "list_products",
        "get_contract",
        "resolve_metric",
        "check_allowed_use",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "explain_quality",
        "trace_lineage",
    )
