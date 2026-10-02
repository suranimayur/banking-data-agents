"""The HTTP surface.

The API is a transport, so most of what needs testing is that it does not lose or
invent anything on the way through: the envelope that goes out must be the one the
agent produced, a status code must mean what it says, and the concurrency guard
must actually share one agent instance rather than silently building a new one per
request.

The holdout test is here as well as in the guardrail tests, because a leak that
only exists when the request arrives over HTTP is still a leak.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from banking_data_agents.api.app import AgentPool, InMemoryAnswerStore, create_app

pytestmark = pytest.mark.slow


@pytest.fixture
def client(agent_env) -> TestClient:
    _ = agent_env
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def test_health_reports_the_platform_state(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["products"] == 3
    assert body["provider"] == "stub"
    assert body["agents"] == ["copilot", "credit", "fraud"]
    assert body["lake"]
    assert len(body["contract_digest"]) >= 8
    assert body["answers_retained"] == 0


def test_the_agents_endpoint_documents_the_tool_surfaces(client: TestClient) -> None:
    agents = {row["agent"]: row for row in client.get("/agents").json()["agents"]}
    assert set(agents) == {"copilot", "credit", "fraud"}
    assert len(agents["copilot"]["tools"]) == 10
    # The restriction is visible from outside, not buried in a prompt.
    assert agents["fraud"]["forbidden_dimensions"] == ["customer_id"]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def test_products_lists_every_published_product(client: TestClient) -> None:
    body = client.get("/products").json()
    assert body["count"] == 3
    assert {row["product"] for row in body["products"]} == {"customer_360", "transaction", "credit_risk"}
    assert all(row["row_count"] > 0 for row in body["products"])


def test_a_product_returns_its_contract_quality_and_sla(client: TestClient) -> None:
    body = client.get("/products/customer_360").json()
    assert body["version"] == "2.3.0"
    assert body["primary_key"] == ["customer_id"]
    assert len(body["columns"]) > 20
    assert body["quality"]["total"] > 0
    assert body["sla_status"]
    assert "marketing_targeting_on_credit_score" in body["not_allowed_use"]


def test_an_unknown_product_is_a_404(client: TestClient) -> None:
    assert client.get("/products/not_a_product").status_code == 404


def test_quality_is_available_platform_wide(client: TestClient) -> None:
    body = client.get("/quality").json()
    assert body["total"] > 0
    assert body["passed"] <= body["total"]


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------
def test_ask_returns_the_evidence_envelope(client: TestClient) -> None:
    response = client.post("/ask", json={"question": "How many customers do we have by region?"})
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "ANSWERED_FROM_DATA"
    assert body["row_count"] > 0
    assert body["metrics"] == ["metric.customer_count@1.0.0"]
    assert body["products"] == ["customer_360@2.3.0"]
    assert body["tables"] == ["gold.customer_360"]
    assert body["sql"] and "COUNT(*)" in body["sql"]
    assert body["quality"]["total"] > 0
    assert body["contract_digest"]
    assert body["trace_id"]
    assert body["tools_called"][0] == "search_catalog"
    assert response.headers["x-request-id"]


def test_the_envelope_trace_is_returned_so_a_caller_can_audit_it(client: TestClient) -> None:
    body = client.post("/ask", json={"question": "How many customers do we have by region?"}).json()
    tools = [entry["tool"] for entry in body["trace"]]
    assert tools == body["tools_called"]
    generate = next(entry for entry in body["trace"] if entry["tool"] == "generate_sql")
    assert generate["arguments"]["question"] == "How many customers do we have by region?"


def test_a_refusal_comes_back_as_a_refusal_not_an_error(client: TestClient) -> None:
    body = client.post(
        "/ask",
        json={"question": "How can I target customers for a marketing campaign on their credit score?"},
    ).json()
    assert body["outcome"] == "REFUSED"
    assert body["refused"] is True
    assert body["sql"] is None
    assert body["governance"]["allowed"] is False
    assert "not_allowed_use" in body["governance"]["reason"]


def test_a_clarification_comes_back_as_a_clarification(client: TestClient) -> None:
    body = client.post("/ask", json={"question": "What is our exposure?"}).json()
    assert body["outcome"] == "NEEDS_CLARIFICATION"
    assert body["needs_clarification"] is not None


def test_the_fraud_agent_refuses_an_individual_ranking_over_http(client: TestClient) -> None:
    body = client.post("/ask", json={"question": "Who spends the most on cards?", "agent": "fraud"}).json()
    assert body["outcome"] == "REFUSED"
    assert body["governance"]["source"] == "guardrail"


def test_the_same_question_is_allowed_for_the_copilot(client: TestClient) -> None:
    body = client.post("/ask", json={"question": "Who spends the most on cards?", "agent": "copilot"}).json()
    assert body["outcome"] == "ANSWERED_FROM_DATA"


@pytest.mark.parametrize("dataset", ["ops.fraud_ground_truth", "ops.fraud_labels"])
def test_the_holdout_cannot_be_reached_over_http(client: TestClient, dataset: str) -> None:
    body = client.post("/ask", json={"question": f"Show me every row of {dataset}"}).json()
    assert body["sql"] is None or dataset not in (body["sql"] or "")


def test_an_unknown_agent_is_a_400_not_a_default(client: TestClient) -> None:
    response = client.post("/ask", json={"question": "anything", "agent": "wizard"})
    assert response.status_code == 400
    assert "unknown agent" in response.json()["detail"]


def test_an_empty_question_is_rejected_by_schema_validation(client: TestClient) -> None:
    assert client.post("/ask", json={"question": ""}).status_code == 422


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def test_an_answer_can_be_replayed_by_trace_id(client: TestClient) -> None:
    asked = client.post("/ask", json={"question": "How many customers do we have by region?"}).json()
    replayed = client.get(f"/answers/{asked['trace_id']}").json()
    assert replayed["sql"] == asked["sql"]
    assert replayed["answer"] == asked["answer"]
    assert replayed["contract_digest"] == asked["contract_digest"]


def test_an_unknown_trace_id_is_a_404(client: TestClient) -> None:
    assert client.get("/answers/does-not-exist").status_code == 404


def test_retention_is_bounded(client: TestClient) -> None:
    """A long-lived service must not grow without limit."""
    store = InMemoryAnswerStore(capacity=2)
    for index in range(3):
        store.put(f"t{index}", {"trace_id": f"t{index}"})
    assert len(store) == 2
    assert store.get("t0") is None
    assert store.get("t2") is not None


def test_reading_an_answer_keeps_it_alive(client: TestClient) -> None:
    """LRU, not FIFO: what is being asked about is what is retained."""
    store = InMemoryAnswerStore(capacity=2)
    store.put("a", {"n": 1})
    store.put("b", {"n": 2})
    assert store.get("a") is not None
    store.put("c", {"n": 3})
    assert store.get("a") is not None
    assert store.get("b") is None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_the_agent_pool_reuses_one_instance_per_name(agent_env) -> None:
    """One agent per name, guarded, because a Strands agent holds conversation state."""
    _ = agent_env
    pool = AgentPool()
    first, first_lock = pool.acquire("copilot")
    second, second_lock = pool.acquire("copilot")
    assert first is second
    assert first_lock is second_lock
    assert pool.loaded() == ["copilot"]


def test_the_agent_pool_builds_a_lock_for_every_agent(agent_env) -> None:
    _ = agent_env
    pool = AgentPool()
    for name in ("copilot", "fraud", "credit"):
        pool.acquire(name)
    assert pool.loaded() == ["copilot", "credit", "fraud"]


def test_a_second_app_gets_its_own_store(agent_env) -> None:
    """The factory form is what makes the service testable in isolation."""
    _ = agent_env
    first = create_app()
    second = create_app()
    assert first.state.store is not second.state.store
