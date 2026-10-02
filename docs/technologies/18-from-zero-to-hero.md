# 18 — From zero to hero

**Level:** capstone · **You will be able to:** describe how every technology in this
track composes, and rebuild the system from first principles.

## The one idea

Strip the repository down and this is what remains:

> **A language model is allowed to choose *which* governed aggregate to compute. It
> is never allowed to decide *how* to compute it, or *whether* it may.**

Everything else is machinery in service of that sentence. Read the stack as a
sequence of consequences, and it stops being a list of technologies:

```
   "the model must not invent numbers"
        │
        ├── so aggregates must be pre-defined, versioned and owned
        │        → the semantic layer (28 metric fragments)
        │        → contracts (a product is a promise)
        │
        ├── so the model must not write free-form SQL
        │        → the deterministic planner over metric fragments
        │        → the SQL guard, enforced below the model
        │
        ├── so the answer must be defensible
        │        → the evidence envelope
        │        → answer retention (so it is replayable months later)
        │
        └── so this must be provable, not merely claimed
                 → the evaluation gate, tests, telemetry, CDK, CI
```

Each arrow is a technology decision that was *forced* by the one above it. That is
the real lesson of the track: **the stack is not a list of tools someone liked, it is
the shape of an argument.**

## The stack as a sentence

Reading the layers from the bottom up, each built on the one below:

| Layer | Technology | The claim it makes possible |
|-------|-----------|------------------------------|
| Environment | `uv` + CPython 3.12 | The same code behaves the same everywhere |
| Data format | Parquet + Arrow | Scanning is cheap enough to be interactive |
| Compute | Polars, DuckDB | Aggregations run in seconds locally |
| Structure | SQLGlot | SQL can be *inspected* rather than trusted |
| Seam | `QueryEngine` / `Lake` Protocols | Local and cloud differ by configuration, not code |
| Cloud | S3, Glue, Athena, Lake Formation | The same numbers, governed, at scale |
| Custody | DynamoDB, KMS, Secrets Manager | Answers outlive the request; secrets never leak |
| Intelligence | Strands + Bedrock | The model *chooses*; deterministic code *computes* |
| Guard | `sqlguard` + Bedrock Guardrails | Refusal is enforced, not requested |
| Provenance | Evidence envelope | Every figure can be defended |
| Surface | FastAPI, Streamlit | Humans and systems get the same truth |
| Delivery | CDK, GitHub Actions, OIDC | Provisioned and deployed with no stored secrets |
| Assurance | pytest, Ruff, mypy, eval gate, EMF | The claims are verified, measured and alerted on |

## What to learn, in the order it pays off

If you were to rebuild this, the highest-value order is not the order the code was
written in. It is the order in which each step becomes *checkable*:

1. **Make it reproducible.** `uv` + a lockfile + `src/` layout. Without this, nothing
   you learn later is transferable, because none of it is repeatable.
2. **Make the data small and deterministic.** Synthetic data with a fixed seed and
   deliberate incidents ([the synthetic data chapter](../03-synthetic-data.md)). Every
   later claim is now reproducible in seconds, offline.
3. **Make the seam early.** Define `QueryEngine` and `Lake` as Protocols *before*
   choosing a backend. Retrofitting a seam is a rewrite; defining one is an afternoon.
4. **Make the vocabulary.** Versioned metric fragments with owners and one
   `sql_fragment` each. This is the single highest-leverage artifact in the whole
   system — it is what turns "the model wrote SQL" into "the model selected a
   sanctioned definition".
5. **Make the guard, then the agent.** Guard first, because it defines what the agent
   is *allowed* to attempt, and it is testable without a model at all.
6. **Make the envelope.** Provenance is a first-class return type, not a logging
   concern.
7. **Make the gate.** The eval suite turns every earlier promise into a build failure
   when broken.
8. **Only then make it cloud.** CDK, Athena, Bedrock. By this point, the cloud is a
   configuration change, because step 3 already happened.

That ordering is the practical content of "zero to hero", and it is worth noticing
that **the cloud is last**. Most projects start there, and that is why they cannot be
tested.

## Exercises that prove you understand it

Each one is a real change to a real seam, and each has a failure mode that teaches
something:

1. **Add a metric.** Add `avg_transaction_value` to `semantic/metrics/transaction.yaml`
   with an owner and a fragment. Then ask for "average transaction value" and confirm
   the planner selects it. *Teaches:* how the planner and the vocabulary interact.
2. **Break the guard, on purpose.** Comment out the `FORBIDDEN_DATASETS` check in
   `tools/sqlguard.py` and run `uv run bda eval`. *Teaches:* that the eval gate is
   load-bearing, because the suite should go red.
3. **Add an agent.** Write a `collections` agent with its own prompt, a narrower tool
   subset and a `forbidden_dimensions`. Register it in `agents/registry.py`.
   *Teaches:* how little there is to an agent once `BaseAgent` owns the machinery.
4. **Add a refusal.** Extend `not_allowed_use` on `customer_360` and add the matching
   eval case. *Teaches:* governance as a testable artifact rather than a policy PDF.
5. **Swap the engine.** Run the same question through `AthenaEngine`. *Teaches:* the
   value of the Protocol — and, honestly, the dialect differences the guard protects
   you from.
6. **Rename the world.** Change `qualify()` in `infra/config.py` and watch which
   tests fail. *Teaches:* how far the one naming convention reaches.

## Where this repository is honest about its gaps

A capstone should name what is not proven, because an undocumented gap is a lie of
omission:

- **The Docker-dependent paths are unverified in this environment.** The Docker daemon
  was not running, so Floci could not start, so the S3/Glue/Athena end-to-end paths
  and two integration tests did not execute. The code is written, synth-verified and
  covered offline by `moto`; it is *unexecuted*, not *unwritten*.
- **The agents ran on the deterministic stub provider.** Real Bedrock calls are wired
  and configured, but no token was spent here, so real-model behaviour — and the
  guardrails around it — is untested.
- **All data is synthetic.** No real customer record exists in this repository, in any
  commit, and none of the numbers describe a real bank.

Naming these is not a weakness in the project. **Stating the boundary of what you
verified is the difference between engineering and marketing**, and it is the same
discipline the evidence envelope applies to an answer.

## The closing idea

The technologies in this track are all replaceable: swap DuckDB for Trino, Strands
for a hand-rolled loop, Streamlit for a React app, CDK for Terraform. The system
survives all of those substitutions, because the value is not in the tools.

The value is in four properties, and they map exactly onto
[the four commitments](../01-architecture.md):

1. **The vocabulary is governed** — the model selects, it does not invent.
2. **The answer carries its evidence** — provenance is not optional.
3. **Enforcement lives below the model** — a prompt is a request, code is a fact.
4. **Nothing ships unmeasured** — a claim without a gate is a hope.

Learn those four and you can build this platform on any stack, in any language, in
any cloud. Learn only the tools and you can build today's version of it and no other.

---

**Back to:** [the technologies index](README.md) · [the main playbook](../README.md) ·
[the top-level README](../../README.md)
