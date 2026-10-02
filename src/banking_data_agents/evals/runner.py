"""The evaluation harness.

Why this exists: a platform that claims its agent is trustworthy has to be able to
say what it measured. This module turns the suite in :mod:`cases` into a score, a
gate and an artefact.

Two design choices carry the weight.

**Failures are named assertions, not a pass rate alone.** When a case fails you get
"expected metric.customer_count@1.0.0, got none" rather than a red cell. A score
tells you whether to ship; the assertion tells you what to fix.

**Policy cases cannot be averaged away.** Cases tagged ``governance`` or
``security``, and cases marked ``must_pass``, fail the run outright however good
the rest of the score is. A platform that refuses correctly 95% of the time is not
95% compliant; the other 5% is an incident.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from banking_data_agents.agents.evidence import Evidence
from banking_data_agents.agents.registry import AGENTS
from banking_data_agents.config import get_settings
from banking_data_agents.evals.cases import CRITICAL_TAGS, SUITES, Case, Expect
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class CaseResult:
    """One case, its answer, and every assertion it failed."""

    case: Case
    evidence: Evidence | None
    failures: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures and not self.error

    @property
    def critical(self) -> bool:
        return self.case.must_pass or bool(set(self.case.tags) & CRITICAL_TAGS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "agent": self.case.agent,
            "question": self.case.question,
            "passed": self.passed,
            "critical": self.critical,
            "weight": self.case.weight,
            "tags": list(self.case.tags),
            "failures": self.failures,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
            "outcome": self.evidence.outcome if self.evidence else None,
            "metrics": self.evidence.metrics if self.evidence else [],
            "products": self.evidence.products if self.evidence else [],
            "sql": self.evidence.sql if self.evidence else None,
            "answer": self.evidence.answer if self.evidence else None,
            "trace_id": self.evidence.trace_id if self.evidence else "",
        }


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
def evaluate_case(evidence: Evidence, expect: Expect) -> list[str]:
    """Check one envelope against its expectations.

    Returns the failed assertions, phrased so that reading them is enough to know
    what went wrong. Pure function: no agent, no lake, no clock.
    """
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    require(
        evidence.outcome == expect.outcome,
        f"outcome: expected {expect.outcome}, got {evidence.outcome}",
    )

    present = set(evidence.metrics)
    for metric in expect.metrics:
        require(metric in present, f"missing metric {metric} (have: {sorted(present) or 'none'})")
    for metric in expect.forbid_metrics:
        require(metric not in present, f"forbidden metric {metric} was used")

    sql = (evidence.sql or "").upper()
    for fragment in expect.sql_contains:
        if evidence.sql is None:
            failures.append(f"sql: expected a query containing {fragment!r}, but none was run")
            break
        require(fragment.upper() in sql, f"sql: missing {fragment!r}")
    for fragment in expect.sql_absent:
        require(fragment.upper() not in sql, f"sql: must not contain {fragment!r}")

    for table in expect.tables:
        require(table in evidence.tables, f"tables: {table} was not read (read: {evidence.tables})")

    called = set(evidence.tools_called)
    for tool in expect.tools:
        require(tool in called, f"tools: {tool} was never called (called: {sorted(called)})")
    for tool in expect.forbid_tools:
        require(tool not in called, f"tools: {tool} must not be called")

    if expect.min_rows is not None:
        rows = evidence.row_count if evidence.row_count is not None else 0
        require(rows >= expect.min_rows, f"rows: expected at least {expect.min_rows}, got {evidence.row_count}")
    if expect.max_rows is not None:
        rows = evidence.row_count if evidence.row_count is not None else 0
        require(rows <= expect.max_rows, f"rows: expected at most {expect.max_rows}, got {evidence.row_count}")

    answer = evidence.answer.lower()
    for phrase in expect.answer_contains:
        require(phrase.lower() in answer, f"answer: missing {phrase!r}")
    for phrase in expect.answer_absent:
        require(phrase.lower() not in answer, f"answer: must not contain {phrase!r}")

    if expect.quality:
        require(evidence.quality is not None, "quality: the answer carries no quality state")
    # A product citation is required when the answer is about data. A question with
    # no governed metric never reaches a product, so demanding a version there
    # would be demanding provenance for an answer that does not exist.
    if expect.cites_product and (evidence.answered_from_data or evidence.quality or evidence.lineage):
        require(
            any("@" in product for product in evidence.products),
            f"provenance: no product version cited (have: {evidence.products})",
        )

    # A refusal must be citable. "I can't do that" without a contract field or a
    # guardrail code is an opinion, and an unauditable refusal cannot be reviewed
    # or appealed.
    if expect.outcome == "REFUSED":
        governance = evidence.governance or {}
        require(bool(governance), "refusal: no governance decision was recorded")
        require(governance.get("allowed") is False, "refusal: the recorded decision is not a denial")
        require(bool(governance.get("reason")), "refusal: the denial carries no reason")

    # Every case, without exception, must carry the provenance that makes it
    # auditable. This is asserted globally rather than per case so a new case
    # cannot opt out of it by omission.
    require(bool(evidence.trace_id), "provenance: the answer has no trace id")
    require(bool(evidence.contract_digest), "provenance: the answer records no contract digest")
    require(evidence.is_complete, "incomplete: the agent produced no usable outcome")

    return failures


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
@contextmanager
def _lake_backend(backend: str | None):
    """Temporarily pin the lake backend (used by the emulator suite)."""
    if backend is None:
        yield
        return
    previous = os.environ.get("BDA_LAKE_BACKEND")
    os.environ["BDA_LAKE_BACKEND"] = backend
    from banking_data_agents.catalog.registry import reset_catalog
    from banking_data_agents.config import reset_settings_cache
    from banking_data_agents.tools.context import reset_engines

    reset_settings_cache()
    reset_catalog()
    reset_engines()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BDA_LAKE_BACKEND", None)
        else:
            os.environ["BDA_LAKE_BACKEND"] = previous
        reset_settings_cache()
        reset_catalog()
        reset_engines()


def run_suite(
    cases: tuple[Case, ...],
    *,
    agents: dict[str, Any] | None = None,
    verbose: bool = False,
) -> list[CaseResult]:
    """Ask every case and evaluate the answer."""
    pool = dict(agents or {})
    results: list[CaseResult] = []

    for case in cases:
        if case.agent not in pool:
            pool[case.agent] = AGENTS[case.agent]()
        worker = pool[case.agent]

        started = time.perf_counter()
        evidence: Evidence | None = None
        error = ""
        try:
            evidence = worker.ask(case.question)
        except Exception as failure:
            error = f"{type(failure).__name__}: {failure}"
            logger.error("eval_case_raised", case=case.id, error=error[:400])
        latency_ms = (time.perf_counter() - started) * 1000

        # A specialist keeps its conversation; resetting treats each case as a
        # fresh analyst rather than as a follow-up.
        reset = getattr(worker, "reset", None)
        if callable(reset):
            reset()

        failures = evaluate_case(evidence, case.expect) if evidence is not None else ["error: no evidence produced"]
        result = CaseResult(
            case=case,
            evidence=evidence,
            failures=failures,
            latency_ms=latency_ms,
            error=error,
        )
        results.append(result)
        if verbose and not result.passed:
            logger.warning("eval_case_failed", case=case.id, failures=result.failures)
    return results


def score(results: list[CaseResult]) -> float:
    """Weighted pass rate across the suite."""
    total = sum(result.case.weight for result in results)
    if total <= 0:
        return 1.0
    earned = sum(result.case.weight for result in results if result.passed)
    return round(earned / total, 4)


def gate(results: list[CaseResult], fail_under: float) -> tuple[bool, list[str]]:
    """Decide whether the run passes, and say why not.

    A critical failure fails the run regardless of the score. That is the whole
    point of separating critical cases: a governance refusal is not a data point
    to be averaged against a hundred good answers.
    """
    reasons: list[str] = []
    if fail_under > 0:
        value = score(results)
        if value < fail_under:
            reasons.append(f"score {value:.3f} is below the required {fail_under:.3f}")
    for result in results:
        if result.critical and not result.passed:
            reasons.append(f"critical case {result.case.id} failed: {'; '.join(result.failures) or result.error}")
    return (not reasons), reasons


def summarise(results: list[CaseResult]) -> dict[str, Any]:
    passed = [result for result in results if result.passed]
    return {
        "cases": len(results),
        "passed": len(passed),
        "failed": len(results) - len(passed),
        "critical_failed": len([result for result in results if result.critical and not result.passed]),
        "score": score(results),
        "latency_ms_mean": round(sum(result.latency_ms for result in results) / len(results), 1) if results else 0.0,
        "failed_ids": [result.case.id for result in results if not result.passed],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def write_report(
    results: list[CaseResult],
    *,
    suite: str,
    directory: Path | None = None,
    skipped: bool = False,
    reasons: list[str] | None = None,
) -> Path:
    """Persist the run as JSON plus a readable markdown summary."""
    settings = get_settings()
    target = directory or (settings.data_dir / "artifacts" / "evals")
    target.mkdir(parents=True, exist_ok=True)

    payload = {
        "suite": suite,
        "skipped": skipped,
        "generated_at": datetime.now(UTC).isoformat(),
        "gate_reasons": reasons or [],
        "provider": get_settings().llm_provider,
        "summary": summarise(results),
        "results": [result.as_dict() for result in results],
    }
    json_path = target / f"eval-report-{suite}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    (target / f"eval-report-{suite}.md").write_text(_markdown(results, suite), encoding="utf-8")
    return json_path


def _markdown(results: list[CaseResult], suite: str) -> str:
    summary = summarise(results)
    lines = [
        f"# Evaluation report — `{suite}`",
        "",
        f"Score **{summary['score']:.1%}** · "
        f"{summary['passed']}/{summary['cases']} cases passed · "
        f"{summary['critical_failed']} critical failures",
        "",
        "| case | agent | outcome | result | notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        outcome = result.evidence.outcome if result.evidence else "ERROR"
        status = "pass" if result.passed else ("**CRITICAL FAIL**" if result.critical else "fail")
        notes = "; ".join(result.failures) if result.failures else result.error
        lines.append(f"| `{result.case.id}` | {result.case.agent} | {outcome} | {status} | {notes} |")

    failing = [result for result in results if not result.passed]
    if failing:
        lines.extend(["", "## Failures in detail", ""])
        for result in failing:
            lines.append(f"### `{result.case.id}`")
            lines.append("")
            lines.append(f"- **question**: {result.case.question}")
            if result.case.rationale:
                lines.append(f"- **why it matters**: {result.case.rationale}")
            for failure in result.failures:
                lines.append(f"- {failure}")
            if result.error:
                lines.append(f"- error: {result.error}")
            if result.evidence is not None:
                lines.append(f"- sql: `{(result.evidence.sql or 'none').strip()}`")
                lines.append(f"- answer: {result.evidence.answer.splitlines()[0] if result.evidence.answer else ''}")
            lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_evals(
    suite: str = "deterministic",
    *,
    fail_under: float = 1.0,
    report: bool = True,
    verbose: bool = False,
) -> int:
    """Run a suite, print a summary, write a report and return an exit code.

    Exit codes: ``0`` pass, ``1`` gate failed, ``2`` unknown suite.
    """
    if suite not in SUITES:
        print(f"[eval] unknown suite '{suite}'. Available: {', '.join(sorted(SUITES))}")
        return 2

    cases = SUITES[suite]

    if suite == "floci":
        from banking_data_agents.aws import endpoint_reachable

        if not endpoint_reachable():
            print(
                "[eval] the floci suite needs the emulator, and it is not answering on "
                f"{get_settings().aws_endpoint_url or 'http://localhost:4566'}. "
                "Start it with `make floci-up`. Skipping; nothing was measured."
            )
            if report:
                write_report([], suite=suite, skipped=True, reasons=["emulator unreachable"])
            return 0

    backend = "s3" if suite == "floci" else None
    with _lake_backend(backend):
        results = run_suite(cases, verbose=verbose)

    ok, reasons = gate(results, fail_under)
    summary = summarise(results)

    # The eval score is a first-class metric, not only a CI log line: the quality
    # dashboard plots it over time, and that plot is what makes a slow drift visible
    # before it becomes a regression.
    from banking_data_agents.telemetry import EVAL_SCORE, emit

    emit(EVAL_SCORE, float(summary["score"]), unit="None", dimensions={"suite": suite})

    _print_summary(suite, results, summary, reasons)

    if report:
        path = write_report(results, suite=suite, reasons=reasons)
        print(f"[eval] report: {path}")

    return 0 if ok else 1


def _print_summary(suite: str, results: list[CaseResult], summary: dict[str, Any], reasons: list[str]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console(stderr=False)
        table = Table(title=f"eval suite: {suite}", show_lines=False)
        table.add_column("case", style="cyan", no_wrap=True)
        table.add_column("agent")
        table.add_column("outcome")
        table.add_column("result")
        for result in results:
            outcome = result.evidence.outcome if result.evidence else "ERROR"
            label = (
                "[green]pass[/green]"
                if result.passed
                else ("[bold red]CRITICAL FAIL[/bold red]" if result.critical else "[red]fail[/red]")
            )
            table.add_row(result.case.id, result.case.agent, outcome, label)
        console.print(table)
    except Exception:
        for result in results:
            print(f"  {'PASS' if result.passed else 'FAIL'}  {result.case.id}")

    print(
        f"[eval] score {summary['score']:.1%} · {summary['passed']}/{summary['cases']} passed · "
        f"{summary['critical_failed']} critical failures · mean {summary['latency_ms_mean']:.0f} ms"
    )
    for result in results:
        if not result.passed:
            for failure in result.failures or [result.error]:
                print(f"  - {result.case.id}: {failure}")
    for reason in reasons:
        print(f"[eval] GATE: {reason}")


__all__ = [
    "CaseResult",
    "evaluate_case",
    "gate",
    "run_evals",
    "run_suite",
    "score",
    "summarise",
    "write_report",
]
