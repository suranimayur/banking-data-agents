"""The analyst console.

A Streamlit front end over the agent layer, for the audience the platform is
actually for: analysts, product owners and data stewards who will not read a JSON
envelope but still need to know where a number came from.

The design decision that matters is that the console renders the *same* evidence
envelope the API returns and the CLI prints. The SQL, the metric versions, the
contracts, the quality state and the tool timeline are all in the message, because
an analyst who cannot see the provenance of a figure has no way to defend it.

Run with ``bda ui`` or ``streamlit run src/banking_data_agents/ui/console.py``.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import streamlit as st

from banking_data_agents import __version__
from banking_data_agents.agents import AGENTS, get_agent
from banking_data_agents.agents.base import BaseAgent
from banking_data_agents.catalog.registry import get_catalog
from banking_data_agents.config import get_settings
from banking_data_agents.llm import describe_model
from banking_data_agents.logging_setup import configure_logging
from banking_data_agents.pipeline.lake import get_lake

MAX_PREVIEW_ROWS = 50

#: Starter questions. Each one exercises a different guarantee, so the console
#: doubles as a demonstration of the platform rather than only a way to ask.
SUGGESTIONS: dict[str, str] = {
    ":blue[:material/summarize:] How many customers do we have by region?": (
        "How many customers do we have by region?"
    ),
    ":violet[:material/leaderboard:] Who spends the most on cards?": "Who spends the most on cards?",
    ":orange[:material/label:] Show total balance by marketing segment": ("Show total balance by marketing segment"),
    ":green[:material/rule:] Is customer_360 data reliable?": "Is customer_360 data reliable?",
    ":gray[:material/account_tree:] Where does customer_360 come from?": ("Where does customer_360 come from?"),
    ":red[:material/gavel:] Can I target customers for a marketing campaign on their credit score?": (
        "How can I target customers for a marketing campaign on their credit score?"
    ),
    ":yellow[:material/help:] What is our exposure?": "What is our exposure?",
}


# ---------------------------------------------------------------------------
# Cached reads. The catalog and the agent roster change only when the pipeline
# runs, so they are cached; the answers themselves never are.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False)
def catalog_rows() -> list[dict[str, Any]]:
    return get_catalog().list_products()


@st.cache_data(ttl=60, show_spinner=False)
def platform_status() -> dict[str, Any]:
    return get_catalog().status()


@st.cache_data(ttl=300, show_spinner=False)
def agent_roster() -> dict[str, dict[str, Any]]:
    """Agent metadata. The agents themselves are built per question.

    A Strands agent keeps conversation history in mutable state, so a cached
    instance shared across browser sessions would let one analyst's question be
    answered with another's context. Building one per question costs nothing —
    the expensive part, the query engine, is cached process-wide already.
    """
    roster: dict[str, dict[str, Any]] = {}
    for name in sorted(AGENTS):
        worker = get_agent(name)
        roster[name] = {
            "tier": worker.tier,
            "tools": worker.tool_names,
            "forbidden_dimensions": sorted(worker.ctx.forbidden_dimensions),
            "summary": (worker.prompt.splitlines()[1] if len(worker.prompt.splitlines()) > 1 else "").strip(),
        }
    return roster


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
OUTCOME_STYLE: dict[str, tuple[str, str]] = {
    "ANSWERED_FROM_DATA": ("green", "answered from governed data"),
    "ANSWERED_FROM_METADATA": ("blue", "answered from metadata"),
    "REFUSED": ("red", "refused by policy"),
    "NEEDS_CLARIFICATION": ("orange", "needs clarification"),
    "NO_GOVERNED_METRIC": ("yellow", "no governed metric"),
    "INCOMPLETE": ("gray", "incomplete"),
}


def outcome_badge(evidence: dict[str, Any]) -> None:
    outcome = evidence.get("outcome", "INCOMPLETE")
    colour, label = OUTCOME_STYLE.get(outcome, ("gray", outcome))
    st.badge(label, color=colour)


def preview_frame(evidence: dict[str, Any]) -> pl.DataFrame | None:
    columns = evidence.get("columns") or []
    preview = evidence.get("preview") or []
    if not columns or not preview:
        return None
    try:
        return pl.DataFrame(preview, schema=columns, orient="row")
    except Exception:
        return None


def render_result(evidence: dict[str, Any]) -> None:
    """Show the data itself, when a query ran."""
    frame = preview_frame(evidence)
    if frame is None:
        return

    st.dataframe(frame.head(MAX_PREVIEW_ROWS), width="stretch")
    if evidence.get("truncated"):
        st.caption(
            f"Showing the first {min(MAX_PREVIEW_ROWS, len(evidence['preview']))} of {evidence['row_count']} rows."
        )

    # A two-column grouped answer is a chart. Draw it without being asked; a
    # breakdown in a table is a table, but a breakdown is usually a comparison.
    columns = evidence.get("columns") or []
    if len(columns) == 2 and frame.height >= 2 and frame[columns[1]].dtype.is_numeric() and frame.height <= 60:
        st.bar_chart(frame, x=columns[0], y=columns[1], height=260)


def render_evidence(evidence: dict[str, Any]) -> None:
    """The provenance. Everything a reviewer needs to defend the number."""
    provenance = [
        ("outcome", evidence.get("outcome")),
        ("metrics", ", ".join(evidence.get("metrics") or [])),
        ("products", ", ".join(evidence.get("products") or [])),
        ("tables", ", ".join(evidence.get("tables") or [])),
        ("rows", evidence.get("row_count")),
        ("engine", evidence.get("engine")),
        ("as of", evidence.get("asof")),
        ("contract digest", (evidence.get("contract_digest") or "")[:12]),
        ("trace", evidence.get("trace_id")),
        ("model", f"{evidence.get('provider')} · {evidence.get('model_id')}"),
        ("latency", f"{evidence.get('latency_ms')} ms"),
        ("tokens", f"{evidence.get('input_tokens')} in / {evidence.get('output_tokens')} out"),
    ]
    rows = [(label, value) for label, value in provenance if value not in (None, "", [])]
    if rows:
        st.table({label: [value] for label, value in rows})

    quality = evidence.get("quality")
    if isinstance(quality, dict) and quality.get("total"):
        total = quality["total"]
        passed = quality.get("passed", 0)
        st.progress(
            passed / total,
            text=f"Data quality: {passed}/{total} rules passing"
            + (f" · {quality.get('critical_failures')} critical" if quality.get("critical_failures") else ""),
        )
        sla = quality.get("sla")
        if isinstance(sla, dict) and sla:
            st.caption(
                f"Freshness {sla.get('age_hours')}h against an SLA of {sla.get('sla_freshness_hours')}h "
                f"— {sla.get('status')}"
            )
        for failure in (quality.get("failures") or [])[:6]:
            st.warning(
                f"[{failure.get('severity')}] {failure.get('rule_id')} ({failure.get('dataset')}): "
                f"{failure.get('detail')}",
                icon=":material/warning:",
            )

    governance = evidence.get("governance")
    if isinstance(governance, dict) and governance:
        st.error(f"{governance.get('reason')}", icon=":material/gavel:")

    lineage = evidence.get("lineage")
    if isinstance(lineage, dict) and lineage.get("edges"):
        st.caption(
            f"{lineage.get('direction', 'upstream')} lineage: {lineage.get('edge_count')} edge(s) "
            f"from {', '.join(lineage.get('source_systems') or []) or 'unknown sources'}"
        )

    if evidence.get("sql"):
        st.code(evidence["sql"], language="sql")

    for note in evidence.get("warnings") or []:
        st.info(note, icon=":material/info:")


def render_trace(evidence: dict[str, Any]) -> None:
    """Replay the tool calls as a step timeline.

    The steps are rendered from the recorded envelope rather than live, because the
    envelope is the artefact under audit: what a reviewer sees here is exactly what
    the API returned, not a flattering re-render of it.
    """
    trace = evidence.get("trace") or []
    if not trace:
        return
    with st.status(f"Agent trace · {len(trace)} tool call(s)", type="compact", expanded=False):
        for entry in trace:
            with st.status(str(entry.get("tool")), type="step", expanded=False):
                arguments = entry.get("arguments") or {}
                if arguments:
                    st.caption("arguments: " + json.dumps(arguments, default=str)[:400])
                summary = entry.get("result_summary")
                if isinstance(summary, dict):
                    st.json(summary, expanded=False)
                elif summary not in (None, ""):
                    st.caption(str(summary)[:600])


def submitted_text(value: Any) -> str | None:
    """The text of a chat submission.

    ``st.chat_input`` returns a plain string, or a ``ChatInputValue`` once file or
    audio attachments are enabled. Both shapes reach here as ``Any``, so the
    distinction is handled once rather than at the call site.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    return str(text) if text else None


