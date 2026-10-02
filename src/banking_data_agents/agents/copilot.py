"""The Data Product Copilot.

The front door to the platform. It gets the whole tool surface because an analyst
question can be about anything: what exists, what a column means, whether it is
trustworthy, where it came from, and what the number is.
"""

from __future__ import annotations

from banking_data_agents.agents.base import BaseAgent
from banking_data_agents.agents.prompts import COPILOT_PROMPT

__all__ = ["Copilot"]


class Copilot(BaseAgent):
    """Answers analyst questions over governed data products, with evidence."""

    name = "copilot"
    tier = "reasoner"
    prompt = COPILOT_PROMPT
    #: The full surface. Narrowing happens for the specialists, not here.
    tools_enabled: tuple[str, ...] | None = None
