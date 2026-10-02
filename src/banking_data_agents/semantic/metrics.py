"""Versioned metric registry and term resolution.

Two jobs:

1. **Registry** — every aggregate an agent may use, with a version, an owner, a
   definition and a SQL fragment.
2. **Resolution** — map a human term ("high value customer", "risk grade") onto
   registry entries, and *declare ambiguity* when more than one entry fits.

Ambiguity handling is a feature, not an edge case. Silently choosing one of three
competing definitions of "high value" is the most common way an analytics agent
produces a confidently wrong number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from banking_data_agents.config import get_settings
from banking_data_agents.logging_setup import get_logger

logger = get_logger(__name__)

#: Below this score a term is considered unresolved rather than guessed.
MIN_MATCH_SCORE = 0.45
#: Two candidates within this distance of the top score are ambiguous.
AMBIGUITY_MARGIN = 0.12


class MetricParameter(BaseModel):
    name: str
    type: str = "string"
    required: bool = False
    description: str = ""


class Metric(BaseModel):
    """One published aggregate."""

    name: str
    version: str
    owner: str = ""
    definition: str
    unit: str = "count"
    aggregate: str = "count"
    sql_fragment: str
    synonyms: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    note: str | None = None
    parameters: list[MetricParameter] = Field(default_factory=list)

    #: Set by the loader from the file it came from.
    product: str = ""

    @property
    def ref(self) -> str:
        """``metric.total_balance@2.1.0`` — what appears in the evidence envelope."""
        return f"metric.{self.name}@{self.version}"

    @property
    def requires_parameters(self) -> bool:
        return bool(self.parameters)

    def render(self, parameters: dict[str, Any] | None = None) -> str:
        """Fill the SQL fragment, refusing to guess a required parameter."""
        fragment = self.sql_fragment
        provided = parameters or {}
        for parameter in self.parameters:
            if parameter.name not in provided:
                if parameter.required:
                    raise ValueError(
                        f"metric {self.ref} requires parameter '{parameter.name}'; "
                        "ask the user rather than assuming a value"
                    )
                continue
            value = provided[parameter.name]
            literal = f"'{value}'" if isinstance(value, str) else str(value)
            fragment = fragment.replace(f"?{parameter.name}", literal)
        if "?" in fragment:
            raise ValueError(f"metric {self.ref} has an unfilled parameter in {fragment!r}")
        return fragment

    def to_prompt(self) -> str:
        bits = [f"- {self.ref} [{self.unit}] {self.definition.splitlines()[0].strip()}"]
        bits.append(f"  sql: {self.sql_fragment}")
        if self.synonyms:
            bits.append(f"  aka: {', '.join(self.synonyms[:6])}")
        if self.parameters:
            for parameter in self.parameters:
                bits.append(f"  param {parameter.name} ({parameter.type}{', required' if parameter.required else ''})")
        if self.caveats:
            bits.append(f"  caveat: {self.caveats[0].strip()}")
        return "\n".join(bits)


@dataclass
class Candidate:
    metric: Metric
    score: float
    matched_on: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.metric.ref,
            "name": self.metric.name,
            "product": self.metric.product,
            "definition": " ".join(self.metric.definition.split()),
            "unit": self.metric.unit,
            "score": round(self.score, 3),
            "matched_on": self.matched_on,
            "requires_parameters": self.metric.requires_parameters,
        }


@dataclass
class ResolutionResult:
    term: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def resolved(self) -> Metric | None:
        """The single confident match, or ``None`` when the term is ambiguous."""
        return None if self.is_ambiguous or not self.candidates else self.candidates[0].metric

    @property
    def is_ambiguous(self) -> bool:
        if len(self.candidates) < 2:
            return False
        return (self.candidates[0].score - self.candidates[1].score) <= AMBIGUITY_MARGIN

    @property
    def is_unresolved(self) -> bool:
        return not self.candidates

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "status": "ambiguous" if self.is_ambiguous else ("unresolved" if self.is_unresolved else "resolved"),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def metrics_dir() -> Path:
    return get_settings().metrics_dir


@lru_cache(maxsize=1)
def load_metrics(directory: Path | None = None) -> dict[str, Metric]:
    """Load every metric definition, keyed by name."""
    base = directory or metrics_dir()
    if not base.exists():
        raise FileNotFoundError(f"no metrics directory at {base}")

    metrics: dict[str, Metric] = {}
    for path in sorted(base.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        product = payload.get("product", path.stem)
        for raw in payload.get("metrics", []):
            metric = Metric.model_validate({**raw, "product": product})
            if metric.name in metrics:
                raise ValueError(f"duplicate metric '{metric.name}' (in {path.name})")
            metrics[metric.name] = metric

    if not metrics:
        raise ValueError(f"no metrics found in {base}")
    logger.info("metrics_loaded", count=len(metrics))
    return metrics


def reload_metrics() -> dict[str, Metric]:
    load_metrics.cache_clear()
    return load_metrics()


def metrics_for_product(product: str) -> list[Metric]:
    return [m for m in load_metrics().values() if m.product == product]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "by", "with", "is", "are"}
    return {t for t in _normalise(text).split() if t and t not in stop and len(t) > 2}


def resolve_metric(term: str, *, product: str | None = None, limit: int = 5) -> ResolutionResult:
    """Resolve a human term to metric candidates, in ranked order.

    Scoring is deliberately transparent rather than clever: an exact name or
    synonym match is a strong signal; token overlap with the definition is a weak
    one. Anything that scores similarly to the top candidate is reported as
    ambiguous so the agent asks instead of guessing.
    """
    metrics = [m for m in load_metrics().values() if product is None or m.product == product]
    needle = _normalise(term)
    needle_tokens = _tokens(term)
    scored: list[Candidate] = []

    for metric in metrics:
        name = _normalise(metric.name.replace("_", " "))
        synonyms = [_normalise(s) for s in metric.synonyms]
        definition_tokens = _tokens(metric.definition)

        score = 0.0
        matched_on = ""

        if needle == name:
            score, matched_on = 1.0, "name"
        elif needle in synonyms:
            score, matched_on = 0.95, "synonym"
        elif name in needle or any(syn in needle for syn in synonyms):
            score, matched_on = 0.85, "phrase_contains"
        elif needle_tokens and name.split()[0] in needle_tokens:
            score, matched_on = 0.6, "name_token"
        else:
            overlap = needle_tokens & definition_tokens
            if overlap:
                score = 0.45 * (len(overlap) / max(len(needle_tokens), 1)) + min(0.3, 0.1 * len(overlap))
                matched_on = f"definition:{','.join(sorted(overlap)[:3])}"

        if score >= MIN_MATCH_SCORE:
            scored.append(Candidate(metric=metric, score=round(score, 4), matched_on=matched_on))

    scored.sort(key=lambda c: (-c.score, c.metric.name))
    return ResolutionResult(term=term, candidates=scored[:limit])


def render_metric_catalogue(product: str | None = None) -> str:
    """Prompt block listing the metrics available for SQL generation."""
    metrics = sorted(
        (m for m in load_metrics().values() if product is None or m.product == product),
        key=lambda m: (m.product, m.name),
    )
    return "\n".join(metric.to_prompt() for metric in metrics)


__all__ = [
    "AMBIGUITY_MARGIN",
    "MIN_MATCH_SCORE",
    "Candidate",
    "Metric",
    "MetricParameter",
    "ResolutionResult",
    "load_metrics",
    "metrics_dir",
    "metrics_for_product",
    "reload_metrics",
    "render_metric_catalogue",
    "resolve_metric",
]
