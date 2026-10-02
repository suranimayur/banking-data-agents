"""The Fraud Signal Analyst.

A specialist over the same substrate as the Copilot. The interesting engineering
is not the prompt — it is that this agent's tool surface cannot reach the labelled
outcomes. The fraud ground truth lives in ``ops.fraud_ground_truth``, which
:mod:`banking_data_agents.tools.sqlguard` refuses for every agent; the subset here
narrows the surface further so the intent is legible from the configuration.

Why this matters: the holdout labels exist to *measure* the platform. An agent
that can read them will eventually leak them into an answer, and then the
evaluation numbers are worthless.
"""

from __future__ import annotations

from banking_data_agents.agents.base import BaseAgent
from banking_data_agents.agents.prompts import FRAUD_PROMPT

__all__ = ["FraudAgent"]


class FraudAgent(BaseAgent):
    """Reports elevated-risk patterns in the transaction product."""

    name = "fraud"
    tier = "reasoner"
    prompt = FRAUD_PROMPT

    #: Everything except metric *resolution*: the fraud agent should work from
    #: named behavioural metrics, not from a fuzzy business term it half-matched.
    #: `resolve_metric` is withheld so a vague term becomes a question to the
    #: user rather than a silent best guess.
    tools_enabled = (
        "search_catalog",
        "list_products",
        "get_contract",
        "check_allowed_use",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "explain_quality",
        "trace_lineage",
    )

    #: This agent reports patterns, not people. Grouping or ranking by
    #: ``customer_id`` produces a list of named individuals, which is an
    #: enforcement decision rather than an analytical one, so the SQL guardrail
    #: refuses it. The prompt says the same thing; this is the part that holds.
    forbidden_dimensions = frozenset({"customer_id"})
