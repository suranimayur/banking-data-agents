"""The AWS boundary: object storage, the catalog, the answer store.

Two flavours of test, deliberately separated by fixture so that a failure says which
boundary broke:

``moto``
    In-process mocks. No Docker, no network, deterministic. These are what CI runs,
    and they cover the shape of every call the platform makes.
``floci``
    The real local emulator. These are skipped when it is not running, so a laptop
    without Docker still gets a green suite rather than an ambiguous one.
"""

from __future__ import annotations

import pytest

from banking_data_agents.aws import glue, s3

pytestmark = pytest.mark.integration


@pytest.fixture
def moto_aws(monkeypatch: pytest.MonkeyPatch):
    """Route boto3 at moto, with a clean client cache on both sides of the test.

    A *deployed* env is forced (``BDA_ENV=dev``) because the local env short-circuits
    provisioning: ``ensure_buckets``/``ensure_glue_databases`` deliberately do nothing
    when the platform is running purely on the filesystem. This test is about the
    deployed path, so it has to look deployed.

    Imported inside the fixture so that a developer without the dev extras can still
    run the unit suite.
    """
    moto = pytest.importorskip("moto", reason="moto is a dev dependency for integration tests")
    from banking_data_agents.aws import clear_client_cache

    mock = moto.mock_aws()
    mock.start()
    monkeypatch.setenv("AWS_ENDPOINT_URL", "")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # The `env` field has no prefix, so a deployed environment is `ENV=dev`.
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("BDA_LAKE_BACKEND", "s3")

    from banking_data_agents.config import reset_settings_cache
    from banking_data_agents.tools.context import reset_engines

    reset_settings_cache()
    reset_engines()
    clear_client_cache()
    try:
        yield mock
    finally:
        clear_client_cache()
        reset_engines()
        reset_settings_cache()
        mock.stop()


# ---------------------------------------------------------------------------
# moto: the shape of every call
# ---------------------------------------------------------------------------
def test_lake_creates_its_buckets_and_survives_a_round_trip(moto_aws, tmp_path, monkeypatch) -> None:
    """Write a zone, read it back, over a real (mocked) S3 API.

    This is the test that catches the mistakes the local backend hides: a wrong
    region, a bucket that was never created, a key layout that only works on a
    filesystem.
    """
    import polars as pl

    from banking_data_agents.config import get_settings
    from banking_data_agents.pipeline.lake import S3Lake, ensure_buckets

    settings = get_settings()
    ensure_buckets()

    buckets = {b["Name"] for b in s3().list_buckets().get("Buckets", [])}
    for name in settings.all_buckets:
        assert name in buckets, f"{name} was not created"

    lake = S3Lake()
    frame = pl.DataFrame({"customer_id": ["CUST-0000001", "CUST-0000002"], "balance": [100.0, 250.5]})
    lake.write("gold", "integration_probe", frame)

    read_back = lake.read("gold", "integration_probe")
    assert read_back.height == 2
    assert set(read_back.columns) == {"customer_id", "balance"}

    listed = s3().list_objects_v2(Bucket=settings.bda_gold_bucket, Prefix="integration_probe")
    assert listed.get("KeyCount", 0) >= 1, "the object was not written under the expected prefix"


def test_glue_database_registration_is_idempotent(moto_aws) -> None:
    """Creating the catalog twice must not fail: every run of the pipeline does it."""
    from banking_data_agents.pipeline.lake import ensure_glue_databases

    first = set(ensure_glue_databases())
    assert first, "no Glue databases were registered"
    registered = {d["Name"] for d in glue().get_databases().get("DatabaseList", [])}
    assert first <= registered

    # The second call is what a re-run does, and it is where a naive implementation
    # raises AlreadyExistsException.
    second = set(ensure_glue_databases())
    assert second == set(), "a re-run created databases that already existed"


def test_answer_store_round_trips_an_envelope(moto_aws) -> None:
    """The audit trail has to be readable after the fact, over the real API."""
    from banking_data_agents import aws
    from banking_data_agents.audit import DynamoDBAnswerStore, ensure_answer_store
    from banking_data_agents.config import get_settings

    settings = get_settings()
    table = ensure_answer_store()
    assert table == settings.answer_table_name

    store = DynamoDBAnswerStore()
    envelope = {"trace_id": "abc123", "asked_at": "2026-09-29T00:00:00Z", "outcome": "ANSWERED_FROM_DATA", "agent": "copilot"}
    store.put("abc123", envelope)

    stored = store.get("abc123")
    assert stored is not None
    assert stored["outcome"] == "ANSWERED_FROM_DATA"
    assert stored["agent"] == "copilot"

    # And it is a real table with the key schema the CDK stack declares: a partition
    # key of trace_id and a sort key of asked_at.
    key_schema = aws.dynamodb().describe_table(TableName=table)["Table"]["KeySchema"]
    assert {k["AttributeName"] for k in key_schema} == {"trace_id", "asked_at"}


# ---------------------------------------------------------------------------
# Floci: the same boundary, on the emulator
# ---------------------------------------------------------------------------
def test_floci_answers_a_liveness_probe(require_floci) -> None:
    """If the emulator is up, it should be able to create and list a bucket."""
    import uuid as uuidlib

    name = f"bda-integration-{uuidlib.uuid4().hex[:12]}"
    client = s3()
    client.create_bucket(Bucket=name)
    try:
        assert name in {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    finally:
        # Leave the emulator as we found it: a test that leaks a bucket makes the
        # next test's assertions ambiguous.
        for obj in client.list_objects_v2(Bucket=name).get("Contents", []):
            client.delete_object(Bucket=name, Key=obj["Key"])
        client.delete_bucket(Bucket=name)


def test_floci_serves_the_glue_catalog(require_floci) -> None:
    """The emulator's Glue API is what the pipeline writes its tables into."""
    databases = glue().get_databases().get("DatabaseList", [])
    assert isinstance(databases, list)
