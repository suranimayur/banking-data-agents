"""CLI presentation for the data product catalog."""

from __future__ import annotations

from banking_data_agents.catalog.registry import get_catalog


def catalog_list() -> int:
    catalog = get_catalog()
    products = catalog.list_products()

    print("=" * 96)
    print("DATA PRODUCT CATALOG")
    print("=" * 96)
    print(f"{'PRODUCT':<16}{'VER':<9}{'DOMAIN':<12}{'ROWS':>12}  {'STATUS':<9}{'FRESH':>7}  OWNER")
    print("-" * 96)
    for product in products:
        age = product["age_hours"]
        print(
            f"{product['product']:<16}{product['version']:<9}{product['domain']:<12}"
            f"{product['row_count']:>12,}  {product['status']:<9}"
            f"{(f'{age:.1f}h' if age is not None else '-'):>7}  {product['owner']}"
        )

    print()
    for product in products:
        print(f"  {product['product']}  {product['description']}")
        print(f"    grain      : {product['grain']}   columns: {product['columns']}   metrics: {product['metrics']}")
        print(f"    allowed use: {', '.join(product['allowed_use']) or '-'}")
        if product["not_allowed_use"]:
            print(f"    PROHIBITED : {', '.join(product['not_allowed_use'])}")
        print()

    status = catalog.status()
    print(
        f"[catalog] {status['products']} products · {status['metrics']} metrics · "
        f"contract digest {status['contract_digest']}"
    )
    if status["degraded"]:
        print(f"[catalog] DEGRADED: {', '.join(status['degraded'])}")
    return 0


def catalog_show(product: str) -> int:
    catalog = get_catalog()
    try:
        contract = catalog.get_contract(product)
    except KeyError as exc:
        print(f"[catalog] {exc}")
        return 2

    print("=" * 96)
    print(f"{contract.product}@{contract.version}  ({contract.domain})")
    print("=" * 96)
    print(f"owner          : {contract.owner}")
    print(f"steward        : {contract.steward or '-'}")
    print(f"grain          : {contract.grain}")
    print(f"primary key    : {', '.join(contract.primary_key)}")
    print(f"classification : {contract.classification}")
    print(f"SLA            : freshness {contract.sla.freshness_hours}h · availability {contract.sla.availability}")
    print(f"refresh        : {contract.refresh.schedule}")
    print()
    print(" ".join(contract.description.split()))
    print()

    print(f"{'COLUMN':<28}{'TYPE':<16}{'UNIT':<10}NOTES")
    print("-" * 96)
    for column in contract.columns:
        note = column.description or column.definition or ""
        if column.metric_ref:
            note = f"[{column.metric_ref}] {note}"
        if column.pii:
            note = f"PII · {note}"
        if column.not_allowed_use:
            note = f"RESTRICTED({', '.join(column.not_allowed_use)}) · {note}"
        print(f"{column.name:<28}{column.type:<16}{(column.unit or ''):<10}{' '.join(note.split())[:60]}")

    metrics = catalog.metrics_for(contract.product)
    if metrics:
        print()
        print("METRICS")
        for metric in sorted(metrics, key=lambda m: m.name):
            print(f"  {metric.ref}")
            print(f"    {' '.join(metric.definition.split())}")
            print(f"    sql: {metric.sql_fragment}")
            for caveat in metric.caveats:
                print(f"    caveat: {' '.join(caveat.split())}")

    upstream = catalog.lineage(contract.product, direction="upstream")
    if upstream:
        print()
        print("UPSTREAM LINEAGE")
        for route in upstream:
            indent = "  " * route["level"]
            print(f"  {indent}{route['dataset']}  (via {route['transform_id']})")

    sla = catalog.sla(contract.product)
    if sla:
        row = sla[0]
        print()
        print(
            f"STATUS: {row['status']} · {row['row_count']:,} rows · "
            f"age {row.get('age_hours')}h (SLA {row.get('sla_freshness_hours')}h) · "
            f"dq critical {row.get('dq_critical_failures')} / warnings {row.get('dq_warning_failures')}"
        )
    return 0


__all__ = ["catalog_list", "catalog_show"]
