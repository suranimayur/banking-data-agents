"""Configuration resolves correctly and is environment-driven."""

from __future__ import annotations

import pytest

from banking_data_agents.config import Settings, get_settings, reset_settings_cache


def test_defaults_are_local_and_free(settings: Settings) -> None:
    assert settings.env == "local"
    assert settings.llm_provider == "stub", "tests must never call a real model"
    assert settings.project == "banking-data-agents"


def test_bucket_uris_are_derived_not_hardcoded(settings: Settings) -> None:
    assert settings.gold_uri == f"s3://{settings.bda_gold_bucket}"
    assert settings.athena_output_uri.endswith("/athena-results/")
    assert len(settings.all_buckets) == 5
    assert len(set(settings.all_buckets)) == 5, "bucket names must be distinct"


def test_model_tiers_resolve_to_distinct_models(settings: Settings) -> None:
    router = settings.model_for("router")
    reasoner = settings.model_for("reasoner")
    escalation = settings.model_for("escalation")
    assert len({router, reasoner, escalation}) == 3
    assert "haiku" in router
    assert "sonnet" in reasoner


def test_endpoint_url_drives_local_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    reset_settings_cache()
    try:
        assert get_settings().is_local is True
    finally:
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        reset_settings_cache()


def test_unknown_model_tier_is_rejected(settings: Settings) -> None:
    with pytest.raises(KeyError):
        settings.model_for("does-not-exist")  # type: ignore[arg-type]


def test_budget_defaults_are_finite(settings: Settings) -> None:
    assert 0 < settings.session_budget_usd < settings.user_daily_budget_usd
    assert settings.agent_max_tool_calls > 0
