"""Intent planning over the semantic layer.

This is the part of the system that makes the claim "agents never write free-form
SQL" true rather than aspirational.

The model's job is to choose the right *tool*; it never emits SQL. When the
``generate_sql`` tool is invoked it runs this module, which:

1. finds the governed metric phrases in the question,
2. **refuses to choose** when one phrase maps onto more than one governed metric
   (that is the single most common way an analytics agent is confidently wrong),
3. asks for a required metric parameter rather than inventing a default,
4. composes SQL by pasting versioned metric fragments — never a formula of its own.

Everything here is deterministic and rule-based, which is a deliberate trade: a
plan is reproducible, reviewable and diffable, and the regression suite can assert
exactly which SQL a question produces. The model still does the parts that need
judgement — deciding what the user meant, explaining an answer, noticing that a
figure looks wrong.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from banking_data_agents.catalog.contracts import ProductContract
from banking_data_agents.datagen.reference import STATES
from banking_data_agents.logging_setup import get_logger
from banking_data_agents.semantic.metrics import (
    Metric,
    ResolutionResult,
    resolve_metric,
)

logger = get_logger(__name__)

#: How many rows a grouped answer returns when the question does not say.
DEFAULT_GROUPED_LIMIT = 100
#: How many rows an ungrouped aggregate returns (it is always exactly one).
DEFAULT_SCALAR_LIMIT = 1
#: Upper bound the SQL guardrail enforces. Keep the planner inside it by design.
MAX_LIMIT = 1_000

#: Minimum score for a *fuzzy* term match. Below this a match is usually just a
#: shared English word in a definition, which is how agents hallucinate metrics.
FUZZY_MIN_SCORE = 0.6

#: Metric SQL fragments that mix currencies unless the question filters one.
CURRENCY_HAZARD_METRICS = {"txn_amount_sum", "txn_amount_avg", "net_flow"}

#: Human words for governed dimension columns.
DIMENSION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "region": ("region", "state", "geography", "geographic", "location"),
    "state_code": ("state code", "state_code"),
    "city": ("city", "town"),
    "customer_segment": ("segment", "customer segment", "relationship segment", "value segment"),
    "marketing_segment": ("marketing segment", "campaign segment"),
    "risk_category": ("risk category", "risk band", "risk grade"),
    "score_band": ("score band", "score bucket", "bureau band"),
    "risk_band": ("risk band", "credit grade", "credit band"),
    "age_group": ("age group", "age band", "age bracket"),
    "kyc_status": ("kyc status", "kyc", "verification status"),
    "customer_type": ("customer type", "entity type"),
    "preferred_channel": ("preferred channel", "favourite channel"),
    "channel_group": ("channel group", "channel", "rail", "channel mix"),
    "channel": ("channel",),
    "merchant_category": ("merchant category", "merchant type", "category of merchant"),
    "currency": ("currency", "currencies"),
    "debit_credit": ("debit or credit", "direction", "debit credit"),
    "fraud_label_status": ("fraud label status", "label status"),
    "source_system": ("source system", "source"),
    "confirmed_fraud_method": ("fraud method", "modus operandi", "fraud type"),
    "is_international": ("international", "cross border", "overseas", "domestic"),
    "customer_id": ("customer", "customers", "account holder"),
    "transaction_id": ("transaction", "transactions"),
    "as_of_date": ("as of date", "as-of date"),
    "transaction_month": ("month", "monthly", "by month", "month on month", "mom"),
    "transaction_date": ("date", "daily", "by day"),
    "transaction_hour": ("hour", "hourly", "by hour", "time of day"),
}

#: Words that introduce a grouping key rather than a measure.
GROUPING_CUES = ("by", "per", "across", "each", "breakdown")

#: Columns that may be used as a grouping key even though they are identifiers.
IDENTIFIER_DIMENSIONS = {"customer_id", "transaction_id"}

#: Numeric comparison words -> SQL operator, longest match first.
COMPARATORS: tuple[tuple[str, str], ...] = (
    ("greater than or equal to", ">="),
    ("less than or equal to", "<="),
    ("more than or equal to", ">="),
    ("at least", ">="),
    ("no less than", ">="),
    ("greater than", ">"),
    ("more than", ">"),
    ("bigger than", ">"),
    ("over", ">"),
    ("above", ">"),
    ("exceeding", ">"),
    ("less than", "<"),
    ("fewer than", "<"),
    ("under", "<"),
    ("below", "<"),
    ("at most", "<="),
    ("no more than", "<="),
)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "or",
    "to",
    "in",
    "on",
    "by",
    "with",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "do",
    "does",
    "did",
    "me",
    "my",
    "our",
    "we",
    "you",
    "your",
    "what",
    "which",
    "who",
    "whom",
    "how",
    "many",
    "much",
    "show",
    "give",
    "tell",
    "list",
    "please",
    "can",
    "could",
    "would",
    "should",
    "there",
    "their",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "as",
    "at",
    "from",
    "have",
    "has",
    "had",
    "get",
    "got",
    "all",
    "any",
    "each",
    "per",
    "vs",
    "versus",
}


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MetricInstance:
    """A governed metric plus the values supplied for its parameters."""

    metric: Metric
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return self.metric.ref

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "name": self.metric.name,
            "product": self.metric.product,
            "unit": self.metric.unit,
            "definition": " ".join(self.metric.definition.split()),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class TimeWindow:
    """A half-open date window applied to one column."""

    column: str
    start: date | None
    end: date | None
    description: str

    def as_sql(self) -> str | None:
        clauses: list[str] = []
        if self.start is not None:
            clauses.append(f"{self.column} >= DATE '{self.start.isoformat()}'")
        if self.end is not None:
            clauses.append(f"{self.column} <= DATE '{self.end.isoformat()}'")
        return " AND ".join(clauses) if clauses else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "description": self.description,
        }


@dataclass
class Plan:
    """Everything needed to compile one governed query."""

    question: str
    product: str | None = None
    metrics: list[MetricInstance] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    filter_notes: list[str] = field(default_factory=list)
    time_window: TimeWindow | None = None
    order_by: str | None = None
    descending: bool = True
    limit: int = DEFAULT_GROUPED_LIMIT
    ambiguities: list[ResolutionResult] = field(default_factory=list)
    ambiguous_terms: list[str] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- states --------------------------------------------------------------
    @property
    def is_ambiguous(self) -> bool:
        return bool(self.ambiguities)

    @property
    def has_metrics(self) -> bool:
        return bool(self.metrics)

    @property
    def requires_parameters(self) -> list[MetricInstance]:
        return [m for m in self.metrics if m.metric.requires_parameters and not m.parameters]

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "product": self.product,
            "metrics": [m.as_dict() for m in self.metrics],
            "dimensions": list(self.dimensions),
            "filters": list(self.filters),
            "filter_notes": list(self.filter_notes),
            "time_window": self.time_window.as_dict() if self.time_window else None,
            "order_by": self.order_by,
            "descending": self.descending,
            "limit": self.limit,
            "notes": list(self.notes),
            "ambiguous_terms": list(self.ambiguous_terms),
            "missing_terms": list(self.missing_terms),
        }


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).replace("_", " ").strip()


def _tokens(text: str) -> list[str]:
    return _normalise(text).split()


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in _STOPWORDS and len(t) > 1]


def _singular(token: str) -> str:
    """Crude English singularisation, used only for phrase lookup.

    "Customers by region" must find the metric registered as ``customer count``.
    A real lemmatiser is not worth a dependency here, and a wrong guess simply
    fails to match, which is the safe direction.
    """
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _phrase_keys(phrase: str) -> list[str]:
    normalised = _normalise(phrase)
    if not normalised:
        return []
    singular = " ".join(_singular(token) for token in normalised.split())
    return [normalised] if singular == normalised else [normalised, singular]


def _shift_months(anchor: date, months: int) -> date:
    """Add ``months`` (which may be negative) to a date, clamping the day."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Metric phrase detection
