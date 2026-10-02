"""``bda ask`` — put a question to an agent from the terminal.

The CLI is the fastest way to see the whole platform work, and it is deliberately
thin: build an agent, ask it, render the envelope. Anything cleverer would make it
a worse test of the layer beneath it.
"""

from __future__ import annotations

import sys

from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)


def ask_once(
    question: str,
    *,
    show_sql: bool = True,
    json_out: bool = False,
    agent: str = "copilot",
) -> int:
    """Ask one question. Returns 0 when the agent produced a complete answer.

    A non-zero exit for an incomplete answer is what lets CI treat "the agent
    shrugged" as a failure rather than a log line.
    """
    from banking_data_agents.agents import get_agent

    worker = get_agent(agent)
    if not json_out:
        print(f"[bda] agent={worker.name} provider={worker.provider} model={worker.model_id}", file=sys.stderr)
        print(f"[bda] tools={len(worker.tool_names)}: {', '.join(worker.tool_names)}", file=sys.stderr)
        print("", file=sys.stderr)

    envelope = worker.ask(question)

    # Retention is best effort and off by default: a laptop run has no table to
    # write to, and an audit write must never fail an answer that already exists.
    from banking_data_agents.audit import record_answer

    recorded = record_answer(envelope, agent=agent)

    if json_out:
        print(envelope.to_json())
    else:
        print(envelope.render(show_sql=show_sql))
        print(
            f"\n[bda] {envelope.latency_ms:.0f} ms · "
            f"{envelope.input_tokens}+{envelope.output_tokens} tokens · trace {envelope.trace_id}",
            file=sys.stderr,
        )
        if recorded:
            print("[bda] answer retained; replay it with `bda answers get " + recorded + "`", file=sys.stderr)

    if not envelope.is_complete:
        print(
            "[bda] the agent did not produce a complete answer; see the evidence block above.",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = ["ask_once"]
