"""The evaluation harness.

``cases`` states what a correct answer looks like; ``runner`` decides whether the
platform produced one and writes the artefact CI gates on.

The suite runs against the deterministic stub model by default, so it is free,
offline and reproducible. ``bda eval --suite floci`` runs the same cases with the
lake pinned to the emulator, which is how the AWS data path — S3, Glue, Athena —
is proved without a cloud account.
"""

from banking_data_agents.evals.cases import SUITE, SUITES, Case, Expect
from banking_data_agents.evals.runner import (
    CaseResult,
    evaluate_case,
    gate,
    run_evals,
    run_suite,
    score,
    summarise,
)

__all__ = [
    "SUITE",
    "SUITES",
    "Case",
    "CaseResult",
    "Expect",
    "evaluate_case",
    "gate",
    "run_evals",
    "run_suite",
    "score",
    "summarise",
]