# ---------------------------------------------------------------------------
def _metric_phrases(metrics: dict[str, Metric]) -> dict[str, list[Metric]]:
    """Map every human phrase that names a metric onto the metrics it names.

    A phrase with more than one entry is ambiguity in the data estate itself, not
    a parsing failure — ``outstanding`` really does mean two different published
    numbers depending on which product you are standing in.
    """
    phrases: dict[str, list[Metric]] = {}
    for metric in metrics.values():
        candidates = {metric.name.replace("_", " "), *metric.synonyms}
        for phrase in candidates:
            for key in _phrase_keys(phrase):
                bucket = phrases.setdefault(key, [])
                if metric not in bucket:
                    bucket.append(metric)
    return phrases


def _value_span(tokens: list[str], value_tokens: list[str]) -> int | None:
    """Index where a multi-word value occurs contiguously in the question."""
    if not value_tokens:
        return None
    size = len(value_tokens)
    for start in range(len(tokens) - size + 1):
        if tokens[start : start + size] == value_tokens:
            return start
    return None


def _find_phrases(question: str, phrases: dict[str, list[Metric]]) -> list[tuple[int, str, list[Metric]]]:
    """Greedy longest-match scan for metric phrases.

    Longest-first matters: ``high value customer`` must win over ``customer``,
    and both must be consumed so the shorter phrase cannot be matched again.
    """
    tokens = _tokens(question)
    found: list[tuple[int, str, list[Metric]]] = []
    claimed: set[int] = set()

    for size in range(4, 0, -1):
        for start in range(len(tokens) - size + 1):
            span = set(range(start, start + size))
            if span & claimed:
                # Already taken by a longer phrase.
                continue
            span_tokens = tokens[start : start + size]
            for key in _phrase_keys(" ".join(span_tokens)):
                if key in phrases:
                    found.append((start, key, phrases[key]))
                    claimed |= span
                    break

    found.sort(key=lambda item: item[0])
    return found


# ---------------------------------------------------------------------------
# Dimension, filter and window detection
# ---------------------------------------------------------------------------
def _find_phrases_unordered(
    question: str,
    phrases: dict[str, list[Metric]],
    claimed: set[int],
    *,
    min_tokens: int = 2,
) -> list[tuple[int, str, list[Metric]]]:
    """Match multi-word metric phrases whose words appear out of order.

    "Which customers spend the most on cards" never contains the adjacent words
    "card spend", yet it plainly asks for the ``card_spend_*`` metric. Without
    this pass the honest answer is "no governed metric for: spend", which is
    exactly the kind of unhelpful literalism that makes people distrust an
    analytics agent.

    Two guards keep it from becoming a bag-of-words free-for-all. The phrase must
    be at least ``min_tokens`` long (a single shared English word is not
    evidence), and words already claimed by a contiguous match are unavailable.
    Longer phrases are tried first, so the most specific reading wins.
    """
    tokens = _tokens(question)
    available = Counter(_singular(token) for index, token in enumerate(tokens) if index not in claimed)
    found: list[tuple[int, str, list[Metric]]] = []
    taken: set[str] = set()

    # Most tokens first, then alphabetically for stability across runs.
    for key, matched in sorted(phrases.items(), key=lambda item: (-len(item[0].split()), item[0])):
        key_tokens = [_singular(token) for token in key.split()]
        if len(key_tokens) < min_tokens:
            continue
        # A token may serve only one metric, so "card spend" and "total spend"
        # cannot both claim "spend".
        if any(token in taken for token in key_tokens):
            continue
        needed = Counter(key_tokens)
        if any(available[token] < count for token, count in needed.items()):
            continue
        start = min(
            index for index, token in enumerate(tokens) if _singular(token) in key_tokens and index not in claimed
        )
        found.append((start, key, matched))
        taken.update(key_tokens)

    return found


def _dimension_phrases(contract: ProductContract, *, allow_identifiers: bool) -> dict[str, list[str]]:
    """Phrase -> column names, for every groupable column in a contract."""
    phrases: dict[str, list[str]] = {}
    for column in contract.columns:
        if column.name.startswith("_"):
            continue
        identifier = column.name in IDENTIFIER_DIMENSIONS
        if identifier and not allow_identifiers:
            continue
        if not identifier:
            # Group by anything categorical or low-cardinality, plus the date
            # columns a user might break a trend down by.
            groupable = (
                bool(column.enum)
                or column.type in {"string", "date", "boolean"}
                or column.name.endswith(("_group", "_band", "_category", "_month"))
            )
            if not groupable:
                continue
        names = DIMENSION_SYNONYMS.get(column.name, ()) or (column.name.replace("_", " "),)
        for name in names:
            for key in _phrase_keys(name):
                bucket = phrases.setdefault(key, [])
                if column.name not in bucket:
                    bucket.append(column.name)
    return phrases


