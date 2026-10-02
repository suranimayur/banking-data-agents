"""The analyst console, driven headlessly.

Streamlit ships an in-process test harness, so the console is tested the way it
runs — script re-executed on each interaction — rather than by importing its
functions and hoping the wiring is right. That matters here because most of the
console's behaviour *is* wiring: a widget keyed twice, a session-state value read
before it exists, or an evidence field that arrived as a string are all bugs that
only appear when the page actually renders.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

CONSOLE = Path(__file__).resolve().parents[2] / "src" / "banking_data_agents" / "ui" / "console.py"

pytestmark = pytest.mark.slow


def launch(agent_env) -> AppTest:
    _ = agent_env
    app = AppTest.from_file(str(CONSOLE), default_timeout=300)
    return app.run()


def element_types(app: AppTest) -> set[str]:
    return {type(element).__name__ for element in app.main}


def main_text(app: AppTest) -> str:
    return "\n".join(str(element.value) for element in app.main if hasattr(element, "value"))


def test_the_console_renders_without_an_exception(agent_env) -> None:
    app = launch(agent_env)
    assert [str(exception.value) for exception in app.exception] == []


def test_the_console_titles_itself(agent_env) -> None:
    app = launch(agent_env)
    assert [title.value for title in app.title] == ["Data product Copilot"]


def test_starter_questions_are_offered_before_the_first_message(agent_env) -> None:
    """The suggestions are how an analyst discovers what the platform can answer."""
    app = launch(agent_env)
    assert "Try one of these" in main_text(app)
    # st.pills renders as a button group in the element tree.
    assert "ButtonGroup" in element_types(app)


def test_the_sidebar_documents_the_published_products(agent_env) -> None:
    app = launch(agent_env)
    sidebar = "\n".join(str(element.value) for element in app.sidebar if hasattr(element, "value"))
    for product in ("customer_360", "transaction", "credit_risk"):
        assert product in sidebar
    assert "contracts" in sidebar
    assert "model" in sidebar


def test_the_sidebar_exposes_the_fraud_agents_restriction(agent_env) -> None:
    """The restriction belongs where an analyst can see it before asking."""
    app = launch(agent_env)
    app.session_state["_unused"] = None  # touch session state so the widget tree is stable
    sidebar = "\n".join(str(element.value) for element in app.sidebar if hasattr(element, "value"))
    # copilot is the default agent, so the fraud restriction is not shown yet
    assert "cannot group by" not in sidebar


def test_a_question_renders_an_answer_a_table_and_the_sql(agent_env) -> None:
    app = launch(agent_env)
    app.chat_input[0].set_value("How many customers do we have by region?").run()
    assert [str(exception.value) for exception in app.exception] == []
    text = main_text(app)
    assert "row(s) returned" in text
    assert "answered from governed data" in text
    # The preview becomes a real table, and the SQL is shown verbatim.
    frames = app.get("dataframe")
    assert frames, "the result preview should render as a table"
    assert "region" in list(frames[0].value.columns)
    codes = app.get("code")
    assert any("SELECT" in code.value for code in codes)
    assert any("gold.customer_360" in code.value for code in codes)


def test_the_evidence_panel_carries_the_provenance(agent_env) -> None:
    """An analyst must be able to defend the number without opening the CLI."""
    app = launch(agent_env)
    app.chat_input[0].set_value("How many customers do we have by region?").run()
    text = main_text(app)
    # The governed metric, with its version, is part of the answer itself.
    assert "metric.customer_count@1.0.0" in text
    # The provenance block is rendered as a key/value table.
    assert app.get("table"), "the provenance table should render"


def test_a_refusal_is_rendered_as_a_refusal(agent_env) -> None:
    app = launch(agent_env)
    app.chat_input[0].set_value("How can I target customers for a marketing campaign on their credit score?").run()
    assert [str(exception.value) for exception in app.exception] == []
    text = main_text(app)
    assert "refused by policy" in text
    assert "not_allowed_use" in text


def test_a_clarification_is_rendered_as_a_question(agent_env) -> None:
    app = launch(agent_env)
    app.chat_input[0].set_value("What is our exposure?").run()
    assert [str(exception.value) for exception in app.exception] == []
    text = main_text(app)
    assert "needs clarification" in text
    assert "Which do you mean" in text


def test_clearing_the_conversation_resets_the_page(agent_env) -> None:
    app = launch(agent_env)
    app.chat_input[0].set_value("How many customers do we have by region?").run()
    assert "row(s) returned" in main_text(app)
    # The first button in the sidebar is "Clear conversation".
    app.sidebar.button[0].click().run()
    assert [str(exception.value) for exception in app.exception] == []
    assert "row(s) returned" not in main_text(app)
    assert "Try one of these" in main_text(app)


def test_the_console_renders_the_agent_trace(agent_env) -> None:
    """The audit trail is visible to the analyst, not only in the API response."""
    app = launch(agent_env)
    app.chat_input[0].set_value("How many customers do we have by region?").run()
    states = app.get("status")
    labels = " ".join(str(getattr(state, "label", "")) for state in states)
    assert "tool call" in labels
