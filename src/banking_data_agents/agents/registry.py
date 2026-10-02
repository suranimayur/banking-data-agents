"""Names to agent classes.

Kept in a separate module from :mod:`banking_data_agents.agents.base` so that the
base class can be imported without importing every agent (and therefore without
importing every prompt), which keeps import cycles impossible.
"""

from __future__ import annotations

from banking_data_agents.agents.base import BaseAgent
from banking_data_agents.agents.copilot import Copilot
from banking_data_agents.agents.credit import CreditAgent
from banking_data_agents.agents.fraud import FraudAgent

#: The agents the platform ships, keyed by the name used on the command line,
#: in the API and in evaluation suites.
AGENTS: dict[str, type[BaseAgent]] = {
    "copilot": Copilot,
    "fraud": FraudAgent,
    "credit": CreditAgent,
}

__all__ = ["AGENTS", "Copilot", "CreditAgent", "FraudAgent"]
