# 01 — Python toolchain and `uv`

**Level:** beginner → advanced · **You will be able to:** reproduce any Python
environment byte-for-byte on any machine.

## The idea

Before packaging standards, "it works on my machine" was not a joke so much as a
description of the state of the art. You had three cooperating systems that each
kept their own opinion:

- **Which interpreter** runs the code (`python3.11` vs `3.12` behave differently).
- **Which packages** are installed, and at which versions.
- **How** those packages were fetched (from PyPI, from a Git URL, from a local path).

`pip` manages the second and third. Nothing standard manages the first. So teams
committed `requirements.txt`, hoped for the best, and discovered interpreter drift in
production — which is exactly when a `SyntaxError` on a newer syntax is most
expensive.

`uv` is a single fast tool that owns **all three**. It reads `pyproject.toml`,
resolves a lockfile, and can download a *specific* CPython version. That last part is
what makes this repository reproducible on a laptop that has never had Python 3.12.

## The mental model

Two files, two audiences:

```
pyproject.toml   ← what I want   (loose ranges, human intent, read by humans)
uv.lock          ← what I got    (exact versions + hashes, generated, read by uv)
```

`pyproject.toml` says `polars>=1.13` — a *range*. `uv.lock` says
`polars==1.44.2` with a SHA-256 of the wheel — a *fact*. Humans decide the range;
`uv` decides the fact; nothing else decides anything. When both people run
`uv sync`, they get the same fact, or they get an error, but never a silent
difference.

## The minimum you need

```bash
# Create/refresh the environment exactly as locked, including the interpreter.
uv sync

# Run something inside it. No `source .venv/bin/activate` required — ever.
uv run bda --help
uv run python -c "import polars; print(polars.__version__)"

# Add a dependency to the right group.
uv add httpx                    # runtime
uv add --dev pytest             # dev group
uv add --group infra aws-cdk-lib

# See what would change without changing it.
uv lock --check
```

`uv run` is the important habit. It resolves the environment *before* running and
fails loudly if the lockfile is stale, which is why this repository's `Makefile` and
CI files never activate a virtualenv.

## In this project

### Interpreter pinning

`pyproject.toml`:

```toml
requires-python = ">=3.12,<3.13"
```

The upper bound is deliberate. Strands, Pydantic and the CDK are all fast-moving; a
silent jump to 3.13 changes wheel availability for native extensions such as
Polars and DuckDB. The bound makes that a decision someone has to make on purpose.
On this machine `uv` provisions **CPython 3.12.12** even though the system `python`
is 3.14 — which is precisely the drift `uv` exists to remove.

### Dependency groups instead of optional extras

```toml
dependencies = [ ... ]          # what the app needs to run

[dependency-groups]
dev = ["pytest", "moto", "ruff", "mypy", ...]
infra = ["aws-cdk-lib", "constructs"]
```

Two groups, two jobs:

- **dev** — to test and lint. Installed in CI on every pull request.
- **infra** — only to *deploy*. `aws-cdk-lib` is a large dependency that an analyst
  running `bda ask` has no use for.

That split is why `tests/unit/test_infra.py` **skips** on a machine without the
`infra` group rather than failing. A green suite on a laptop without CDK is better
than an ambiguous red one. The same reasoning covers the `moto` group: `moto` is
dev-only, and `pyparsing` is listed next to it with a comment explaining that
`moto`'s Glue backend imports it lazily, so without it `mock_aws` fails at *call*
time, not import time — the kind of failure that looks like a bug in your code.

### The `src/` layout

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/banking_data_agents"]
```

Code lives in `src/banking_data_agents/`, not at the repository root. This is not
cosmetic. With a flat layout, `import banking_data_agents` can accidentally resolve
to the *source directory* instead of the *installed package*, so tests pass against
code that was never packaged — and then production imports a different file. The
`src/` layout makes that impossible: the only way to import the package is to
install it.

### The entry point

```toml
[project.scripts]
bda = "banking_data_agents.cli:main"
```

`uv sync` writes a `bda` executable into the environment, so `uv run bda ask "…"`
works without `python -m`. That is what CI calls, which means CI exercises the same
path a human does.

### Locking without locking

`uv.lock` is committed. `addopts` and every `uv run` verify it implicitly. In CI,
`.github/workflows/ci.yml` runs `uv sync --locked` so a stale lockfile fails the
build instead of being silently regenerated. Regenerating a lockfile is a decision,
not a side effect.

## Level up

**Determinism vs freshness.** A lockfile is only useful if you update it on purpose.
This repository's `dependabot.yml` opens grouped PRs weekly, so upgrades arrive as
reviewable diffs with a passing test suite attached. The alternative — floating
ranges — means the upgrade happens at 2 a.m. during a deploy instead.

**Reproducibility has a boundary.** `uv` pins Python packages. It does not pin the
Docker image, the operating system, or the AWS service behaviour. The other chapters
in this track each cover one of those boundaries.

**What to learn next:** PEP 621 (`[project]` metadata), PEP 735 (dependency groups),
and the difference between a *lockfile* (a fact about resolutions) and a *constraint*
(a rule about allowed resolutions).

## Try it

```bash
cd banking-data-agents
uv lock --check                       # is the lockfile in sync with pyproject?
uv run python -c "import sys; print(sys.version)"
uv sync --locked                      # exactly what CI does
uv run bda --help
```
