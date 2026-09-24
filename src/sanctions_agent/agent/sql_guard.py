"""Guard for the Q&A agent's free-form SQL escape hatch.

Accepts only a single read query over ``analytics`` views, with an allow-list of functions, and
forces a LIMIT. It runs as the ``sanctions_analyst`` role (which cannot see anything else) inside a
read-only, time-limited transaction - so this guard is defence in depth, not the only barrier.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

ALLOWED_VIEWS = {
    "v_source_status",
    "v_run_summary",
    "v_run_progress",
    "v_version_counts",
    "v_fill_rates",
    "v_quality_metrics",
    "v_change_volume",
    "v_dq_issues",
    "v_enrichment_coverage",
    "v_notice_coverage",
    "v_notices",
    "v_removal_holds",
    "v_proposals",
    "v_incidents",
    "v_agent_activity",
    "v_agent_tool_usage",
    "v_signals",
    "v_snapshots",
    "v_field_catalog",
    "v_fetch_health",
}
ALLOWED_ANON_FUNCS = {
    "date_trunc",
    "to_char",
    "now",
    "current_date",
    "age",
    "date_part",
    "percentile_cont",
    "percentile_disc",
    "string_agg",
    "array_agg",
    "jsonb_each_text",
    "jsonb_object_keys",
    "jsonb_array_length",
    "cardinality",
    "array_length",
    "bool_or",
    "bool_and",
    "greatest",
    "least",
    "round",
    "abs",
    "floor",
    "ceil",
    "coalesce",
    "nullif",
    "lower",
    "upper",
    "length",
    "concat",
    "left",
    "right",
    "trim",
    "split_part",
    "make_interval",
    "extract",
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "stddev",
    "variance",
    "mode",
}
FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.Merge,
    exp.Into,
    exp.Lock,
)
MAX_LIMIT = 500


class SqlRejected(ValueError):
    pass


def guard(sql: str) -> str:
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SqlRejected(f"could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise SqlRejected("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise SqlRejected("only SELECT queries are allowed")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlRejected(f"{type(node).__name__} is not allowed")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        schema = (t.db or "").lower()
        if name in ctes and not schema:
            continue
        if schema and schema != "analytics":
            raise SqlRejected(f"only the analytics schema is readable (got {schema}.{name})")
        if name not in ALLOWED_VIEWS:
            raise SqlRejected(f"unknown or non-analytics relation {name!r}")
    for anon in tree.find_all(exp.Anonymous):
        if anon.name.lower() not in ALLOWED_ANON_FUNCS:
            raise SqlRejected(f"function {anon.name!r} is not allowed")
    for f in tree.find_all(exp.Func):
        n = (f.sql_name() or "").lower()
        if n.startswith("pg_") or n in {
            "set_config",
            "current_setting",
            "dblink",
            "lo_import",
            "lo_export",
            "query_to_xml",
            "xpath",
        }:
            raise SqlRejected(f"function {n!r} is not allowed")
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(MAX_LIMIT)
    else:
        try:
            value = int(limit.expression.this)
        except (AttributeError, TypeError, ValueError):
            value = MAX_LIMIT + 1
        if value > MAX_LIMIT:
            tree = tree.limit(MAX_LIMIT)
    return tree.sql(dialect="postgres")
