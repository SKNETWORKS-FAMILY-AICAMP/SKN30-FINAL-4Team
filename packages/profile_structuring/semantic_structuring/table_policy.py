"""Deterministic product-policy enrichment for LLM table classifications."""

from __future__ import annotations

from .models import TableCatalogEntry, TableCatalogProposal, TableClass, TablePolicy


_FIXED_POLICY = {
    TableClass.RULE: TablePolicy.SEARCH_ONLY,
    TableClass.ENTITY_RELATION: TablePolicy.SEARCH_ONLY,
    TableClass.CATALOG: TablePolicy.SEARCH_ONLY,
    TableClass.PROCESS: TablePolicy.SEARCH_ONLY,
    TableClass.FORM_OR_LAYOUT: TablePolicy.EXCLUDE,
    TableClass.UNRESOLVED: TablePolicy.SEARCH_ONLY,
}


def resolve_table_policy(proposal: TableCatalogProposal, *, aggregate_ready: bool = False) -> TableCatalogEntry:
    """Attach policy without another LLM call.

    Financial tables are aggregatable only when the common-IR adapter has
    independently verified stable header, row, and cell relationships.
    """

    if proposal.table_class == TableClass.FINANCIAL:
        policy = TablePolicy.SEARCH_AND_AGGREGATE if aggregate_ready else TablePolicy.SEARCH_ONLY
    else:
        policy = _FIXED_POLICY[proposal.table_class]
    return TableCatalogEntry(**proposal.model_dump(), policy=policy)
