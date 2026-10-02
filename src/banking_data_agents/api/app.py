"""The Copilot HTTP API.

A thin transport over the agent layer. Everything an answer needs — the number,
the SQL, the metrics, the contracts, the tool trace — is already in the
:class:`~banking_data_agents.agents.evidence.Evidence` envelope, so the API's job
is routing, concurrency and honest status codes, not logic.

Two decisions worth stating.

**One agent per (agent, worker) pair, guarded by a lock.** A Strands agent holds
conversation history in mutable state, so concurrent requests against one instance
would interleave two conversations. The lock makes that impossible; the pool makes
it cheap. There are no secrets in the answer path and no per-request state to leak.

**Answers are retained by trace id.** ``GET /answers/{trace_id}`` returns the exact
envelope a caller was given, which is what makes a dispute resolvable. The default
store is in-process and bounded — correct for a single task and honest about it:
:class:`AnswerStore` is the seam where a DynamoDB table goes in production.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from banking_data_agents import __version__
from banking_data_agents.agents import AGENTS, get_agent
from banking_data_agents.agents.base import BaseAgent
from banking_data_agents.audit import DynamoDBAnswerStore, answer_store_configured
from banking_data_agents.catalog.registry import get_catalog
from banking_data_agents.config import get_settings
from banking_data_agents.llm import describe_model
from banking_data_agents.logging_setup import configure_logging, get_logger
from banking_data_agents.pipeline.lake import get_lake

logger = get_logger(__name__)

#: The longest question worth planning. Beyond this the request is a document.
MAX_QUESTION_CHARS = 2_000

#: How many answers to keep for audit lookup in-process.
DEFAULT_RETENTION = 256


# ---------------------------------------------------------------------------
# Answer store
# ---------------------------------------------------------------------------
class AnswerStore(Protocol):
    """Where answered envelopes are kept so they can be produced on demand."""

    def put(self, trace_id: str, payload: dict[str, Any]) -> None: ...
    def get(self, trace_id: str) -> dict[str, Any] | None: ...


class InMemoryAnswerStore:
    """A bounded, in-process store. Replaced by DynamoDB in production.

    ``OrderedDict`` with move-to-end on read gives a small LRU: a busy service
    retains what is being asked about rather than what was asked about first.
    """

    def __init__(self, capacity: int = DEFAULT_RETENTION) -> None:
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def put(self, trace_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._items[trace_id] = payload
            self._items.move_to_end(trace_id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._items.get(trace_id)
            if payload is not None:
                self._items.move_to_end(trace_id)
            return payload

    def __len__(self) -> int:
        return len(self._items)


def default_answer_store() -> AnswerStore:
    """Retain to DynamoDB when configured, otherwise in-process.

    The switch is a setting rather than a code path: the same envelope, the same
    trace id, the same ``/answers/{trace_id}`` lookup either way.
    """
    if answer_store_configured():
        try:
            return DynamoDBAnswerStore()
        except Exception as error:  # pragma: no cover - misconfiguration
            logger.warning("answer_store_unavailable", error=str(error)[:300])
    return InMemoryAnswerStore()


# ---------------------------------------------------------------------------
# Agent pool
# ---------------------------------------------------------------------------
class AgentPool:
    """One guarded agent instance per name, built on first use."""

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, name: str) -> tuple[BaseAgent, threading.Lock]:
        with self._guard:
            if name not in self._agents:
                self._agents[name] = get_agent(name)
                self._locks[name] = threading.Lock()
            return self._agents[name], self._locks[name]

    def loaded(self) -> list[str]:
        return sorted(self._agents)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    agent: str = Field(default="copilot", description="copilot | fraud | credit")


class AskResponse(BaseModel):
    """The evidence envelope. Every field exists to make the answer auditable."""

    question: str
    answer: str
    trace_id: str
    outcome: str
    sql: str | None = None
    metrics: list[str] = []
    products: list[str] = []
    tables: list[str] = []
    row_count: int | None = None
    columns: list[str] = []
    preview: list[list[Any]] = []
    quality: dict[str, Any] | None = None
    lineage: dict[str, Any] | None = None
    governance: dict[str, Any] | None = None
    needs_clarification: dict[str, Any] | None = None
    missing_metrics: list[str] = []
    warnings: list[str] = []
    trace: list[dict[str, Any]] = []
    tools_called: list[str] = []
    contract_digest: str = ""
    asof: str = ""
    engine: str = ""
    provider: str = ""
    model_id: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    refused: bool = False
    answered_from_data: bool = False
    is_complete: bool = False


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def create_app(*, store: AnswerStore | None = None) -> FastAPI:
    """Build the application. Factory form so tests get an isolated store."""
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Banking Data Product Agents",
        version=__version__,
        description=(
            "Governed analytics agents over published banking data products. Every answer "
            "carries the SQL, the versioned metrics and the contracts it was built from."
        ),
    )
    app.state.pool = AgentPool()
    app.state.store = store or default_answer_store()

    @app.get("/health", summary="Liveness, provider and catalog status")
    def health() -> dict[str, Any]:
        catalog = get_catalog()
        try:
            status = catalog.status()
        except Exception as error:
            return JSONResponse(  # type: ignore[return-value]
                status_code=503,
                content={"status": "degraded", "error": str(error)[:300]},
            )
        return {
            "status": "ok" if not status["degraded"] else "degraded",
            "env": settings.env,
            "provider": settings.llm_provider,
            "model": describe_model(),
            "lake": type(get_lake()).__name__,
            "endpoint": settings.aws_endpoint_url or "aws",
            "products": status["products"],
            "degraded_products": status["degraded"],
            "metrics": status["metrics"],
            "contract_digest": status["contract_digest"],
            "agents": sorted(AGENTS),
            "answer_store": type(app.state.store).__name__,
            "answers_retained": len(app.state.store),
        }

    @app.get("/products", summary="The published data products")
    def products() -> dict[str, Any]:
        rows = get_catalog().list_products()
        return {"count": len(rows), "contract_digest": get_catalog().digest, "products": rows}

    @app.get("/products/{product}", summary="One contract, with its quality and SLA")
    def product(product: str) -> dict[str, Any]:
        catalog = get_catalog()
        try:
            contract = catalog.get_contract(product)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {
            "product": contract.product,
            "version": contract.version,
            "owner": contract.owner,
            "steward": contract.steward,
            "description": " ".join(contract.description.split()),
            "grain": contract.grain,
            "primary_key": list(contract.primary_key),
            "classification": contract.classification,
            "sla": {
                "freshness_hours": contract.sla.freshness_hours,
                "availability": contract.sla.availability,
                "row_count_floor": contract.sla.row_count_floor,
            },
            "allowed_use": list(contract.allowed_use),
            "not_allowed_use": list(contract.not_allowed_use),
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "unit": column.unit,
                    "enum": column.enum,
                    "pii": column.pii,
                    "metric_ref": column.metric_ref,
                    "not_allowed_use": list(column.not_allowed_use),
                }
                for column in contract.columns
            ],
            "quality": catalog.quality_summary(contract.product),
            "sla_status": catalog.sla(contract.product),
        }

    @app.get("/quality", summary="Platform-wide quality and freshness")
    def quality() -> dict[str, Any]:
        return get_catalog().quality_summary(None)

    @app.get("/agents", summary="Available agents and their tool surfaces")
    def agents() -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        for name in sorted(AGENTS):
            worker, _lock = app.state.pool.acquire(name)
            out.append(
                {
                    "agent": name,
                    "tier": worker.tier,
                    "tools": worker.tool_names,
                    "forbidden_dimensions": sorted(worker.ctx.forbidden_dimensions),
                }
            )
        return {"count": len(out), "agents": out}

    @app.post("/ask", response_model=AskResponse, summary="Ask an agent a question")
    def ask(request: AskRequest) -> AskResponse:
        if request.agent not in AGENTS:
            raise HTTPException(
                status_code=400,
                detail=f"unknown agent '{request.agent}'. Available: {', '.join(sorted(AGENTS))}",
            )
        worker, lock = app.state.pool.acquire(request.agent)
        started = time.perf_counter()
        # A Strands agent keeps conversation history in mutable state, so two
        # requests must never share one instance at the same time.
        with lock:
            envelope = worker.ask(request.question)
            worker.reset()

        payload = envelope.to_dict()
        # The agent name is what makes a retained envelope attributable to the
        # surface that produced it; without it a stored answer is anonymous.
        payload["agent"] = request.agent
        app.state.store.put(envelope.trace_id, payload)
        logger.info(
            "api_answered",
            agent=request.agent,
            trace_id=envelope.trace_id,
            outcome=envelope.outcome,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return AskResponse(**{key: payload.get(key) for key in AskResponse.model_fields})

    @app.get("/answers/{trace_id}", summary="Replay an answer by trace id")
    def answer(trace_id: str) -> dict[str, Any]:
        payload = app.state.store.get(trace_id)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"no answer recorded for trace {trace_id}")
        return payload

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        """Give every request a correlation id, so logs and answers can be joined."""
        request_id_value = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["x-request-id"] = request_id_value
        return response

    return app


app = create_app()


def get_app() -> FastAPI:
    """Dependency-usable accessor (and the uvicorn import target)."""
    return app


__all__ = [
    "AgentPool",
    "AnswerStore",
    "AskRequest",
    "AskResponse",
    "InMemoryAnswerStore",
    "app",
    "create_app",
    "default_answer_store",
]
