"""``bda answers`` — read back what an agent answered.

An evidence envelope is only worth assembling if it can be retrieved later. The
HTTP API exposes it at ``/answers/{trace_id}``; this is the same lookup from the
terminal, which is what an operator actually reaches for when a caller says
"the number I got yesterday was wrong".

The command is deliberately honest about *where* an answer lives. Retention is
opt-in (``BDA_ANSWER_TABLE``), so when it is off the command says so rather than
printing an empty result that reads like a lost answer.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)


def _store() -> Any:
    from banking_data_agents.audit import DynamoDBAnswerStore, answer_store_configured
    from banking_data_agents.config import get_settings

    settings = get_settings()
    if not answer_store_configured():
        print(
            "[bda] answer retention is off (set BDA_ANSWER_TABLE to enable it).\n"
            "[bda] locally, run: export BDA_ANSWER_TABLE=" + (settings.answer_table_name or "bda-local-answers"),
            file=sys.stderr,
        )
        return None
    try:
        return DynamoDBAnswerStore()
    except Exception as error:  # pragma: no cover - misconfiguration
        print(f"[bda] could not open the answer store: {error}", file=sys.stderr)
        return None


def answers_get(trace_id: str, *, as_json: bool = False) -> int:
    """Print the newest retained answer for a trace id. Returns 1 if not found."""
    store = _store()
    if store is None:
        return 2

    payload = store.get(trace_id)
    if payload is None:
        print(f"[bda] no retained answer for trace {trace_id}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"[bda] trace      {payload.get('trace_id', trace_id)}")
    print(f"[bda] agent      {payload.get('agent', '-')}")
    print(f"[bda] outcome    {payload.get('outcome', '-')}")
    print(f"[bda] asked_at   {payload.get('asked_at', '-')}")
    print(f"[bda] question   {payload.get('question', '-')}")
    if payload.get("product"):
        print(f"[bda] product    {payload['product']}")
    if payload.get("sql"):
        print("\n-- sql --")
        print(payload["sql"])
    if payload.get("answer"):
        print("\n-- answer --")
        print(payload["answer"])
    preview = payload.get("preview") or []
    if preview:
        print(f"\n-- preview ({len(preview)} rows) --")
        for row in preview[:20]:
            print(json.dumps(row, default=str))
    if payload.get("truncated_for_retention"):
        print("\n[bda] preview and trace were trimmed before retention (item size limit).", file=sys.stderr)
    return 0


def answers_list(limit: int = 20) -> int:
    """Print a summary of the most recently retained answers."""
    store = _store()
    if store is None:
        return 2

    rows = store.recent(limit=limit)
    if not rows:
        print("[bda] no retained answers yet.", file=sys.stderr)
        return 0

    print(f"[bda] {len(rows)} most recent retained answers")
    for row in rows:
        print(
            f"  {str(row.get('asked_at', '-'))[:19]}  "
            f"{row.get('outcome', '-')!s:<26} "
            f"{row.get('agent', '-')!s:<10} "
            f"{row.get('trace_id', '-')!s}  "
            f"{str(row.get('question', ''))[:60]}"
        )
    return 0


__all__ = ["answers_get", "answers_list"]
