"""The agent layer.

Three agents, one machine. ``Copilot`` answers analyst questions over the whole
platform; ``FraudAgent`` and ``CreditAgent`` are specialists over a narrowed tool
surface. All three return an :class:`~banking_data_agents.agents.evidence.Evidence`
envelope, so every answer carries the SQL, the metric versions, the contracts and
the tool trace that produced it.
"""

from banking_data_agents.agents.base import BaseAgent, get_agent
from banking_data_agents.agents.copilot import Copilot
from banking_data_agents.agents.credit import CreditAgent
from banking_data_agents.agents.evidence import Evidence, build_evidence
from banking_data_agents.agents.fraud import FraudAgent
from banking_data_agents.agents.registry import AGENTS

__all__ = [
    "AGENTS",
    "BaseAgent",
    "Copilot",
    "CreditAgent",
    "Evidence",
    "FraudAgent",
    "build_evidence",
    "get_agent",
]