def _find_dimensions(
    question: str,
    contract: ProductContract,
    *,
    allow_identifiers: bool,
    exclude: set[int] | None = None,
) -> tuple[list[str], set[int]]:
    tokens = _tokens(question)
    phrases = _dimension_phrases(contract, allow_identifiers=allow_identifiers)
    # Tokens already used by a metric phrase cannot also be a grouping key.
    claimed: set[int] = set(exclude or ())
    dimensions: list[str] = []

    for size in range(4, 0, -1):
        for start in range(len(tokens) - size + 1):
            span = set(range(start, start + size))
            columns: list[str] | None = None
            for key in _phrase_keys(" ".join(tokens[start : start + size])):
                if key in phrases:
                    columns = phrases[key]
                    break
            if not columns:
                continue
            # A phrase naming two columns (region vs state_code) prefers the
            # first declared, which the contract orders by importance.
            column = columns[0]
            if column in dimensions:
                continue
            # In "top 10 customers by balance" the word "customers" names both the
            # entity we are ranking and a count metric. The entity reading wins,
            # otherwise the answer would be a single count at the wrong grain.
            if span & claimed and column not in IDENTIFIER_DIMENSIONS:
                continue
            claimed |= span
            dimensions.append(column)

    # Preserve the contract's column order so SQL output is stable.
    order = {name: index for index, name in enumerate(contract.column_names)}
    dimensions.sort(key=lambda name: order.get(name, 999))
    return dimensions, claimed


def _enum_filters(
    question: str,
    contract: ProductContract,
    chosen_dimensions: list[str],
    consumed: set[int] | None = None,
) -> tuple[list[str], list[str]]:
    """Equality filters for enum values that literally appear in the question.

    ``consumed`` holds the token positions already claimed by a metric or
    dimension phrase. Without it, "international share" would also be filtered to
    ``channel = 'INTERNATIONAL'``, which would pin the answer at 1.0.
    """
    tokens = _tokens(question)
    token_set = set(tokens)
    consumed = consumed or set()
    lowered = question.lower()
    clauses: list[str] = []
    notes: list[str] = []

    for column in contract.columns:
        if not column.enum or column.name.startswith("_"):
            continue
        if column.name in chosen_dimensions:
            continue
        for value in column.enum:
            value_tokens = _tokens(value)
            if not value_tokens or not set(value_tokens) <= token_set:
                continue
            span = _value_span(tokens, value_tokens)
            if span is None or set(range(span, span + len(value_tokens))) & consumed:
                continue
            clause = f"{_sql_ident(column.name)} = {_sql_str(value)}"
            if clause not in clauses:
                clauses.append(clause)
                notes.append(f"filtered {column.name} = {value} (from the contract enum)")
            break

    # Geography needs a translation layer: users say "Maharashtra" and "Mumbai",
    # the gold tables store "MH" and "Mumbai".
    if contract.column("region") is not None and "region" not in chosen_dimensions:
        for _code, (name, _cities, _) in STATES.items():
            if name.lower() in lowered:
                # The gold region column carries the human state name; state_code
                # carries the abbreviation. Match the column that exists.
                clause = f"{_sql_ident('region')} = {_sql_str(name)}"
                if clause not in clauses:
                    clauses.append(clause)
                    notes.append(f"filtered region = '{name}' (matched the state name in the question)")
                break
    elif contract.column("state_code") is not None and "state_code" not in chosen_dimensions:
        for code, (name, _cities, _) in STATES.items():
            if name.lower() in lowered:
                clause = f"{_sql_ident('state_code')} = {_sql_str(code)}"
                if clause not in clauses:
                    clauses.append(clause)
                    notes.append(f"filtered state_code = {code} (state name '{name}')")
                break

    if contract.column("city") is not None and "city" not in chosen_dimensions:
        for code, (_state, cities, _) in STATES.items():
            for city in cities:
                if city.lower() in lowered:
                    clause = f"{_sql_ident('city')} = {_sql_str(city)}"
                    if clause not in clauses:
                        clauses.append(clause)
                        notes.append(f"filtered city = {city} (matched to state {code})")
                    break

    # Boolean flags, again only from words no metric already claimed.
    for column in contract.columns:
        if column.type != "boolean":
            continue
        positive = _normalise(column.name.replace("_", " "))
        if "international" in positive:
            for word, flag in (("international", "TRUE"), ("domestic", "FALSE")):
                span = _value_span(tokens, [word])
                if span is None or span in consumed:
                    continue
                clause = f"{_sql_ident(column.name)} = {flag}"
                if clause not in clauses:
                    clauses.append(clause)
                    notes.append(f"filtered {column.name} = {flag} (from the word '{word}')")
        if "fraud" in positive:
            span = _value_span(tokens, ["confirmed", "fraud"])
            if span is not None and not (set(range(span, span + 2)) & consumed):
                clause = f"{_sql_ident(column.name)} = TRUE"
                if clause not in clauses:
                    clauses.append(clause)
                    notes.append("filtered fraud_flag = TRUE (confirmed cases only)")

    return clauses, notes


def _numeric_filters(question: str, contract: ProductContract) -> tuple[list[str], list[str]]:
    """``balance over 1,000,000`` -> ``"total_balance" > 1000000``."""
    tokens = _tokens(question)
    clauses: list[str] = []
    notes: list[str] = []

    column_tokens: dict[str, str] = {}
    for numeric_column in contract.columns:
        if numeric_column.type.startswith(("decimal", "integer", "float", "double")):
            for token in _tokens(numeric_column.name):
                column_tokens.setdefault(token, numeric_column.name)

    for index, token in enumerate(tokens):
        operator: str | None = None
        consumed = 1
        for phrase, candidate in COMPARATORS:
            parts = phrase.split()
            if tokens[index : index + len(parts)] == parts:
                operator = candidate
                consumed = len(parts)
                break
        if not operator:
            continue

        amount = _parse_amount(tokens[index + consumed : index + consumed + 2])
        if amount is None:
            continue

        # "balance over 500,000" and "over 500,000 in total balance" are the
        # same filter; look immediately before the comparator, then after the
        # number, before giving up.
        column: str | None = None
        if index:
            column = column_tokens.get(tokens[index - 1])
        if column is None:
            for token in tokens[index + consumed + 1 : index + consumed + 5]:
                if token in column_tokens:
                    column = column_tokens[token]
                    break
        if column is None:
            continue

        clause = f"{_sql_ident(column)} {operator} {amount}"
        if clause not in clauses:
            clauses.append(clause)
            notes.append(f"filtered {column} {operator} {amount}")
    return clauses, notes