def render_message(message: dict[str, Any]) -> None:
    with st.chat_message(message["role"], avatar=":material/person:" if message["role"] == "user" else None):
        evidence = message.get("evidence")
        if message["role"] == "assistant" and isinstance(evidence, dict):
            agent_name = message.get("agent", "copilot")
            st.caption(f"{agent_name} · trace {evidence.get('trace_id', '')}")
            outcome_badge(evidence)
            st.markdown(evidence.get("answer") or "_(no answer produced)_")
            render_result(evidence)
            with st.expander("Evidence", expanded=not evidence.get("answered_from_data"), icon=":material/fact_check:"):
                render_evidence(evidence)
            render_trace(evidence)
        else:
            st.markdown(message["content"])


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Data Product Copilot",
    page_icon=":material/analytics:",
    layout="wide",
)

configure_logging()
settings = get_settings()

st.title("Data product Copilot")
st.caption(
    "Ask about the bank's published data products. Every answer cites the governed metrics, "
    "the contract versions and the SQL it was built from."
)

status = platform_status()

# --- sidebar --------------------------------------------------------------
with st.sidebar:
    st.subheader("Session", divider="gray")
    status_colour = "green" if status.get("degraded") == [] else "orange"
    st.badge("catalog healthy" if status.get("degraded") == [] else "degraded", color=status_colour)  # type: ignore[arg-type]
    st.caption(f"env `{settings.env}` · lake `{type(get_lake()).__name__}`")
    st.caption(f"model `{describe_model()}`")
    st.caption(f"{status.get('products', 0)} products · {status.get('metrics', 0)} metrics")
    st.caption(f"contracts `{(status.get('contract_digest') or '')[:12]}`")

    roster = agent_roster()
    agent_name = (
        st.segmented_control(
            "Agent",
            options=sorted(roster),
            default="copilot",
            selection_mode="single",
            help="Copilot answers analyst questions; the specialists have narrower mandates.",
        )
        or "copilot"
    )
    limits = roster[agent_name]
    st.caption(f"tier `{limits['tier']}` · {len(limits['tools'])} tools")
    if limits["forbidden_dimensions"]:
        st.caption("cannot group by: " + ", ".join(f"`{name}`" for name in limits["forbidden_dimensions"]))

    with st.expander("Published products", icon=":material/table:"):
        for product in catalog_rows():
            st.markdown(f"**{product['product']}** `{product['version']}`")
            st.caption(
                f"{product['owner']} · {product['grain']} · {product['row_count']:,} rows · "
                f"{product['metrics']} metrics · {product['status']}"
            )

    if st.button("Clear conversation", icon=":material/delete_sweep:", width="stretch"):
        st.session_state.messages = []
        st.rerun()

    st.caption(f"banking-data-agents {__version__}")

