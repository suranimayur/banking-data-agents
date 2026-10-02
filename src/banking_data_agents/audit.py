"""Answer retention: the audit side of the evidence envelope.

The envelope is only worth assembling if it survives the request. This module is
where it survives: an answer is written to a DynamoDB table keyed by its trace id,
so ``GET /answers/{trace_id}`` can reproduce exactly what a caller was told —
months later, from a different process, after the task that produced it is gone.

Three decisions are deliberate.

**The payload is stored as one JSON string.** DynamoDB rejects Python floats, and
a result preview full of amounts is full of floats. Flattening the envelope into
DynamoDB attributes would mean writing a converter that has to be right for every
future field; storing the envelope's own JSON keeps it lossless by construction.
The handful of attributes alongside it (``outcome``, ``agent``, ``asked_at``) exist
so the table is queryable without parsing, and so operators can answer "how many
refusals this week" with a scan instead of a script.

**Writes are best effort and reads are exact.** Retention failing must never fail a
request that has already been answered — the analyst gets their number either way.
So :meth:`DynamoDBAnswerStore.put` logs and returns rather than raising, while
:meth:`get` raises on an operational error, because a silent 404 on an audit lookup
is worse than an error.

**The store is a seam, not a mock.** Locally and in tests the API keeps a bounded
in-process store; when ``BDA_ANSWER_TABLE`` is set, both ``bda ask`` and the API
write to the same table the CDK stack creates (``bda-<env>-answers``). Same
envelope, same keys, one code path per environment.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

#: The largest item DynamoDB will accept, with headroom for the wrapper attributes.
MAX_ITEM_BYTES = 400_000

#: How many days an answer is retained before the table's TTL removes it.
DEFAULT_TTL_DAYS = 400

#: Envelope fields that also become queryable attributes.
_INDEXED_FIELDS = ("outcome", "agent", "engine", "provider", "model_id", "refused")


def answer_store_configured() -> bool:
    """True when ``BDA_ANSWER_TABLE`` was set, i.e. this process should retain.

    Deliberately keyed on the variable rather than on :attr:`answer_table_name`:
    the name has a sensible default, but retention must be an explicit choice, or
    every laptop run would log failed writes against a table nobody created.
    """
    return bool(get_settings().bda_answer_table)


def _asked_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _expires_at(ttl_days: int) -> int:
    return int(time.time()) + ttl_days * 86_400


def _typed(value: Any) -> dict[str, Any]:
    """Encode one attribute for the low-level DynamoDB client.

    ``aws.dynamodb()`` is a low-level client, so every attribute is a tagged
    union (``{"S": ...}``) rather than a plain Python value. Doing this by hand
    keeps the module free of a boto3 resource and makes the wire format explicit
    at the one place that writes it.
    """
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    return {"S": value if isinstance(value, str) else str(value)}


def _item(trace_id: str, payload: dict[str, Any], *, agent: str = "", ttl_days: int = DEFAULT_TTL_DAYS) -> dict[str, Any]:
    """Shape one answer into a DynamoDB item.

    The JSON body is trimmed rather than the write being dropped when an envelope
    is unusually large (a 200-row preview plus a long trace can approach the
    400 KB item limit): losing the preview is survivable, losing the answer is not.
    """
    body = dict(payload)
    body.setdefault("trace_id", trace_id)
    if agent:
        body.setdefault("agent", agent)

    encoded = json.dumps(body, default=str)
    trimmed = False
    if len(encoded.encode("utf-8")) > MAX_ITEM_BYTES:
        body["preview"] = []
        body["trace"] = []
        body["truncated_for_retention"] = True
        encoded = json.dumps(body, default=str)
        trimmed = True

    item: dict[str, Any] = {
        "trace_id": trace_id,
        "asked_at": str(body.get("asked_at") or _asked_at()),
        "expires_at": _expires_at(ttl_days),
        "payload": encoded,
        "trimmed": trimmed,
    }
    question = str(body.get("question") or "")[:500]
    if question:
        item["question"] = question
    for field in _INDEXED_FIELDS:
        value = body.get(field)
        if value is not None:
            item[field] = value

    # DynamoDB rejects floats and zero-length strings anywhere in an item. Coerce
    # anything unstructured to text and drop empties, then tag every value for the
    # low-level client.
    encoded_item: dict[str, Any] = {}
    for key, value in item.items():
        value = value if isinstance(value, (str, int, bool)) else str(value)
        if isinstance(value, str) and not value:
            continue
        encoded_item[key] = _typed(value)
    return encoded_item


class DynamoDBAnswerStore:
    """The answer store behind ``/answers/{trace_id}`` in a deployed environment."""

    def __init__(
        self,
        table_name: str | None = None,
        *,
        client: Any | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
    ) -> None:
        from banking_data_agents import aws

        self.table_name = table_name or get_settings().answer_table_name
        if not self.table_name:
            raise ValueError("no answer table configured: set BDA_ANSWER_TABLE")
        self._client = client if client is not None else aws.dynamodb()
        self._ttl_days = ttl_days
        #: Counts writes that failed, so ``/health`` can report a store that is
        #: silently losing answers instead of implying retention works.
        self.failures = 0

    # -- writes -------------------------------------------------------------
    def put(self, trace_id: str, payload: dict[str, Any]) -> None:
        """Retain an answer. Never raises: the answer already reached the caller."""
        agent = str(payload.get("agent") or "")
        try:
            self._client.put_item(TableName=self.table_name, Item=_item(trace_id, payload, agent=agent, ttl_days=self._ttl_days))
        except Exception as error:  # pragma: no cover - depends on a real outage
            self.failures += 1
            logger.warning(
                "answer_retention_failed",
                table=self.table_name,
                trace_id=trace_id,
                failures=self.failures,
                error=str(error)[:300],
            )

    # -- reads --------------------------------------------------------------
    def get(self, trace_id: str) -> dict[str, Any] | None:
        """The newest retained answer for a trace id, or ``None`` if never stored."""
        response = self._client.query(
            TableName=self.table_name,
            KeyConditionExpression="trace_id = :trace",
            ExpressionAttributeValues={":trace": {"S": trace_id}},
            # One trace id can legitimately be asked more than once (a retry after a
            # clarification); the newest is the one the caller is looking at.
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items") or []
        if not items:
            return None
        raw = items[0].get("payload")
        if raw is None:
            return None
        return json.loads(raw["S"] if isinstance(raw, dict) else raw)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """The newest retained answers, newest first.

        A scan rather than a query: "show me what the agents have been asked"
        has no partition key to work from, and the table is TTL-bounded. The
        payload is dropped and only its summary attributes are returned, which
        is what an operator actually reads.
        """
        response = self._client.scan(TableName=self.table_name, Limit=max(1, limit))
        items = response.get("Items") or []
        summaries: list[dict[str, Any]] = []
        for item in items:
            summaries.append(
                {
                    key: (value["S"] if isinstance(value, dict) and "S" in value else value)
                    for key, value in item.items()
                    if key != "payload"
                }
            )
        summaries.sort(key=lambda row: str(row.get("asked_at", "")), reverse=True)
        return summaries

    def count(self, *, trace_id: str | None = None) -> int:
        """How many answers are retained (optionally for one trace id)."""
        if trace_id:
            return int(
                self._client.query(
                    TableName=self.table_name,
                    KeyConditionExpression="trace_id = :trace",
                    ExpressionAttributeValues={":trace": {"S": trace_id}},
                    Select="COUNT",
                ).get("Count", 0)
            )
        return int(self._client.scan(TableName=self.table_name, Select="COUNT").get("Count", 0))

    def __len__(self) -> int:
        return self.count()


# ---------------------------------------------------------------------------
# Provisioning (local, Floci, and tests)
# ---------------------------------------------------------------------------
def ensure_answer_store(table_name: str | None = None) -> str:
    """Create the answers table if it does not exist, and return its name.

    CDK owns this table in ``dev``/``staging``/``prod``; this exists so a laptop
    or an emulator can run the same code path without a deployment. The key schema
    mirrors the stack exactly — ``trace_id`` partition, ``asked_at`` sort, TTL on
    ``expires_at`` — because a table that differs from production is a test that
    proves nothing.
    """
    from banking_data_agents import aws

    name = table_name or get_settings().answer_table_name
    if not name:
        raise ValueError("no answer table configured: set BDA_ANSWER_TABLE")

    client = aws.dynamodb()
    try:
        client.describe_table(TableName=name)
        return name
    except client.exceptions.ResourceNotFoundException:
        pass
    except Exception as error:  # pragma: no cover - transport failures
        if "ResourceNotFound" not in str(error):
            raise

    client.create_table(
        TableName=name,
        AttributeDefinitions=[
            {"AttributeName": "trace_id", "AttributeType": "S"},
            {"AttributeName": "asked_at", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "trace_id", "KeyType": "HASH"},
            {"AttributeName": "asked_at", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=name)
    logger.info("answer_store_created", table=name)
    return name


def record_answer(envelope: Any, *, agent: str = "", store: Any | None = None) -> str | None:
    """Retain one agent answer, if retention is configured.

    Returns the trace id when the answer was stored, ``None`` when retention is
    switched off — the CLI says which, so "where is my answer?" is answerable
    rather than a guess.
    """
    trace_id = str(getattr(envelope, "trace_id", "") or "")
    if not trace_id:
        return None

    payload = envelope.to_dict() if hasattr(envelope, "to_dict") else dict(envelope)
    if agent:
        payload["agent"] = agent

    if store is None:
        if not answer_store_configured():
            return None
        store = DynamoDBAnswerStore()
    store.put(trace_id, payload)
    return trace_id


__all__ = [
    "DEFAULT_TTL_DAYS",
    "MAX_ITEM_BYTES",
    "DynamoDBAnswerStore",
    "answer_store_configured",
    "ensure_answer_store",
    "record_answer",
]