def _parse_amount(tokens: list[str]) -> int | float | None:
    if not tokens:
        return None
    raw = tokens[0]
    # "1.5m" / "250k" / "1,000,000"
    multiplier = 1
    if raw.endswith("k"):
        multiplier, raw = 1_000, raw[:-1]
    elif raw.endswith("m"):
        multiplier, raw = 1_000_000, raw[:-1]
    digits = re.match(r"^\d+(?:\.\d+)?$", raw.replace(",", ""))
    if not digits:
        return None
    value = float(digits.group(0)) * multiplier
    return int(value) if value.is_integer() else value


def _time_column(product: str) -> str | None:
    return {
        "transaction": "transaction_date",
        "credit_risk": "as_of_date",
        # customer_360 is a snapshot: there is no event date to window on.
        "customer_360": None,
    }.get(product)


def _detect_time_window(question: str, product: str, anchor: date) -> TimeWindow | None:
    column = _time_column(product)
    lowered = question.lower()
    if column is None:
        if re.search(r"\b(last|past|this|since|in \d{4})\b", lowered):
            return None
        return None

    match = re.search(r"\b(?:last|past|trailing)\s+(\d+)\s+(day|week|month|quarter|year)s?\b", lowered)
    if match:
        count = int(match.group(1))
        unit = match.group(2)
        if unit == "month":
            # Months are not a fixed number of days, so they are shifted on the
            # calendar rather than approximated with a timedelta.
            start = _shift_months(anchor, -count)
        else:
            start = (
                anchor
                - {
                    "day": timedelta(days=count),
                    "week": timedelta(weeks=count),
                    "quarter": timedelta(days=91 * count),
                    "year": timedelta(days=365 * count),
                }[unit]
            )
        return TimeWindow(column, start, anchor, f"last {count} {unit}{'s' if count != 1 else ''}")

    if re.search(r"\b(last|past|previous)\s+month\b", lowered):
        first_this_month = anchor.replace(day=1)
        end = first_this_month - timedelta(days=1)
        return TimeWindow(column, end.replace(day=1), end, "last calendar month")

    if re.search(r"\b(this|current)\s+month\b", lowered):
        return TimeWindow(column, anchor.replace(day=1), anchor, "month to date")

    if re.search(r"\byear\s+to\s+date\b|\bthis\s+year\b|\bcurrent\s+year\b|\bytd\b", lowered):
        return TimeWindow(column, date(anchor.year, 1, 1), anchor, "year to date")

    match = re.search(r"\b(?:in|during|for)\s+(\d{4})\b", lowered)
    if match:
        year = int(match.group(1))
        return TimeWindow(column, date(year, 1, 1), date(year, 12, 31), f"calendar year {year}")

    match = re.search(r"\b(?:in|during|for)\s+([a-z]+)\s+(\d{4})\b", lowered)
    if match and match.group(1) in _MONTHS:
        month = _MONTHS[match.group(1)]
        year = int(match.group(2))
        end = _shift_months(date(year, month, 1), 1) - timedelta(days=1)
        return TimeWindow(column, date(year, month, 1), end, f"{match.group(1).title()} {year}")

    match = re.search(r"\bsince\s+(\d{4})-(\d{2})-(\d{2})\b", lowered)
    if match:
        start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return TimeWindow(column, start, anchor, f"since {start.isoformat()}")

    return None


_RANKING = re.compile(r"\b(?:top|first|largest|biggest|highest|bottom|last|smallest|lowest)\s+(\d+)\b", re.IGNORECASE)
_TIME_UNIT = re.compile(r"^\s*(?:day|week|month|quarter|year)s?\b", re.IGNORECASE)

#: Words that ask for an extreme, which implies a ranking even without a number.
_SUPERLATIVE = re.compile(
    r"\b(most|least|fewest|highest|lowest|largest|biggest|smallest|greatest|best|worst|top|bottom)\b",
    re.IGNORECASE,
)
#: Superlatives that rank the other way up.
_ASCENDING_SUPERLATIVE = re.compile(r"\b(least|fewest|lowest|smallest|worst|bottom)\b", re.IGNORECASE)

#: Entity nouns -> the identifier column that a "which <entity> ... the most"
#: question is really grouping by. "who" counts: it names a person, and the only
#: person in this estate is the customer.
_ENTITY_DIMENSIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("who", "whom", "customer", "customers", "account holder", "client", "clients", "borrower", "borrowers"),
        "customer_id",
    ),
    (("transaction", "transactions", "txn", "txns"), "transaction_id"),
)

#: How many rows an implicit ranking ("... spend the most") returns. Asked
#: without a number, such a question means "the leaders", not "all 2,000".
DEFAULT_RANKING_LIMIT = 10


def _entity_dimension(question: str) -> str | None:
    """The identifier a superlative question is ranking, if it names one.

    "Who spends the most on cards" and "which customers spend the most" are the
    same question at the same grain; this is what lets the planner see that.
    "What is the highest balance" names no entity and stays a scalar.
    """
    text = " " + _normalise(question) + " "
    for nouns, column in _ENTITY_DIMENSIONS:
        if any(re.search(rf"\b{re.escape(noun)}\b", text) for noun in nouns):
            return column
    return None


