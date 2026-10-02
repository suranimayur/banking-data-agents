"""Model provider abstraction.

One logical model interface, three implementations selected by ``LLM_PROVIDER``:

``stub``
    A deterministic scripted model. Free, offline and reproducible, so the
    evaluation suite and CI can assert behaviour instead of vibes. This is the
    default for tests.

``floci``
    Amazon Bedrock Runtime emulated by Floci at ``http://localhost:4566``. Proves
    the AWS wiring end to end without a cloud account.

``bedrock``
    Real Amazon Bedrock. Same code path, real Claude models, real token cost.

All three are Strands ``Model`` implementations, so the agent code is identical
in every case and no business logic branches on the environment.
"""

from banking_data_agents.llm.factory import (
    describe_model,
    get_model,
    get_provider_name,
    reset_models,
)
from banking_data_agents.llm.stub import StubModel

__all__ = [
    "StubModel",
    "describe_model",
    "get_model",
    "get_provider_name",
    "reset_models",
]
