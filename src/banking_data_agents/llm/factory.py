"""Model selection.

The whole platform talks to one interface — the Strands ``Model`` protocol — and
this module decides which implementation sits behind it:

=========  =====================================================================
Provider   What it is
=========  =====================================================================
``stub``   :class:`~banking_data_agents.llm.stub.StubModel`. Deterministic,
           offline, free. CI and the evaluation suite run against this.
``floci``  Amazon Bedrock Runtime as emulated by Floci on ``localhost:4566``.
           Proves the AWS wiring, the request shape and the retry/error paths
           without a cloud account. Still free.
``bedrock`` Real Amazon Bedrock with real credentials and real token cost.
=========  =====================================================================

Nothing else in the codebase branches on the provider. A change of provider
changes which bytes leave the process, not which code runs — which is the only
way "tested locally, runs in production" can be an honest claim.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from banking_data_agents.config import ModelTier, get_settings
from banking_data_agents.llm.stub import StubModel
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

#: Bedrock model configuration applied to every real request.
#: Temperature zero because these agents must be reproducible and auditable —
#: the same question must not produce two different numbers for two people.
_MODEL_CONFIG: dict[str, Any] = {
    "temperature": 0.0,
    "max_tokens": 4096,
}

#: Providers for which an endpoint URL means "emulator" rather than nothing.
_URL_PROVIDERS = {"floci"}


def get_provider_name() -> str:
    """The configured provider, lowercased and validated."""
    return get_settings().llm_provider


def _stub_model() -> StubModel:
    logger.info("llm_provider_selected", provider="stub", cost="free")
    return StubModel()


def _bedrock_model(tier: ModelTier) -> Any:
    """Build a Strands ``BedrockModel``, pointed at Floci or at real AWS."""
    import boto3
    from botocore.client import Config as BotoConfig
    from strands.models.bedrock import BedrockModel

    settings = get_settings()
    provider = settings.llm_provider

    # A session is passed rather than relying on the ambient environment because
    # the settings file (.env) is not exported into ``os.environ``; without this
    # the SDK would sign with whatever credentials happen to be on the machine.
    session = boto3.Session(
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        aws_session_token=settings.aws_session_token or None,
    )

    kwargs: dict[str, Any] = {
        "boto_session": session,
        "model_id": settings.model_for(tier),
        # Long reads are normal for a reasoning model; short connects fail fast.
        "boto_client_config": BotoConfig(
            retries={"max_attempts": 4, "mode": "standard"},
            connect_timeout=5,
            read_timeout=120,
        ),
        **_MODEL_CONFIG,
    }

    if provider in _URL_PROVIDERS and settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url

    logger.info(
        "llm_provider_selected",
        provider=provider,
        tier=tier,
        model_id=kwargs["model_id"],
        endpoint=kwargs.get("endpoint_url") or "aws",
    )
    return BedrockModel(**kwargs)


@lru_cache(maxsize=8)
def get_model(tier: ModelTier = "reasoner") -> Any:
    """Return the model for a logical tier, cached per process.

    ``router``    classification and routing decisions — small and cheap.
    ``reasoner``  the analyst-facing Copilot and the specialist agents.
    ``escalation`` long-horizon work: root-causing a data incident, drafting a
                  contract change.
    """
    provider = get_provider_name()
    if provider == "stub":
        # Every tier is the same scripted policy; the tier only matters once a
        # provider that charges per token is in play.
        return _stub_model()
    return _bedrock_model(tier)


def reset_models() -> None:
    """Drop cached models. Used by tests that switch provider mid-process."""
    get_model.cache_clear()


def describe_model(tier: ModelTier = "reasoner") -> str:
    """One-line description for CLI banners and the console header."""
    settings = get_settings()
    provider = settings.llm_provider
    if provider == "stub":
        return "stub:scripted (deterministic, offline, free)"
    if provider == "floci":
        return f"floci bedrock-runtime {settings.aws_endpoint_url} · {settings.model_for(tier)}"
    return f"bedrock {settings.aws_region} · {settings.model_for(tier)}"


__all__ = ["describe_model", "get_model", "get_provider_name", "reset_models"]