def _is_bare_entity_count(phrase: str, metric: Metric, question: str) -> bool:
    """True for a row count matched by an entity noun alone, with nothing to count.

    "Show me everything about customer 360" contains the noun ``customer`` and no
    request to count anything, so answering "2000" is a guess about what was
    asked. A phrase that says *what* to count — ``customer segment``, ``how many
    customers`` — is a real request and is left alone.
    """
    tokens = phrase.split()
    if len(tokens) != 1 or metric.aggregate != "count":
        return False
    noun = _singular(tokens[0])
    if not any(noun == _singular(name) for nouns, _column in _ENTITY_DIMENSIONS for name in nouns):
        return False
    return not _COUNT_INTENT.search(question)


#: A relative time phrase. "the last 3 months" is a filter, and the word
#: "months" inside it must not also be read as the month column.
_TIME_WINDOW_PHRASE = re.compile(
    r"\b(?:last|past|trailing|previous|prior|next|this|current)\s+(?:\d+\s+)?(?:day|week|month|quarter|year)s?\b",
    re.IGNORECASE,
)


def _time_window_span(question: str) -> set[int]:
    """Token indices covered by a relative time phrase.

    Without this, "how many transactions in the last 3 months" acquires a
    ``GROUP BY transaction_month`` nobody asked for, because ``months`` is also a
    synonym for the month column. The window and the breakdown are different
    requests and only one of them was made.
    """
    tokens = _tokens(question)
    text = " ".join(tokens)
    span: set[int] = set()
    for match in _TIME_WINDOW_PHRASE.finditer(text):
        start = len(text[: match.start()].split())
        span |= set(range(start, start + len(match.group(0).split())))
    return span


