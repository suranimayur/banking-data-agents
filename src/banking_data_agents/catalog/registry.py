"""The runtime catalog.

Everything an agent needs to answer "what do we have, can I use it, and can I
trust it?" — products, contracts, metrics, quality, lineage and SLA — behind one
object. The agent tools are thin wrappers over this, which keeps the tool layer
free of business logic.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import polars as pl

from banking_data_agents.catalog.contracts import (
    ProductContract,
    contract_digest,
    load_contracts,
)
from banking_data_agents.logging_setup import get_logger
from banking_data_agents.pipeline.lake import Lake, get_lake
from banking_data_agents.semantic.metrics import (
    Metric,
    ResolutionResult,
    metrics_for_product,
    resolve_metric,
)

logger = get_logger(__name__)

_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "to",
    "in",
    "on",
    "by",
    "with",
    "show",
    "me",
    "what",
    "is",
    "are",
    "how",
    "many",
    "much",
    "give",
    "list",
    "all",
}


def _singular(token: str) -> str:
    """Crude singularisation so "customers" matches the product `customer_360`.

    Without it a search for "how many customers by region" ties between every
    product whose description mentions customers, and the alphabetical tie-break
    picks the wrong one. A wrong guess at a stem simply fails to match, which is
    the safe direction.
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text: str) -> set[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return {_singular(token) for token in cleaned.split() if token and token not in _STOPWORDS and len(token) > 2}


class Catalog:
    """Read-only view over the contracts, metrics and operational metadata."""

    def __init__(self, lake: Lake | None = None) -> None:
        self._lake = lake

    @property
    def lake(self) -> Lake:
        if self._lake is None:
            self._lake = get_lake()
        return self._lake

    # -- products ------------------------------------------------------------
    @property
    def contracts(self) -> dict[str, ProductContract]:
        return load_contracts()

    @property
    def digest(self) -> str:
        return contract_digest()

    def product_names(self) -> list[str]:
        return sorted(self.contracts)

    def get_contract(self, product: str) -> ProductContract:
        contracts = self.contracts
        key = product.strip().lower()
        if key in contracts:
            return contracts[key]
        # Tolerate "gold.customer_360", "customer-360" and similar.
        normalised = key.removeprefix("gold.").replace("-", "_").replace(" ", "_")
        if normalised in contracts:
            return contracts[normalised]
        raise KeyError(f"unknown data product '{product}'. Available: {', '.join(self.product_names())}")

    def _sla_rows(self) -> pl.DataFrame:
        try:
            return self.lake.read("ops", "sla_status")
        except FileNotFoundError:
            return pl.DataFrame(
                schema={
                    "product": pl.Utf8,
                    "row_count": pl.Int64,
                    "status": pl.Utf8,
                    "age_hours": pl.Float64,
                    "dq_critical_failures": pl.Int64,
                    "dq_warning_failures": pl.Int64,
                    "sla_freshness_hours": pl.Int64,
                }
            )

    def list_products(self) -> list[dict[str, Any]]:
        """One summary row per product, contract joined to live operational status."""
        sla = self._sla_rows()
        rows: list[dict[str, Any]] = []
        for name, contract in sorted(self.contracts.items()):
            status = sla.filter(pl.col("product") == name) if sla.height else None
            rows.append(
                {
                    "product": name,
                    "version": contract.version,
                    "domain": contract.domain,
                    "owner": contract.owner,
                    "grain": contract.grain,
                    "description": " ".join(contract.description.split()),
                    "classification": contract.classification,
                    "columns": len(contract.columns),
                    "metrics": len(metrics_for_product(name)),
                    "sla_freshness_hours": contract.sla.freshness_hours,
                    "row_count": int(status["row_count"][0]) if status is not None and status.height else 0,
                    "status": str(status["status"][0]) if status is not None and status.height else "UNKNOWN",
                    "age_hours": float(status["age_hours"][0])
                    if status is not None and status.height and status["age_hours"][0] is not None
                    else None,
                    "dq_critical_failures": int(status["dq_critical_failures"][0])
                    if status is not None and status.height
                    else 0,
                    "allowed_use": list(contract.allowed_use),
                    "not_allowed_use": list(contract.not_allowed_use),
                }
            )
        return rows

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Rank products against a free-text question.

        Scores on product name, domain, description, column names and metric
        names, so "who spends the most on cards" still finds customer_360 via the
        card_spend_90d column and metric.
        """
        needle = _tokens(query)
        if not needle:
            return self.list_products()[:limit]

        scored: list[tuple[float, dict[str, Any]]] = []
        for summary in self.list_products():
            contract = self.contracts[summary["product"]]
            haystack = " ".join(
                [
                    contract.product.replace("_", " "),
                    contract.domain,
                    contract.description,
                    " ".join(contract.column_names),
                    " ".join(metric.name for metric in metrics_for_product(contract.product)),
                    " ".join(contract.allowed_use),
                ]
            )
            overlap = needle & _tokens(haystack)
            score = len(overlap) / len(needle)
            # A name match is a stronger signal than incidental description overlap.
            if _tokens(contract.product) & needle:
                score += 0.4
            if score > 0:
                scored.append(
                    (score, {**summary, "match_score": round(min(score, 1.0), 3), "matched_terms": sorted(overlap)})
                )

        scored.sort(key=lambda item: (-item[0], item[1]["product"]))
        return [summary for _, summary in scored[:limit]]

    # -- metrics -------------------------------------------------------------
    def metrics_for(self, product: str) -> list[Metric]:
        return metrics_for_product(self.get_contract(product).product)

    def resolve(self, term: str, product: str | None = None) -> ResolutionResult:
        return resolve_metric(term, product=product)

    def resolve_many(self, terms: list[str], product: str | None = None) -> list[ResolutionResult]:
        return [resolve_metric(term, product=product) for term in terms]

    # -- trust signals -------------------------------------------------------
    def quality(self, product: str | None = None) -> pl.DataFrame:
        try:
            results = self.lake.read("ops", "dq_results")
        except FileNotFoundError:
            return pl.DataFrame()
        if product is None:
            return results
        return results.filter(
            pl.col("dataset").is_in([f"gold.{product}", f"silver.{product}", f"bronze.{product}"])
            | pl.col("rule_text").str.contains(product, literal=True)
        )

    def quality_summary(self, product: str | None = None) -> dict[str, Any]:
        results = self.quality(product)
        if not results.height:
            return {"total": 0, "passed": 0, "failures": [], "note": "no quality results recorded yet"}
        failures = results.filter(pl.col("status") != "PASS")
        return {
            "total": results.height,
            "passed": int(results.filter(pl.col("status") == "PASS").height),
            "critical_failures": int(failures.filter(pl.col("severity") == "critical").height)
            if failures.height
            else 0,
            "warning_failures": int(failures.filter(pl.col("severity") == "warning").height) if failures.height else 0,
            "failures": [
                {
                    "dataset": row["dataset"],
                    "rule_id": row["rule_id"],
                    "severity": row["severity"],
                    "status": row["status"],
                    "rule_text": row["rule_text"],
                    "detail": row["detail"],
                }
                for row in failures.head(20).iter_rows(named=True)
            ],
        }

    def sla(self, product: str | None = None) -> list[dict[str, Any]]:
        sla = self._sla_rows()
        if not sla.height:
            return []
        if product:
            sla = sla.filter(pl.col("product") == product)
        return sla.to_dicts()

    def lineage(self, product: str, direction: str = "upstream", depth: int = 8) -> list[dict[str, Any]]:
        from banking_data_agents.pipeline.publish import trace

        dataset = product if "." in product else f"gold.{product}"
        return trace(self.lake, dataset, direction=direction, depth=depth)

    def upstream_sources(self, product: str) -> list[str]:
        """Source systems that ultimately feed a product (for lineage answers)."""
        routes = self.lineage(product, direction="upstream")
        return sorted({r["dataset"] for r in routes if r["dataset"].startswith("bronze.")})

    def usability(self, product: str, column: str, intended_use: str) -> tuple[bool, str]:
        return self.get_contract(product).usability(column, intended_use)

    def status(self) -> dict[str, Any]:
        """Small health block used by the API and the console header."""
        products = self.list_products()
        return {
            "products": len(products),
            "degraded": [p["product"] for p in products if p["status"] != "OK"],
            "contract_digest": self.digest,
            "metrics": len({m.name for p in self.contracts.values() for m in self.metrics_for(p.product)}),
        }


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    """Process-wide catalog singleton."""
    return Catalog()


def reset_catalog() -> None:
    get_catalog.cache_clear()


__all__ = ["Catalog", "get_catalog", "reset_catalog"]
