# 11 — Streamlit

**Level:** beginner → intermediate · **You will be able to:** build a data console
without writing front-end code, and recognise the execution model that trips
everyone up first.

## The idea

Building a front end has always meant a build step, a component tree, a state
library and a bundler. For an internal analytics tool, that is an enormous cost for
what is often a table, a chart and a text box.

Streamlit inverts it: **a script is an app.** You write Python top to bottom, and the
framework runs it, renders the widgets your code created, and re-runs the whole
script whenever a user interacts. No HTML, no JS, no routing, no build.

## The mental model

**The script re-runs from the top on every interaction.** This is the single fact
that explains every Streamlit behaviour, including the ones that feel like bugs.

```
   user types a question
        │
        ▼
   script re-executes from line 1
        │
        ├── widgets return their current values
        ├── st.session_state survives the re-run   ← your memory
        └── plain local variables do NOT survive     ← your memory leak
```

So there are exactly two things to master: what must persist goes in
`st.session_state`, and what is expensive gets a cache decorator.

## The minimum you need

```python
import streamlit as st

st.title("Analyst console")

# Cheap: recomputed every rerun, fine.
st.write("Ask a question")

with st.form("ask"):
    question = st.text_input("Question")
    submitted = st.form_submit_button("Ask")

if submitted:
    st.session_state.setdefault("messages", []).append(question)   # survives reruns

for m in st.session_state.messages:
    st.markdown(m)
```

Caching, and the distinction that matters:

```python
@st.cache_data(ttl=60)     # data: DataFrames, dicts, lists — copied per call, safe
def catalog(): ...

@st.cache_resource        # resources: clients, engines, connections — shared
def engine(): ...
```

Using `cache_resource` for mutable data gives you shared-state bugs across users;
using `cache_data` for a connection pool gives you a new pool per call. Getting this
pair backwards is the most common Streamlit performance bug.

## In this project

`ui/console.py` is the analyst console, and its governing decision is stated in its
own docstring: **it renders the same evidence envelope the API returns and the CLI
prints.** It is not a second implementation of truth.

Concretely:

- **Cached, cheap reads.** `catalog()` and friends use
  `@st.cache_data(ttl=60, show_spinner=False)`, with a longer TTL (300s) for the
  slowest — the catalogue changes only when the pipeline runs, so re-reading it per
  rerun would be pure waste.
- **Session state as the transcript.** `st.session_state.messages` holds the
  conversation; each entry stores the question, the answer and the whole envelope.
  A rerun from any interaction reconstructs the page without re-asking the model —
  which matters, because re-asking is a *cost*, not just a delay.
- **Explicit reset.** Clearing the transcript assigns `messages = []` and calls
  `st.rerun()`. Without the rerun, the UI would still show the old messages, because
  the current pass already rendered them.
- **No re-derivation.** The console never computes a number. It displays the
  envelope's row preview, capped at `MAX_PREVIEW_ROWS = 50`, and shows the SQL, the
  metric versions, the contract versions and the quality state alongside. That is
  the entire "trust" design: the reader can see where the figure came from.
- **Suggestions that demonstrate, not decorate.** `SUGGESTIONS` maps styled labels
  (with Material icons) to starter questions, chosen so that **each one exercises a
  different guarantee** — a plain aggregate, a ranking, a metadata question, a
  quality question, a lineage question, and a plain `REFUSED`
  ("Can I target customers for a marketing campaign on their credit score?"). The
  console doubles as a demonstration of the platform.

That last idea is worth stealing: a demo UI whose buttons each prove a different
property does more for trust than a page of prose.

## Level up

**Widgets are re-created every run.** Keys matter: give widgets stable `key=` values
and Streamlit can preserve their state correctly across reruns.

**`st.rerun()` is control flow, not a refresh button.** It aborts the current pass
and starts a new one, which is why it belongs immediately after mutating session
state.

**Long work needs progress.** A multi-second agent call should show a spinner or a
status region, or users click twice and pay twice.

**Secrets have a home.** `.streamlit/secrets.toml` is gitignored here (see
`.gitignore`), which is the framework's intended place for credentials — never in
the script and never in the repo.

**What to learn next:** `st.fragment` for partial reruns (the real fix for slow
pages), `st.dataframe` column configuration, `st.status`/`st.spinner` for long calls,
and multipage apps via a `pages/` directory.

## Try it

```bash
cd banking-data-agents
uv run bda ui            # opens the analyst console
# or: uv run streamlit run src/banking_data_agents/ui/console.py

uv run pytest tests/unit/test_console.py -q
```

Then try the `REFUSED` suggestion ("Can I target customers for a marketing campaign
on their credit score?") and read the governance decision the console shows beside it
— that is the whole thesis of the project in one click.