def _detect_limit(question: str) -> tuple[int | None, bool | None]:
    """Row limit and sort direction implied by the question.

    "last 3 months" is a time window, not a row limit. The negative check on a
    following time unit is what keeps those two apart.
    """
    lowered = question.lower()
    for match in _RANKING.finditer(lowered):
        if _TIME_UNIT.match(lowered[match.end() :]):
            continue
        value = int(match.group(1))
        ascending = bool(re.search(r"\b(bottom|lowest|smallest)\b", lowered))
        return max(1, min(value, MAX_LIMIT)), not ascending

    count_match = re.search(r"\b(\d+)\s+(?:customers|accounts|transactions|rows|merchants|cities|regions)\b", lowered)
    if count_match and not _TIME_UNIT.match(lowered[count_match.end() :]):
        return max(1, min(int(count_match.group(1)), MAX_LIMIT)), True

    adjective_match = re.search(r"\b(\d+)\s+(?:largest|biggest|highest|smallest|lowest|best|worst)\b", lowered)
    if adjective_match:
        ascending = bool(re.search(r"\b(smallest|lowest|worst)\b", lowered))
        return max(1, min(int(adjective_match.group(1)), MAX_LIMIT)), not ascending
    return None, None


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def plan_question(
    question: str,
    *,
    products: list[str] | None = None,
    anchor: date | None = None,
    metrics: dict[str, Metric] | None = None,
    contracts: dict[str, ProductContract] | None = None,
) -> Plan:
    """Turn a question into a governed plan, or into a reason it cannot be one."""
    from banking_data_agents.catalog.contracts import load_contracts
    from banking_data_agents.semantic.metrics import load_metrics

    catalog_metrics = metrics or load_metrics()
    catalog_contracts = contracts or load_contracts()
    anchor = anchor or date.today()

    plan = Plan(question=question)
    phrases = _metric_phrases(catalog_metrics)
    hits = _find_phrases(question, phrases)

    # Anything the contiguous scan already read as a metric phrase cannot also be
    # read as a different metric's words, so the unordered pass runs on what is
    # left over.
    contiguous = set()
    for start, phrase, _matched in hits:
        contiguous |= set(range(start, start + len(phrase.split())))
    hits = hits + _find_phrases_unordered(question, phrases, contiguous)

    grouping = _grouping_positions(question, lookahead=GROUPING_LOOKAHEAD)

    # "spend by risk band" asks for a breakdown, not for the risk-band count
    # metric. The same phrase is both, so the grammatical cue decides — and a
    # phrase the cue discards must not consume its words, or the very dimension it
    # named would be excluded from the grouping scan by its own metric reading.
    accepted = [hit for hit in hits if not (hit[0] in grouping and _names_a_dimension(hit[1], catalog_contracts))]
    consumed: set[int] = set()
    for start, phrase, _matched in accepted:
        consumed |= set(range(start, start + len(phrase.split())))

    contended: list[tuple[str, list[Metric]]] = []
    for _start, phrase, matched in accepted:
        if len(matched) == 1 and _is_bare_entity_count(phrase, matched[0], question):
            # A bare entity noun is not a measure. Recorded so a later reader can
            # see why a term the platform knows produced no metric.
            consumed |= set(range(_start, _start + len(phrase.split())))
            continue
        if len(matched) > 1:
            # One term, several governed definitions. Whether that is *ambiguity*
            # or just the same phrase in two products depends on the rest of the
            # question, so defer the decision until the product is known.
            contended.append((phrase, matched))
            continue
        metric = matched[0]
        if all(existing.metric.name != metric.name for existing in plan.metrics):
            plan.metrics.append(MetricInstance(metric=metric))

    if not plan.metrics and not contended:
        # Fall back to fuzzy term resolution for phrasings the phrase list misses.
        # The bar is deliberately high: definition-token overlap alone produces
        # confident nonsense ("customers" matching four different metrics).
        #
        # A row count is additionally excluded unless the question asks to count.
        # "Show me everything about customer 360" fuzzy-matches the token
        # "customer" and would otherwise answer "2000" — a number nobody
        # requested, arrived at by guessing at the user's meaning rather than by
        # reading their words.
        count_intent = bool(_COUNT_INTENT.search(question))
        for candidate in _content_tokens(question):
            result = resolve_metric(candidate)
            candidates = [scored for scored in result.candidates if count_intent or scored.metric.aggregate != "count"]
            if not candidates or candidates[0].score < FUZZY_MIN_SCORE:
                continue
            best = candidates[0].score
            tied = [scored.metric for scored in candidates if scored.score == best]
            if len(tied) > 1 and len({metric.product for metric in tied}) > 1:
                contended.append((candidate, tied))
            else:
                resolved = candidates[0].metric
                if all(m.metric.name != resolved.name for m in plan.metrics):
                    plan.metrics.append(MetricInstance(metric=resolved))

    if not plan.metrics and not contended:
        plan.missing_terms = _missing_terms(question, catalog_contracts)
        if not plan.missing_terms:
            plan.missing_terms = _content_tokens(question)
        return plan

    # -- which product --------------------------------------------------------
    product_counts: dict[str, int] = {}
    for instance in plan.metrics:
        product_counts[instance.metric.product] = product_counts.get(instance.metric.product, 0) + 1

    if product_counts:
        if products:
            preferred = {p for p in products if p in product_counts}
            if preferred:
                product_counts = {p: c for p, c in product_counts.items() if p in preferred}
        plan.product = max(sorted(product_counts), key=lambda name: product_counts[name])
        others = sorted(set(product_counts) - {plan.product})
        if others:
            plan.notes.append(
                f"metrics also exist for {', '.join(others)}; answered from {plan.product} "
                "because it matched the most of the question"
            )
        plan.metrics = [m for m in plan.metrics if m.metric.product == plan.product]
    elif products:
        plan.product = products[0]

    # -- resolve the contended phrases now that a product is known ------------
    for phrase, matched in contended:
        scoped = [m for m in matched if plan.product is None or m.product == plan.product]
        if len(scoped) == 1 and scoped[0].product == plan.product:
            metric = scoped[0]
            plan.notes.append(
                f"'{phrase}' also names metrics in "
                + ", ".join(sorted({m.product for m in matched if m.product != metric.product}))
                + f"; resolved to {metric.ref} from {plan.product}"
            )
            if all(existing.metric.name != metric.name for existing in plan.metrics):
                plan.metrics.append(MetricInstance(metric=metric))
            continue
        plan.ambiguous_terms.append(phrase)
        plan.ambiguities.append(_ambiguity_for(phrase, scoped if len(scoped) > 1 else matched))

    if not plan.metrics:
        # Everything the user said was contested; ask rather than guess.
        if plan.ambiguities:
            plan.product = plan.ambiguities[0].candidates[0].metric.product
        else:
            plan.product = next(iter(catalog_contracts))
    if not plan.product:
        plan.product = sorted({m.metric.product for m in plan.metrics})[0]

    contract = catalog_contracts.get(plan.product)
    if contract is None:
        plan.notes.append(f"product '{plan.product}' has no published contract")
        return plan

    # -- parameters -----------------------------------------------------------
    for instance in plan.metrics:
        if instance.metric.requires_parameters:
            plan.notes.append(
                f"{instance.ref} requires {', '.join(p.name for p in instance.metric.parameters)}; not assumed"
            )

    # -- dimensions, filters, window -----------------------------------------
    ranking = _detect_limit(question)
    superlative = bool(_SUPERLATIVE.search(question))
    entity_dimension = _entity_dimension(question) if superlative else None
    # "who spends the most on cards" is a top-N question with the N left out. The
    # entity is what makes that reading safe: "what is the highest balance" names
    # no entity and must stay a single scalar.
    implicit_ranking = ranking[0] is None and entity_dimension is not None
    if implicit_ranking:
        ranking = (DEFAULT_RANKING_LIMIT, not _ASCENDING_SUPERLATIVE.search(question))
    wants_detail = superlative or any(
        word in question.lower() for word in ("top", "bottom", "highest", "lowest", "largest", "smallest")
    )
    found_dimensions, dimension_span = _find_dimensions(
        question,
        contract,
        allow_identifiers=bool(ranking[0]) and wants_detail,
        # Words inside a relative window ("the last 3 months") are spoken for.
        exclude=consumed | _time_window_span(question),
    )
    # "who" names the customer without using the word "customer", so the dimension
    # scan cannot have found the column; add it explicitly.
    if (
        implicit_ranking
        and entity_dimension
        and entity_dimension not in found_dimensions
        and entity_dimension in contract.column_names
    ):
        found_dimensions = [entity_dimension, *found_dimensions]
    plan.dimensions = found_dimensions
    consumed |= dimension_span
    plan.filters, enum_notes = _enum_filters(question, contract, plan.dimensions, consumed)
    numeric_clauses, numeric_notes = _numeric_filters(question, contract)
    plan.filters.extend(numeric_clauses)
    plan.filter_notes.extend([*enum_notes, *numeric_notes])

    _reconcile_aggregates(plan, question, catalog_metrics)
    plan.metrics = _prune_metrics(plan, contract, question)
    if not plan.metrics and not plan.ambiguities:
        # Pruning removed the only measure there was, so this product cannot
        # answer the question. Say what is missing rather than compile a stub.
        # Ambiguity is excluded deliberately: a question whose every term was
        # contested already has its own answer ("which definition did you mean?"),
        # and reporting it as a missing metric would hide the real problem.
        plan.missing_terms = _missing_terms(question, catalog_contracts) or _content_tokens(question)
        return plan

    # A grouping key that exists in a different product is a limitation worth
    # stating, not one worth silently ignoring.
    for phrase, owners in _orphan_dimensions(question, plan.product, catalog_contracts):
        plan.notes.append(
            f"'{phrase}' is a grouping key in {', '.join(owners)}, but {plan.product} "
            f"has no such column; the breakdown was not applied"
        )

    window = _detect_time_window(question, plan.product, anchor)
    if window is None and re.search(r"\b(last|past|trailing|since|year to date|ytd)\b", question.lower()):
        plan.notes.append(
            f"{plan.product} is a snapshot product with no event date, so the requested "
            "time window was not applied; figures are as of the latest refresh"
        )
    plan.time_window = window

    # -- shape ----------------------------------------------------------------
    limit, descending = ranking
    if limit is None:
        limit = DEFAULT_GROUPED_LIMIT if plan.dimensions else DEFAULT_SCALAR_LIMIT
    plan.limit = max(1, min(limit, MAX_LIMIT))
    plan.descending = True if descending is None else descending
    if plan.metrics:
        plan.order_by = plan.metrics[0].metric.name

    # Metric name colliding with a selected dimension would make ORDER BY
    # ambiguous, so the compiler renames it and the plan records the alias.
    if plan.order_by in plan.dimensions:
        plan.notes.append(f"metric '{plan.order_by}' shares its name with a selected dimension; output aliased")

    for instance in plan.metrics:
        if instance.metric.name in CURRENCY_HAZARD_METRICS:
            plan.notes.append(
                f"{instance.ref}: card-rail international amounts are in USD, so an unfiltered "
                "total mixes currencies. Filter currency = 'INR' or report per currency."
            )
    return plan


