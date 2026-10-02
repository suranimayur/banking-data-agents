"""Data product catalog.

Contracts are machine-readable *and* runtime inputs: the Copilot reads them to
discover products, to know which columns may be used for which purpose, and to
explain why it refused a request.
"""

from banking_data_agents.catalog.contracts import (
    ColumnContract,
    ProductContract,
    load_contracts,
    validate_contracts,
)
from banking_data_agents.catalog.registry import Catalog, get_catalog

__all__ = [
    "Catalog",
    "ColumnContract",
    "ProductContract",
    "get_catalog",
    "load_contracts",
    "validate_contracts",
]
