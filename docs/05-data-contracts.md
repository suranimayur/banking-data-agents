# 05 · Data contracts

## Concept

A table is a shape. A **contract** is a promise: what the columns mean, who owns
them, how fresh they are, and — the part everyone forgets — what the data may
**not** be used for.

An agent that tells an analyst "the answer is 372" has to be able to say which
version of which product produced it, and whether that product is allowed to
answer this question at all. That is only possible if the promise is written down
and versioned.

## The contract in these files

`contracts/*.yaml`, loaded by `catalog/contracts.py`:

```yaml
product: customer_360
version: 2.3.0
owner: customer-insights@bank.example
primary_key: [customer_id]
freshness_sla_hours: 24
columns:
  - name: customer_id
    type: string
    description: Stable identifier for the customer.
    pii: true
  - name: region
    type: string
    description: >
      State name (e.g. Maharashtra). The abbreviation lives in state_code.
  …
not_allowed_use:
  - marketing_targeting_on_credit_score
  - individual_credit_decisions_without_human_review
```

Published versions, verified:

| Product | Version | Grain |
|---------|---------|-------|
| `customer_360` | **2.3.0** | one row per customer |
| `transaction` | **1.6.0** | one row per transaction |
| `credit_risk` | **2.1.0** | one row per customer |

## Why `not_allowed_use` is enforced, not documented

Most catalogs have a "usage notes" field that nobody reads and nothing checks.
Here the field is an input to a decision: the tool `check_allowed_use` consults it,
and an agent that asks for a prohibited purpose gets `REFUSED` with the contract
and the clause named in the envelope. "How can I target customers for a marketing
campaign on their credit score?" is a test case, and it is expected to refuse.

This is the difference between governance as a document and governance as a
function. See [17 Security and compliance](17-security-and-compliance.md).

## Versioning

Versions move when the promise moves:

- **patch** — description or metadata only.
- **minor** — a new or nullable column.
- **major** — a semantic change (a unit changes, a column's meaning changes).

An answer cites `customer_360@2.3.0`, not "customer_360". When an analyst says a
number changed, the first question is whether the product version changed, and the
envelope answers it without a conversation.

## How contracts are used

| Consumer | What it does |
|----------|--------------|
| `publish` | Records the catalog entry: version, columns, quality, SLA, digest |
| `tools/impl.py::get_contract` | Returns the contract to the agent |
| `tools/impl.py::check_allowed_use` | Decides whether the purpose is permitted |
| `agents/evidence.py` | Stamps `contract_versions` and `contract_digest` on every answer |
| `/products/{product}` | Serves it to humans |
| `infra/stacks/catalog.py` | Publishes the catalog into the deployed environment |

`contract_digest` is a hash over the whole catalog, so two environments can be
compared cheaply and a "why did the number change" question has a mechanical first
step.

## Contracts and the answer

Every complete answer carries:

- the product **and its version**, for each product read;
- the **contract digest** of the catalog as it stood at answer time;
- the **quality state** of the tables touched, including warnings;
- the **SLA status**, so staleness is visible rather than assumed.

## A contract change, end to end

1. Edit `contracts/customer_360.yaml`, bump the version, add `not_allowed_use` if
   the new column brings a new restriction.
2. `uv run bda pipeline run` re-publishes.
3. `uv run bda eval` — the governance and version cases must still pass.
4. The API's `/products/customer_360` now reports the new version, and every new
   answer cites it.
5. CI's `eval-pr.yml` fails the pull request if the gate drops.

## Where this lands in production

Contracts are source-controlled, so a change is a pull request with an owner in
`.github/CODEOWNERS`. The catalog is published into the environment by
`infra/stacks/catalog.py`; the digest is visible on `/health` so a deployment can
be confirmed to have taken effect.

## Invariants a change must not break

1. Every published product has a version and an `owner`.
2. Removing a column is a **major** bump, never a rename in place.
3. `not_allowed_use` is consulted before data is touched, not after.
4. The contract digest changes if and only if a contract changed.
5. A product that fails its quality rules is not silently published.