# --- conversation ---------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []

if not st.session_state.messages:
    st.markdown("##### Try one of these")
    picked = st.pills("Suggested questions", list(SUGGESTIONS), label_visibility="collapsed")
    pending = SUGGESTIONS.get(picked or "")
else:
    pending = None

for message in st.session_state.messages:
    render_message(message)

prompt = st.chat_input("Ask about the bank's data products", submit_mode="disable")
question = pending or submitted_text(prompt)

if question:
    st.session_state.messages.append({"role": "user", "content": question, "evidence": None})
    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.status(":shimmer[Answering]", type="compact", expanded=False) as status_box:
            st.write(f"Calling **{agent_name}** on `{describe_model()}`")
            worker: BaseAgent = get_agent(agent_name)
            envelope = worker.ask(question)
            status_box.update(
                label=f"Answered in {envelope.latency_ms:.0f} ms · {len(envelope.tools_called)} tool call(s)",
                state="complete",
            )
        payload = envelope.to_dict()
        outcome_badge(payload)
        st.markdown(payload.get("answer") or "")
        render_result(payload)
        with st.expander("Evidence", expanded=not payload.get("answered_from_data"), icon=":material/fact_check:"):
            render_evidence(payload)
        render_trace(payload)

    st.session_state.messages.append(
        {"role": "assistant", "content": payload.get("answer", ""), "evidence": payload, "agent": agent_name}
    )
    st.rerun()
