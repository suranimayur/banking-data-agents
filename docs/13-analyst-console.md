# 13 · The analyst console

## Concept

The console is for the person who does not want to read SQL. It shows what the API
returns and **never re-derives truth**. If the console and the API could disagree
about a number, one of them is lying and nobody knows which.

So `ui/console.py` is a thin Streamlit client over the same surfaces the API
exposes: ask a question, read the answer, read the evidence behind it.

```bash
uv run bda ui --port 8501
```

## What it shows

- **Ask.** A question box, an agent selector, and the answer.
- **Evidence.** The outcome, the metrics and their owners, the products and their
  versions, the SQL, the quality state, the lineage, the tool trace and the cost —
  the envelope, rendered ([10](10-the-evidence-envelope.md)).
- **Catalog.** The published products, their contracts, their quality and their
  SLAs.
- **Governance.** A refusal is displayed as a refusal, with the clause that caused
  it, in the same place a number would be. An interface that hides refusals teaches
  analysts to work around the platform.

## Why Streamlit

The console is read-only and single-user-per-session, which is exactly Streamlit's
shape. A richer SPA would be more code for the same information. The tradeoff is
accepted knowingly: if the console needs to become a product, it becomes a client of
the API rather than a bigger Streamlit app.

`.streamlit/config.toml` pins the theme, so the console does not look different on
every laptop.

## Tests

`tests/unit/test_console.py` covers the rendering functions without a browser: an
answered envelope, a refusal, a clarification, and the catalog view. The rule the
tests encode is that the console displays whatever the envelope says — including
`truncated`, `quality` warnings and `sla_status`.

## Where this lands in production

`infra/stacks/ui.py` deploys the console alongside the API for the environment it
belongs to. It talks to that environment's gateway, so an analyst cannot
accidentally read staging data from the production console — which is a smaller
problem than it sounds right up until it isn't.

## Invariants a change must not break

1. The console never computes a number the API did not return.
2. A refusal, a clarification and a warning are displayed, not swallowed.
3. `truncated` is shown when the envelope says so.
4. The console reads only from the API for its environment.
