"""Streamlit analyst console.

``banking_data_agents.ui.console`` is a page script, not a library: Streamlit runs
it top to bottom on every interaction. It is deliberately not imported by any
other module, so the agent layer never depends on the UI.

Start it with ``bda ui`` (which shells out to ``streamlit run``) so the port and
the theme in ``.streamlit/config.toml`` come along.
"""