_TIME_WORDS = {
    "last",
    "past",
    "trailing",
    "previous",
    "this",
    "current",
    "since",
    "during",
    "year",
    "ytd",
    "month",
    "week",
    "day",
    "quarter",
    "date",
    "as",
    "of",
    "to",
    "today",
}


def _known_vocabulary(contracts: dict[str, ProductContract]) -> set[str]:
    """Every word the platform understands, used to report what it did not."""
    from banking_data_agents.semantic.metrics import load_metrics

    vocabulary: set[str] = set()
    for phrase in _metric_phrases(load_metrics()):
        vocabulary.update(phrase.split())
    for contract in contracts.values():
        for column in contract.columns:
            vocabulary.update(_tokens(column.name))
            names = DIMENSION_SYNONYMS.get(column.name) or (column.name.replace("_", " "),)
            for name in names:
                for key in _phrase_keys(name):
                    vocabulary.update(key.split())
            for value in column.enum or []:
                vocabulary.update(_tokens(value))
    for code, (name, cities, _) in STATES.items():
        vocabulary.update(_tokens(name))
        vocabulary.add(_normalise(code))
        for city in cities:
            vocabulary.update(_tokens(city))
    for phrase, _ in COMPARATORS:
        vocabulary.update(phrase.split())
    vocabulary.update(_TIME_WORDS)
    vocabulary.update(_MONTHS)
    return vocabulary


def _missing_terms(question: str, contracts: dict[str, ProductContract]) -> list[str]:
    vocabulary = _known_vocabulary(contracts)
    missing: list[str] = []
    for token in _content_tokens(question):
        if token.isdigit() or token in vocabulary or _singular(token) in vocabulary:
            continue
        missing.append(token)
    return missing


#: Aggregate words a user may state explicitly. Only the ones that genuinely
#: change the number are listed: "highest balance" is SUM(...) sorted descending,
#: which is the same figure as MAX, so treating "highest" as an aggregate cue
#: would produce noise rather than corrections.
_AGGREGATE_CUES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("average", "avg", "mean"), "avg"),
    (("median",), "median"),
)


def _reconcile_aggregates(plan: Plan, question: str, all_metrics: dict[str, Metric]) -> None:
    """Honour an explicitly requested aggregate, or say it is not available.

    "the average balance" must never come back as a sum. When a governed metric
    with the requested aggregate covers the same subject it is substituted and the
    plan says so; when none exists the plan records why, because silently
    answering a different question is the failure mode this module exists to
    prevent.
    """
    tokens = set(_content_tokens(question))
    for words, aggregate in _AGGREGATE_CUES:
        if not (tokens & set(words)):
            continue
        for index, instance in enumerate(list(plan.metrics)):
            if instance.metric.aggregate == aggregate:
                continue
            subject = set(_content_tokens(instance.metric.name.replace("_", " ")))
            replacement = next(
                (
                    metric
                    for metric in all_metrics.values()
                    if metric.product == plan.product
                    and metric.aggregate == aggregate
                    and not metric.requires_parameters
                    and subject & set(_content_tokens(metric.name.replace("_", " ")))
                ),
                None,
            )
            if replacement is None:
                plan.notes.append(
                    f"the request asks for the {aggregate} of '{instance.metric.name}', but no "
                    f"governed {aggregate} metric covers it; reported {instance.ref} instead"
                )
            else:
                plan.metrics[index] = MetricInstance(metric=replacement)
                plan.notes.append(
                    f"the request asks for the {aggregate}, so {instance.ref} was replaced by {replacement.ref}"
                )


_COUNT_INTENT = re.compile(r"\b(how many|number of|count of|count|volume of)\b", re.IGNORECASE)


def _bare_row_count(instance: MetricInstance) -> bool:
    """True for a metric that is nothing but ``COUNT(*)``."""
    return instance.metric.aggregate == "count" and instance.metric.sql_fragment.strip().upper() == "COUNT(*)"


def _prune_metrics(plan: Plan, contract: ProductContract, question: str) -> list[MetricInstance]:
    """Drop metrics that cannot mean anything at the requested grain.

    Three rules, all about *not* answering a question nobody asked:

    * At primary-key grain ``COUNT(*)`` is always 1. "Top 10 customers by balance"
      must not come back with a count-of-customers column.
    * "How many customers have a balance over X" is a count question. Projecting
      the balance as well invites the reader to compare two different things.
    * If the row count is the *only* thing left at primary-key grain, the question
      was about a different entity entirely — "which customers have the fewest
      transactions" cannot be answered from the customer product, because every
      customer has exactly one row in it. Returning 1 for all of them would be
      arithmetic dressed up as insight.
    """
    kept = list(plan.metrics)
    at_primary_key = set(contract.primary_key) <= set(plan.dimensions)

    if at_primary_key:
        degenerate = [instance for instance in kept if _bare_row_count(instance)]
        if degenerate and len(degenerate) < len(kept):
            kept = [instance for instance in kept if instance not in degenerate]
            plan.notes.append(
                "dropped "
                + ", ".join(instance.ref for instance in degenerate)
                + f" because the query is grouped at the {contract.product} primary key"
            )
        elif degenerate:
            plan.notes.append(
                f"grouped by {', '.join(contract.primary_key)}, the only available metric in "
                f"{contract.product} is a row count, which is 1 for every group. The measure "
                "being asked for belongs to another product; a cross-product answer would "
                "need that product's metrics over a shared key."
            )
            return []

    if _COUNT_INTENT.search(question):
        counts = [instance for instance in kept if instance.metric.unit == "count"]
        if counts and len(counts) < len(kept):
            dropped = [instance for instance in kept if instance not in counts]
            plan.notes.append(
                "count question: projected "
                + ", ".join(instance.ref for instance in counts)
                + "; "
                + ", ".join(instance.ref for instance in dropped)
                + " left in the plan only as a filter"
            )
            kept = counts

    # Two metrics with the same SQL fragment are the same number. Presenting it
    # twice under two names invites the reader to compare them and draw a
    # conclusion from a difference that does not exist.
    duplicates: list[MetricInstance] = []
    seen_fragments: set[str] = set()
    unique: list[MetricInstance] = []
    for instance in kept:
        fragment = " ".join(instance.metric.sql_fragment.split()).upper()
        if fragment in seen_fragments:
            duplicates.append(instance)
            continue
        seen_fragments.add(fragment)
        unique.append(instance)
    if duplicates and unique:
        plan.notes.append(
            "dropped "
            + ", ".join(instance.ref for instance in duplicates)
            + ": identical aggregate to "
            + ", ".join(instance.ref for instance in unique)
        )
        kept = unique

    return kept


