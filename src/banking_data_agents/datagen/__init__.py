"""Deterministic synthetic banking source systems.

Emulates six upstream systems (CRM, core banking, loans, cards, bureau, payments)
with realistic behavioural structure, deliberately injected data-quality problems,
and a fraud label holdout that agent code must never read.

Everything is generated from a single seed, so a run is byte-reproducible and CI
can assert that two generations are identical.
"""

from banking_data_agents.datagen.generate import generate_all, generate_source

__all__ = ["generate_all", "generate_source"]
