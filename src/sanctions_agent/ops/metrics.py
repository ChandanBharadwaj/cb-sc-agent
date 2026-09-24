"""Prometheus metrics, computed from Postgres at scrape time (always consistent with the DB)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from sanctions_agent.db.engine import fetch_all, tx


class DbCollector(Collector):
    def collect(self) -> Iterator[Any]:
        try:
            with tx() as conn:
                sources = fetch_all(
                    conn,
                    """SELECT source_id, level, status, is_core, breaker_state, consecutive_failures,
                    extract(epoch FROM last_success_at) AS last_success, extract(epoch FROM last_change_at) AS last_change,
                    extract(epoch FROM last_fetch_at) AS last_fetch,
                    extract(epoch FROM now() - coalesce(last_success_at, created_at)) AS staleness
                    FROM source""",
                )
                records = fetch_all(
                    conn,
                    """SELECT v.source_id, e.key AS entity_type, e.value::int AS n
                    FROM list_version v, jsonb_each_text(v.counts_by_type) e WHERE v.status = 'PUBLISHED'""",
                )
                runs = fetch_all(
                    conn,
                    """SELECT source_id, status, count(*) AS n FROM ingestion_run
                    WHERE queued_at > now() - interval '24 hours' GROUP BY 1, 2""",
                )
                incidents = fetch_all(
                    conn, "SELECT severity, count(*) AS n FROM incident WHERE status <> 'RESOLVED' GROUP BY 1"
                )
                proposals = fetch_all(
                    conn,
                    "SELECT kind, count(*) AS n FROM proposed_change WHERE status = 'PENDING' GROUP BY 1",
                )
                agent = fetch_all(
                    conn,
                    """SELECT coalesce(sum(cost_usd), 0) AS cost, coalesce(sum(input_tokens), 0) AS inp,
                    coalesce(sum(output_tokens), 0) AS outp, count(*) FILTER (WHERE fallback_used) AS fallbacks
                    FROM agent_cycle WHERE started_at >= date_trunc('day', now())""",
                )
                held = fetch_all(
                    conn,
                    "SELECT count(*) AS n FROM removal_candidate WHERE status IN ('PENDING','EVIDENCE_FOUND','REJECTED_PARSER')",
                )
        except Exception:  # scraping must never crash the exporter
            yield GaugeMetricFamily("sanctions_db_up", "Database reachable", value=0)
            return
        yield GaugeMetricFamily("sanctions_db_up", "Database reachable", value=1)
        g = {
            name: GaugeMetricFamily(f"sanctions_source_{name}", help_, labels=["source_id", "level", "core"])
            for name, help_ in (
                ("last_success_timestamp", "Last run that left published data current (unix s)"),
                ("last_change_timestamp", "Last new version published (unix s)"),
                ("last_fetch_timestamp", "Last successful HTTP fetch (unix s)"),
                ("staleness_seconds", "Seconds since last success"),
                ("consecutive_failures", "Failed runs in a row"),
                ("breaker_open", "1 if the circuit breaker is open"),
                ("active", "1 if the source is ACTIVE"),
            )
        }
        for s in sources:
            lbl = [s["source_id"], str(s["level"]), str(s["is_core"]).lower()]
            for key, col in (
                ("last_success_timestamp", "last_success"),
                ("last_change_timestamp", "last_change"),
                ("last_fetch_timestamp", "last_fetch"),
                ("staleness_seconds", "staleness"),
            ):
                if s[col] is not None:
                    g[key].add_metric(lbl, float(s[col]))
            g["consecutive_failures"].add_metric(lbl, s["consecutive_failures"])
            g["breaker_open"].add_metric(lbl, 1 if s["breaker_state"] == "OPEN" else 0)
            g["active"].add_metric(lbl, 1 if s["status"] == "ACTIVE" else 0)
        yield from g.values()
        rec = GaugeMetricFamily(
            "sanctions_records", "Records in the published version", labels=["source_id", "entity_type"]
        )
        for r in records:
            rec.add_metric([r["source_id"], r["entity_type"]], r["n"])
        yield rec
        rg = GaugeMetricFamily(
            "sanctions_runs_24h", "Runs in the last 24h by status", labels=["source_id", "status"]
        )
        for r in runs:
            rg.add_metric([r["source_id"], r["status"]], r["n"])
        yield rg
        ig = GaugeMetricFamily("sanctions_open_incidents", "Open incidents", labels=["severity"])
        for r in incidents:
            ig.add_metric([r["severity"]], r["n"])
        yield ig
        pg = GaugeMetricFamily("sanctions_pending_proposals", "Proposals awaiting review", labels=["kind"])
        for r in proposals:
            pg.add_metric([r["kind"]], r["n"])
        yield pg
        yield GaugeMetricFamily(
            "sanctions_held_removals",
            "Removals still blocking (awaiting confirmation)",
            value=held[0]["n"] if held else 0,
        )
        a = agent[0] if agent else {"cost": 0, "inp": 0, "outp": 0, "fallbacks": 0}
        yield GaugeMetricFamily(
            "sanctions_agent_cost_usd_today", "LLM spend today (USD)", value=float(a["cost"])
        )
        yield GaugeMetricFamily(
            "sanctions_agent_input_tokens_today", "LLM input tokens today", value=float(a["inp"])
        )
        yield GaugeMetricFamily(
            "sanctions_agent_output_tokens_today", "LLM output tokens today", value=float(a["outp"])
        )
        yield GaugeMetricFamily(
            "sanctions_agent_fallback_cycles_today",
            "Cycles handled by autopilot fallback",
            value=float(a["fallbacks"]),
        )
