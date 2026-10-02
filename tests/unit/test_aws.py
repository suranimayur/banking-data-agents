"""The client factory must be the only place that knows about Floci."""

from __future__ import annotations

import pytest

from banking_data_agents import aws
from banking_data_agents.config import get_settings, reset_settings_cache


@pytest.fixture(autouse=True)
def _clear_clients():
    aws.clear_client_cache()
    yield
    aws.clear_client_cache()
    reset_settings_cache()


def test_clients_built_without_endpoint_talk_to_aws() -> None:
    s3 = aws.s3()
    assert s3.meta.service_model.service_name == "s3"
    assert s3.meta.endpoint_url.startswith("https://"), s3.meta.endpoint_url


def test_clients_are_cached_not_reconstructed() -> None:
    assert aws.s3() is aws.s3()


def test_endpoint_override_redirects_every_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    reset_settings_cache()
    aws.clear_client_cache()

    assert aws.s3().meta.endpoint_url == "http://localhost:4566"
    assert aws.glue().meta.endpoint_url == "http://localhost:4566"
    assert aws.athena().meta.endpoint_url == "http://localhost:4566"
    assert aws.bedrock_runtime().meta.endpoint_url == "http://localhost:4566"

    assert get_settings().is_local is True

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    reset_settings_cache()
    aws.clear_client_cache()


def test_describe_endpoint_is_human_readable() -> None:
    assert "real AWS" in aws.describe_endpoint()


def test_missing_bucket_detection() -> None:
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "NoSuchBucket", "Message": "nope"}}, "HeadBucket")
    assert aws.is_missing_bucket(err) is True

    other = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "HeadBucket")
    assert aws.is_missing_bucket(other) is False
