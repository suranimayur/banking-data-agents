"""AWS client construction.

Every boto3 client in the project is built here so that pointing the platform at
Floci versus real AWS is a single environment variable. botocore honours
``AWS_ENDPOINT_URL`` natively, but we set it explicitly so that the behaviour is
visible and overridable per call site.
"""

from __future__ import annotations

import functools
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from banking_data_agents.config import get_settings

#: boto3 client constructor defaults tuned for a demo platform.
_CLIENT_CONFIG = BotoConfig(
    retries={"max_attempts": 5, "mode": "standard"},
    connect_timeout=5,
    read_timeout=120,
    # Floci serves every service on one port; keep connection reuse on.
    max_pool_connections=25,
)


@functools.lru_cache(maxsize=64)
def client(service: str, *, region: str | None = None) -> Any:
    """Return a cached boto3 client for ``service``.

    Respects ``AWS_ENDPOINT_URL`` so the identical call graph runs against Floci
    or real AWS without branching.
    """
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "region_name": region or settings.aws_region,
        "aws_access_key_id": settings.aws_access_key_id,
        "aws_secret_access_key": settings.aws_secret_access_key,
        "config": _CLIENT_CONFIG,
    }
    if settings.aws_session_token:
        kwargs["aws_session_token"] = settings.aws_session_token
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return boto3.client(service, **kwargs)


def clear_client_cache() -> None:
    """Drop cached clients (tests and credential rotation)."""
    client.cache_clear()


def s3() -> Any:
    return client("s3")


def glue() -> Any:
    return client("glue")


def athena() -> Any:
    return client("athena")


def dynamodb() -> Any:
    return client("dynamodb")


def stepfunctions() -> Any:
    return client("stepfunctions")


def bedrock_runtime() -> Any:
    return client("bedrock-runtime")


def secretsmanager() -> Any:
    return client("secretsmanager")


def sts() -> Any:
    return client("sts")


@functools.lru_cache(maxsize=8)
def _probe(url: str, timeout: float) -> bool:
    import httpx

    try:
        return httpx.get(url, timeout=timeout).status_code < 500
    except Exception:
        return False


def endpoint_reachable(timeout: float = 0.75) -> bool:
    """Best-effort check that the configured AWS endpoint answers.

    Used by the CLI to fail fast with a helpful message instead of a stack trace
    when the Floci container is not running, and by the lake to fall back to the
    local backend instead of hanging on every client construction.

    The probe is short and its result is cached for the process lifetime, so an
    emulator that is down costs one timeout rather than one per call.
    """
    settings = get_settings()
    if not settings.aws_endpoint_url:
        return True

    return any(_probe(f"{settings.aws_endpoint_url}{path}", timeout) for path in ("/_floci/health", "/"))


def clear_reachability_cache() -> None:
    """Forget a cached reachability result (e.g. after starting the emulator)."""
    _probe.cache_clear()


def describe_endpoint() -> str:
    """Human-readable description of where AWS calls are going."""
    settings = get_settings()
    if settings.aws_endpoint_url:
        return f"Floci emulator at {settings.aws_endpoint_url} (region {settings.aws_region})"
    return f"real AWS (region {settings.aws_region})"


def is_missing_bucket(error: ClientError) -> bool:
    """True when a ClientError is an S3 "no such bucket"."""
    code = error.response.get("Error", {}).get("Code", "")
    return code in {"NoSuchBucket", "404", "NotFound"}


__all__ = [
    "ClientError",
    "EndpointConnectionError",
    "athena",
    "bedrock_runtime",
    "clear_client_cache",
    "clear_reachability_cache",
    "client",
    "describe_endpoint",
    "dynamodb",
    "endpoint_reachable",
    "glue",
    "is_missing_bucket",
    "s3",
    "secretsmanager",
    "stepfunctions",
    "sts",
]