#: How far past a grouping cue a metric reading is still suppressed. "by
#: marketing segment" puts the cue three tokens before the word `segment`, and
#: `segment` happens to be a metric synonym as well as a column synonym; without
#: the lookahead the metric reading wins and the breakdown silently disappears.
GROUPING_LOOKAHEAD = 3


def _grouping_positions(question: str, *, lookahead: int = 1) -> set[int]:
    """Token indices a grouping cue introduces (``by region`` -> the index of ``region``)."""
    tokens = _tokens(question)
    positions: set[int] = set()
    for index, token in enumerate(tokens):
        if token in GROUPING_CUES:
            for offset in range(1, lookahead + 1):
                if index + offset < len(tokens):
                    positions.add(index + offset)
    return positions


def _orphan_dimensions(
    question: str, product: str, contracts: dict[str, ProductContract]
) -> list[tuple[str, list[str]]]:
    """Grouping keys the question asks for that the chosen product cannot supply."""
    tokens = _tokens(question)
    orphans: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for start in sorted(_grouping_positions(question)):
        for size in (3, 2, 1):
            span_tokens = tokens[start : start + size]
            if len(span_tokens) < size:
                continue
            key = _phrase_keys(" ".join(span_tokens))[0]
            owners = sorted(
                name
                for name, contract in contracts.items()
                if key in _dimension_phrases(contract, allow_identifiers=True)
            )
            if owners and product not in owners and key not in seen:
                seen.add(key)
                orphans.append((key, owners))
            if owners:
                break
    return orphans


def _names_a_dimension(phrase: str, contracts: dict[str, ProductContract]) -> bool:
    """True when a phrase is also a groupable column somewhere in the estate."""
    key = _phrase_keys(phrase)[0]
    return any(key in _dimension_phrases(contract, allow_identifiers=True) for contract in contracts.values())


def _ambiguity_for(phrase: str, matches: list[Metric]) -> ResolutionResult:
    """Build a resolution result from literal phrase collisions."""
    from banking_data_agents.semantic.metrics import Candidate

    candidates = [Candidate(metric=metric, score=1.0, matched_on=f"phrase:{phrase}") for metric in matches]
    return ResolutionResult(term=phrase, candidates=candidates)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------
def compile_plan(plan: Plan, contract: ProductContract) -> str:
    """Render a plan to SQL. Deterministic: same plan, byte-identical query."""
    if not plan.metrics:
        raise ValueError("cannot compile a plan with no metrics")
    if plan.product is None:
        raise ValueError("cannot compile a plan with no product")
    if plan.requires_parameters:
        names = ", ".join(
            f"{instance.ref}.{parameter.name}"
            for instance in plan.requires_parameters
            for parameter in instance.metric.parameters
        )
        raise ValueError(f"missing required metric parameter(s): {names}")

    dimension_set = set(plan.dimensions)
    projections: list[str] = [_sql_ident(name) for name in plan.dimensions]
    aliases: dict[str, str] = {}

    for instance in plan.metrics:
        expression = instance.metric.render(instance.parameters)
        alias = instance.metric.name
        if alias in dimension_set:
            alias = f"count_{alias}"
        aliases[instance.ref] = alias
        projections.append(f"{expression} AS {alias}")

    lines = [f"SELECT {', '.join(projections)}", f"FROM gold.{plan.product}"]

    predicates: list[str] = list(plan.filters)
    if plan.time_window:
        window_sql = plan.time_window.as_sql()
        if window_sql:
            predicates.append(window_sql)

    if predicates:
        lines.append("WHERE " + "\n  AND ".join(predicates))

    if plan.dimensions:
        lines.append("GROUP BY " + ", ".join(_sql_ident(name) for name in plan.dimensions))

    if plan.order_by:
        alias = aliases.get(
            next((m.ref for m in plan.metrics if m.metric.name == plan.order_by), ""),
            plan.order_by,
        )
        lines.append(f"ORDER BY {alias} {'DESC' if plan.descending else 'ASC'}")

    lines.append(f"LIMIT {plan.limit}")
    return "\n".join(lines) + ";"


def plan_and_compile(
    question: str,
    *,
    products: list[str] | None = None,
    anchor: date | None = None,
) -> tuple[Plan, str | None]:
    """Plan and compile in one call. Returns ``(plan, sql_or_None)``."""
    from banking_data_agents.catalog.contracts import load_contracts

    plan = plan_question(question, products=products, anchor=anchor)
    if not plan.has_metrics or plan.is_ambiguous or plan.requires_parameters:
        return plan, None
    contract = load_contracts().get(plan.product or "")
    if contract is None:
        return plan, None
    return plan, compile_plan(plan, contract)


def explain_plan(plan: Plan) -> str:
    """Short human summary of what the plan will do. Used in answers and logs."""
    parts: list[str] = []
    if plan.product:
        parts.append(f"product={plan.product}")
    if plan.metrics:
        parts.append("metrics=" + ", ".join(m.ref for m in plan.metrics))
    if plan.dimensions:
        parts.append("group by " + ", ".join(plan.dimensions))
    if plan.time_window:
        parts.append(plan.time_window.description)
    if plan.filters:
        parts.append(f"{len(plan.filters)} filter(s)")
    parts.append(f"limit {plan.limit}")
    return " · ".join(parts)


__all__ = [
    "DEFAULT_GROUPED_LIMIT",
    "DEFAULT_SCALAR_LIMIT",
    "MAX_LIMIT",
    "MetricInstance",
    "Plan",
    "TimeWindow",
    "compile_plan",
    "explain_plan",
    "plan_and_compile",
    "plan_question",
]
