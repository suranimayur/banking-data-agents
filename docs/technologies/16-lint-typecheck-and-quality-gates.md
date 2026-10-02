# 16 — Lint, types and quality gates

**Level:** intermediate → advanced · **You will be able to:** explain what Ruff and
mypy each buy, and why a strict gate is cheaper than a review argument.

## The idea

Code review is expensive human attention, and it is routinely spent on things a
machine could have caught: an unused import, a missing type, an unsorted import
block, a mutable default. Every minute spent on those is a minute not spent on
whether the design is right.

Two tools move that work left.

**Ruff** is a linter and formatter — it is extraordinarily fast (Rust, and it
replaces Flake8, isort, pyupgrade, bandit and more). It catches the mechanical
mistakes.

**mypy** is a static type checker. It analyses annotations *without running code* and
finds a class of bugs that tests usually miss: `None` where a value was assumed, a
wrong argument name, a return type that does not match every branch.

## The mental model

```
           cheap ──────────────────────────────► expensive
    formatter   linter      type checker      tests      review      production
     Ruff fmt    Ruff        mypy             pytest     humans      customers
     "layout"    "smells"    "shape"          "behaviour" "design"   "regret"
```

Each stage should be strict enough that the next stage is not doing its job. A human
reviewer asking "did you mean `Optional`?" means the type checker is too lax.

## The minimum you need

```bash
uv run ruff check .          # lint
uv run ruff check --fix .    # autofix what is safe
uv run ruff format .         # format
uv run mypy src              # type check
```

## In this project

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
src = ["src", "tests", "infra"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]
ignore = ["E501"]

[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
warn_unused_ignores = true
no_implicit_optional = true
files = ["src/banking_data_agents", "infra"]
```

Each decision is deliberate:

- **`src = ["src", "tests", "infra"]`** — with the note that *"`infra` is included so
  the deployment code is held to the same lint rules as the application. It is
  excluded from the wheel, not from review."* Deployment code is the code most likely
  to be written once and never revisited, which is exactly why it needs the most
  mechanical help.
- **`E501` ignored, `line-length = 120`** — the formatter owns line length; a linter
  duplicating it produces noise without value.
- **`warn_unused_ignores = true`** — the important one. A `type: ignore` that is no
  longer needed becomes a **failure**, so suppressions cannot silently accumulate and
  hide real errors later.
- **`no_implicit_optional`** — `def f(x: str = None)` is an error, so `Optional` must
  be explicit.
- **`ignore_missing_imports = true`** — needed because `aws-cdk-lib` lives in an
  optional dependency group; a type check with only `dev` installed must not fail on
  a missing CDK stub. This is the type-checking consequence of the dependency-group
  decision in [chapter 01](01-python-toolchain-and-uv.md).

### Current state

```
ruff check .           All checks passed!
mypy src               Success: no issues found in 54 source files
```

Both hold across `src`, `tests` and `infra`. Real fixes made during development, to
show the tools are load-bearing rather than decorative: three import-ordering
violations (`I`), and two **stale `type: ignore` comments** in `ui/console.py` and
`api/app.py` that `warn_unused_ignores` surfaced — both of which were masking
questions about whether the underlying annotation was still right.

### The gates in CI

`ci.yml` runs lint and types on every pull request, so a style debate never happens
in review. That is the actual benefit: not cleaner code in the abstract, but **review
attention redirected to design**.

## Level up

**A strict gate on an existing codebase needs a ratchet.** Turning on `--strict`
mypy across 50k lines produces thousands of errors and no progress. The technique is
to make the current error count the baseline and fail only on an increase — and to
fix the `warn_unused_ignores` errors first, because deleting dead suppressions often
reveals real bugs underneath.

**`# type: ignore` is a debt with an address.** Always include the error code
(`# type: ignore[arg-type]`) so a *different* error appearing on that line is not
silently swallowed.

**Types are documentation that cannot go stale.** In this repository the `QueryEngine`
and `AnswerStore` Protocols are the clearest statement of the architectural seam in
[chapter 03](03-duckdb.md) — clearer than prose, because mypy verifies every
implementation against them.

**What to learn next:** mypy `--strict` and per-module overrides for a gradual
migration; `Protocol` vs `ABC` (structural vs nominal typing — this project uses
`Protocol`); and Ruff's per-file ignores, used here for one `tests/**` rule where
assertions are legitimately not production code.

## Try it

```bash
cd banking-data-agents
uv run ruff check .
uv run mypy src

# See the config actually doing something: add a stale ignore and watch it fail.
uv run python - <<'PY'
import pathlib
p = pathlib.Path("src/banking_data_agents/config.py")
s = p.read_text(encoding="utf-8")
print("suppressions in tree:", sum(
    1 for f in pathlib.Path("src").rglob("*.py")
    for line in f.read_text(encoding="utf-8").splitlines()
    if "type: ignore" in line
))
PY
```
