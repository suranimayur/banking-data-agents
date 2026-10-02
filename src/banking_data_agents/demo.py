"""End-to-end demonstration: generation, medallion, agents, refusals, gate.

``make demo`` runs this. It is the answer to "so what does it actually do", and it
is written to be honest rather than flattering: every number it prints comes from
running the real pipeline, the real tools and the real agents, and every step
records whether it succeeded.

What it walks through, in the order the platform itself works:

1. **Environment.** Where the lake lives, which model provider is answering, which
   model is used for which tier. A demo that does not say whether it is running
   against Floci or real Bedrock is a demo whose output cannot be interpreted.
2. **Generation.** Twelve months of synthetic banking data across six source
   systems, into a landing zone.
3. **The medallion run.** Bronze, silver, gold and the publication of contracts,
   metrics and lineage. Not a mock: the same code path CI runs.
4. **The governed metadata surface.** The catalog and quality tools, called directly,
   so the output is what the agent sees rather than a summary of it.
5. **The Copilot.** A metric question, a quality question, a lineage question, a
   refusal and an ambiguity — because the refusals and the ambiguity handling are
   the features, and a demo that only shows the happy path would be selling the
   wrong thing.
6. **The two specialist agents.** One fraud triage and one credit assessment.
7. **The evaluation gate.** The same deterministic suite that blocks a merge.

Exit code 0 means every step passed. A failure is reported with the step that
failed, and the demo keeps going where it can, because a demo that stops at the
first problem tells you less than one that reports all of them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from banking_data_agents.aws import describe_endpoint
from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

console = Console()

#: The questions the demo asks the Copilot. Chosen so that between them they exercise
#: every outcome the evidence envelope can carry.
COPILOT_SCENARIOS: tuple[tuple[str, str, str], ...] = (
    (
        "metric",
        "How many customers are in each customer segment?",
        "A governed metric question: should resolve metrics, compile SQL and run it.",
    ),
    (
        "quality",
        "What is the data quality status of the customer_360 product?",
        "A metadata question: answers from the quality rules rather than from a query.",
    ),
    (
        "lineage",
        "Where does the transaction product get its data from?",
        "A metadata question: answers from the published lineage, not from the rows.",
    ),
    (
        "refusal",
        "Which customers have bad credit scores so we can target them for card offers?",
        "A prohibited use: the credit_risk contract forbids marketing targeting.",
    ),
    (
        "ambiguity",
        "What is the average balance?",
        "An ambiguous metric: several definitions compete, so the agent must ask.",
    ),
)

#: One question per specialist. Short on purpose: the point is the shape of the
#: answer and its guardrails, not a full investigation.
SPECIALIST_SCENARIOS: tuple[tuple[str, str], ...] = (
    ("fraud", "Triage the recent flagged card transactions and tell me which pattern they share."),
    ("credit", "Assess the credit risk of the portfolio and tell me which segment drives it."),
)


@dataclass
class Step:
    """One demonstration step and what came of it."""

    name: str
    ok: bool
    detail: str = ""
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)


class Demo:
    """Runs the steps, renders them, and remembers what went wrong."""

    def __init__(self, *, skip_data: bool = False) -> None:
        self.skip_data = skip_data
        self.steps: list[Step] = []
        self.settings = get_settings()

    # -- rendering -----------------------------------------------------------
    def banner(self) -> None:
        settings = self.settings
        rows = [
            ("environment", settings.env),
            ("lake backend", "local filesystem" if settings.is_local else "S3"),
            ("AWS endpoint", describe_endpoint()),
            ("LLM provider", settings.llm_provider),
            ("router model", settings.model_for("router")),
            ("reasoner model", settings.model_for("reasoner")),
            ("escalation model", settings.model_for("escalation")),
            ("data directory", str(settings.data_dir)),
        ]
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", justify="right")
        table.add_column()
        for key, value in rows:
            table.add_row(key, str(value))
        console.print(
            Panel(
                table,
                title="[bold]Banking Data Agents[/bold]",
                subtitle="governed data products, agentic answers, every claim evidenced",
                border_style="cyan",
            )
        )

    def step(self, name: str, action: Callable[[], str]) -> Step:
        """Run one step, timing it and capturing the failure rather than raising."""
        started = time.perf_counter()
        console.rule(f"[bold]{name}[/bold]", style="dim")
        try:
            detail = action()
            ok = True
        except Exception as error:  # a demo reports failures; it does not abort on them
            logger.debug("demo_step_failed", step=name, error=str(error)[:300])
            detail = f"{type(error).__name__}: {error}"
            ok = False
        duration = time.perf_counter() - started
        result = Step(name=name, ok=ok, detail=detail, duration_s=duration)
        marker = "[green]ok[/green]" if ok else "[red]FAILED[/red]"
        console.print(f"  {marker}  {detail}  [dim]({duration:.1f}s)[/dim]")
        self.steps.append(result)
        return result

    # -- steps ---------------------------------------------------------------
    def generate_data(self) -> str:
        from banking_data_agents.datagen.generate import generate_all

        if self.skip_data:
            landing = self.settings.landing_dir
            if not landing.exists():
                raise RuntimeError(f"--skip-data was requested but {landing} does not exist")
            return f"skipped generation; reusing {self._landing_summary(landing)}"

        code = generate_all()
        if code != 0:
            raise RuntimeError(f"generate_all returned {code}")
        return self._landing_summary(self.settings.landing_dir)

    @staticmethod
    def _landing_summary(landing: Path) -> str:
        files = list(landing.rglob("*.parquet"))
        total_bytes = sum(path.stat().st_size for path in files)
        return f"{len(files)} datasets, {total_bytes / 1024 / 1024:.1f} MiB in {landing}"

    def run_pipeline(self) -> str:
        from banking_data_agents.pipeline.runner import run_pipeline

        code = run_pipeline()
        if code != 0:
            raise RuntimeError(f"the medallion run returned {code}")

        # Report the lake as the platform reports it: through the governed tool the
        # agent itself uses, not by reaching into storage from the demo.
        quality = self._tool("explain_quality", product="customer_360")
        return (
            f"bronze -> silver -> gold -> publish complete; customer_360 quality: "
            f"{quality.get('passed', '?')}/{quality.get('total', '?')} pass, "
            f"{quality.get('warning_failures', '?')} warn, "
            f"{quality.get('failures', '?')} fail "
            f"({quality.get('critical_failures', '?')} critical)"
        )

    def _tool(self, name: str, **arguments: Any) -> dict[str, Any]:
        """Call a governed tool directly, the same way the agent does."""
        from banking_data_agents.tools.context import ToolContext
        from banking_data_agents.tools.impl import (
            explain_quality,
            get_contract,
            list_products,
            search_catalog,
            trace_lineage,
        )

        # Annotated as a homogeneous callable map so that indexing it and calling the
        # result type-checks: the point of the demo is to call the real tools, not to
        # bypass the type checker on the way.
        registry: dict[str, Callable[..., dict[str, Any]]] = {
            "explain_quality": explain_quality,
            "trace_lineage": trace_lineage,
            "list_products": list_products,
            "get_contract": get_contract,
            "search_catalog": search_catalog,
        }
        if name not in registry:
            raise KeyError(f"unknown tool {name}")

        context = ToolContext()
        payload = registry[name](context, **arguments)
        # Tools record their calls on the context; keeping the full outputs means a
        # later step can read untruncated results rather than the preview.
        return payload if isinstance(payload, dict) else {"result": payload}

    def show_catalog(self) -> str:
        payload = self._tool("list_products")
        products = payload.get("products") or []
        if not products:
            raise RuntimeError("the catalog is empty; did the publish stage run?")

        table = Table(title="Data products", show_lines=False)
        table.add_column("product", style="bold")
        table.add_column("version")
        table.add_column("owner")
        table.add_column("owner contact")
        for product in products:
            table.add_row(
                str(product.get("name", "?")),
                str(product.get("version", "?")),
                str(product.get("owner", "-")),
                str(product.get("owner_contact", product.get("steward_contact", "-"))),
            )
        console.print(table)
        return f"{len(products)} published products"

    def show_lineage(self) -> str:
        payload = self._tool("trace_lineage", product="customer_360", direction="upstream")
        edges = payload.get("edges") or payload.get("lineage") or []
        if not edges:
            raise RuntimeError("no lineage edges were published")

        table = Table(title="customer_360 lineage (upstream)", show_lines=False)
        table.add_column("depth", justify="right")
        table.add_column("transform")
        table.add_column("dataset")
        for edge in edges[:12]:
            table.add_row(
                str(edge.get("depth", "?")),
                str(edge.get("via", edge.get("transform_id", "?"))),
                str(edge.get("dataset", "?")),
            )
        console.print(table)
        if len(edges) > 12:
            console.print(f"  [dim]... and {len(edges) - 12} more edges[/dim]")
        return f"{len(edges)} lineage edges"

    def ask_copilot(self) -> str:
        from banking_data_agents.agents import get_agent

        agent = get_agent("copilot")
        outcomes: list[str] = []
        failures: list[str] = []

        for label, question, expectation in COPILOT_SCENARIOS:
            console.print()
            console.print(f"[bold]{label}[/bold]: {question}")
            console.print(f"[dim]{expectation}[/dim]")
            envelope = agent.ask(question)
            self._render_envelope(envelope)
            outcomes.append(envelope.outcome)
            if not envelope.is_complete:
                failures.append(label)

        if failures:
            raise RuntimeError(f"the Copilot did not resolve: {', '.join(failures)}")
        return "outcomes: " + ", ".join(outcomes)

    def _render_envelope(self, envelope: Any) -> None:
        """Print the parts of an evidence envelope that make it an evidence envelope."""
        colour = {
            "ANSWERED_FROM_DATA": "green",
            "ANSWERED_FROM_METADATA": "green",
            "REFUSED": "yellow",
            "NEEDS_CLARIFICATION": "yellow",
            "NO_GOVERNED_METRIC": "yellow",
        }.get(envelope.outcome, "red")

        console.print(f"  outcome: [{colour}]{envelope.outcome}[/{colour}]  [dim]({envelope.latency_ms:.0f} ms)[/dim]")
        answer = (envelope.answer or "").strip().replace("\n", " ")
        if answer:
            console.print(f"  answer: {answer[:400]}")

        provenance: list[str] = []
        if envelope.products:
            provenance.append("products: " + ", ".join(envelope.products))
        if envelope.metrics:
            provenance.append("metrics: " + ", ".join(envelope.metrics))
        if envelope.row_count is not None:
            provenance.append(f"rows: {envelope.row_count}")
        if envelope.tables:
            provenance.append("tables: " + ", ".join(envelope.tables))
        for line in provenance:
            console.print(f"  [dim]{line}[/dim]")

        if envelope.sql:
            console.print("  sql:")
            for line in envelope.sql.strip().splitlines():
                console.print(f"    [dim]{line}[/dim]")
        if envelope.governance:
            console.print(f"  [yellow]governance: {envelope.governance}[/yellow]")
        if envelope.needs_clarification:
            console.print(f"  [yellow]clarification: {envelope.needs_clarification}[/yellow]")
        if envelope.missing_metrics:
            console.print(f"  [yellow]no governed metric for: {envelope.missing_metrics}[/yellow]")
        if envelope.warnings:
            console.print(f"  [yellow]warnings: {envelope.warnings}[/yellow]")

    def ask_specialists(self) -> str:
        from banking_data_agents.agents import get_agent

        results: list[str] = []
        for agent_name, question in SPECIALIST_SCENARIOS:
            console.print()
            console.print(f"[bold]{agent_name}[/bold]: {question}")
            envelope = get_agent(agent_name).ask(question)
            self._render_envelope(envelope)
            if not envelope.is_complete:
                raise RuntimeError(f"the {agent_name} agent did not resolve its question")
            results.append(f"{agent_name}={envelope.outcome}")
        return ", ".join(results)

    def run_eval_gate(self) -> str:
        import json

        from banking_data_agents.evals.runner import run_evals

        # 0.97 is the "no regression beyond 3%" rule, expressed against the
        # deterministic suite's 100% baseline.
        code = run_evals(suite="deterministic", fail_under=0.97, report=True)
        report_path = self.settings.data_dir / "artifacts" / "evals" / "eval-report-deterministic.json"
        summary: dict[str, Any] = {}
        if report_path.exists():
            summary = json.loads(report_path.read_text(encoding="utf-8")).get("summary", {})

        score = summary.get("score")
        if code != 0 or (score is not None and score < 0.97):
            raise RuntimeError(f"the eval gate did not pass (exit {code}, score {score})")
        if score is None:
            return "the eval gate passed (no report was written)"
        return (
            f"eval gate passed: {summary.get('passed', '?')}/{summary.get('cases', '?')} "
            f"cases, score {score * 100:.1f}%, "
            f"{summary.get('critical_failed', 0)} critical failures"
        )

    # -- reporting -----------------------------------------------------------
    def report(self) -> int:
        console.print()
        table = Table(title="End-to-end result", show_lines=False)
        table.add_column("step")
        table.add_column("result", justify="center")
        table.add_column("detail")
        table.add_column("time", justify="right")
        for step in self.steps:
            table.add_row(
                step.name,
                "[green]ok[/green]" if step.ok else "[red]FAILED[/red]",
                step.detail[:90],
                f"{step.duration_s:.1f}s",
            )
        console.print(table)

        failed = [step for step in self.steps if not step.ok]
        total = sum(step.duration_s for step in self.steps)
        if failed:
            console.print(f"\n[red]{len(failed)} step(s) failed after {total:.1f}s.[/red]")
            return 1

        console.print(f"\n[green]All {len(self.steps)} steps passed in {total:.1f}s.[/green]")
        console.print(
            Panel(
                "Next: [bold]make api[/bold] to serve the Copilot, [bold]make ui[/bold] for the "
                "analyst console,\n[bold]make synth[/bold] to see the infrastructure, and "
                "[bold]docs/[/bold] for the service-by-service guide.",
                title="Where to go next",
                border_style="dim",
            )
        )
        return 0


def run_demo(*, skip_data: bool = False) -> int:
    """Run the demonstration. Returns a process exit code."""
    demo = Demo(skip_data=skip_data)
    demo.banner()

    demo.step("1. Synthetic source data", demo.generate_data)
    demo.step("2. Medallion run and data quality", demo.run_pipeline)
    demo.step("3. Published catalog", demo.show_catalog)
    demo.step("4. Published lineage", demo.show_lineage)
    demo.step("5. Data Product Copilot", demo.ask_copilot)
    demo.step("6. Fraud and credit agents", demo.ask_specialists)
    demo.step("7. Evaluation gate", demo.run_eval_gate)

    return demo.report()


__all__ = ["COPILOT_SCENARIOS", "SPECIALIST_SCENARIOS", "Demo", "run_demo"]
