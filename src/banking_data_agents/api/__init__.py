"""HTTP surface.

``banking_data_agents.api.app`` builds a FastAPI application around the agent
layer. Run it with ``bda serve`` or ``uvicorn banking_data_agents.api.app:app``.

The API returns the same evidence envelope the CLI prints, so a caller integrating
over HTTP gets the same provenance a terminal user sees — including the SQL, the
metric versions and the tool trace.
"""

from banking_data_agents.api.app import AskRequest, AskResponse, create_app

__all__ = ["AskRequest", "AskResponse", "create_app"]
