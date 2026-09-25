"""Command line: setup, serving, one-off runs, status, Q&A, review and go-live checks.

sanctions-agent db upgrade
sanctions-agent sources import [--file config/sources.yaml] [--overwrite]
sanctions-agent serve | supervise | tick
sanctions-agent run --source ofac_sdn [--mode dry_run] | reparse --source ofac_sdn --sha256 <hash>
sanctions-agent status | ask "which sources are stale?"
sanctions-agent proposals list | approve <id> --reviewer alice | reject <id> --reviewer alice --comment ...
sanctions-agent verify-sources [--source un_sc] [--write-known-paths]
sanctions-agent pin-schemas --source ofac_sdn --url https://...xsd
sanctions-agent load-manual --source eu_fsf --file ./download.xml --reason "EU site down, NFR-08"
sanctions-agent annex import-csv --source eu_annex_xlii --file vessels.csv --act-url https://eur-lex...
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import typer

from sanctions_agent.settings import PROJECT_ROOT, get_settings

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Sanctions list ingestion agent (Level 1 + 2)"
)
db_app = typer.Typer(no_args_is_help=True, help="Database schema")
sources_app = typer.Typer(no_args_is_help=True, help="Source configuration (seed import / export)")
proposals_app = typer.Typer(no_args_is_help=True, help="Human review queue")
annex_app = typer.Typer(no_args_is_help=True, help="EU Annex XLII / IV curated entries")
app.add_typer(db_app, name="db")
app.add_typer(sources_app, name="sources")
app.add_typer(proposals_app, name="proposals")
app.add_typer(annex_app, name="annex")


def _echo_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    typer.echo("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))


def _sync_catalog() -> None:
    from sanctions_agent.db.engine import tx
    from sanctions_agent.quality.field_catalog import sync_field_catalog

    with tx(actor="system:catalog") as conn:
        n = sync_field_catalog(conn)
    typer.echo(f"field catalog: {n} entries")


# ================================================================================ db
@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """Apply Alembic migrations (schema + analytics views + analyst role)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, revision)
    typer.echo(f"database upgraded to {revision}")


# ================================================================================ sources
@sources_app.command("import")
def sources_import(
    file: Path = typer.Option(None, help="Seed YAML (default: SANCTIONS_SOURCES_SEED_FILE)"),
    overwrite: bool = typer.Option(
        False, help="Re-apply seed config to existing sources as a NEW audited version"
    ),
    actor: str = typer.Option("seed", help="Recorded as the change author"),
) -> None:
    """Create sources missing from the database. UI edits are never overwritten unless --overwrite."""
    from sanctions_agent.db.engine import tx
    from sanctions_agent.sources.registry import import_seed

    path = file or get_settings().sources_seed_file
    with tx(actor=actor) as conn:
        res = import_seed(conn, path, actor=actor, overwrite=overwrite)
    for k, v in res.items():
        typer.echo(f"{k}: {', '.join(v) if v else '-'}")
    _sync_catalog()


@sources_app.command("export")
def sources_export(out: Path = typer.Option(None, help="Write to this file instead of stdout")) -> None:
    """Export the live (database) configuration as seed YAML - for backup or GitOps review."""
    from sanctions_agent.db.engine import tx
    from sanctions_agent.sources.registry import export_seed

    with tx() as conn:
        text = export_seed(conn)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@sources_app.command("list")
def sources_list() -> None:
    from sanctions_agent.db.engine import fetch_all, tx

    with tx() as conn:
        rows = fetch_all(
            conn,
            "SELECT source_id, level, adapter_type, status, cadence_minutes, cron_expr, health"
            " FROM analytics.v_source_status ORDER BY level, priority, source_id",
        )
    _table(rows, ["source_id", "level", "adapter_type", "status", "cadence_minutes", "cron_expr", "health"])


# ================================================================================ services
@app.command()
def serve(host: str = "0.0.0.0", port: int = 8080, workers: int = 1) -> None:  # noqa: S104 - container bind
    """Run the API + operations console."""
    import uvicorn

    try:
        _sync_catalog()
    except Exception as e:
        typer.echo(f"field catalog sync skipped: {e}", err=True)
    uvicorn.run(
        "sanctions_agent.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=True,
        log_config=None,
    )


def _supervisor(no_agent: bool) -> Any:
    from sanctions_agent.ops.scheduler import Supervisor

    agent = None
    if not no_agent and os.environ.get("OPENAI_API_KEY"):
        from sanctions_agent.agent.supervisor import SupervisorAgent

        agent = SupervisorAgent()
    elif not no_agent:
        typer.echo("OPENAI_API_KEY not set: running on deterministic autopilot only", err=True)
    return Supervisor(agent=agent)


@app.command()
def supervise(no_agent: bool = typer.Option(False, help="Autopilot only (no LLM)")) -> None:
    """Run the supervisor loop: scheduling, workers, watchdog, agent cycles. Stops cleanly on SIGTERM."""
    try:
        _sync_catalog()
    except Exception as e:
        typer.echo(f"field catalog sync skipped: {e}", err=True)
    _supervisor(no_agent).run_forever()


@app.command()
def tick(no_agent: bool = typer.Option(False, help="Autopilot only (no LLM)")) -> None:
    """Run one supervisor tick (reclaim, signals, watchdog, plan, dispatch) and print what happened."""
    sup = _supervisor(no_agent)
    try:
        _echo_json(sup.tick())
    finally:
        sup.pool.shutdown()


# ================================================================================ runs
@app.command()
def run(
    source: list[str] = typer.Option(
        ..., "--source", "-s", help="Repeat to run several sources as one batch"
    ),
    mode: str = typer.Option("normal", help="normal | force_refetch | dry_run"),
    reason: str = typer.Option("manual run from CLI"),
    limit: int = typer.Option(None, help="Enrichment subject limit"),
) -> None:
    """Run one or more sources now, in this process, as one batch, and print the outcome."""
    from sanctions_agent.db.engine import fetch_all, tx
    from sanctions_agent.pipeline import runs
    from sanctions_agent.pipeline.runner import run_once

    options: dict[str, Any] = {}
    if mode == "force_refetch":
        options["force_refetch"] = True
    elif mode == "dry_run":
        options["dry_run"] = True
    elif mode != "normal":
        raise typer.BadParameter("mode must be normal, force_refetch or dry_run")
    if limit:
        options["limit"] = limit
    user = f"cli:{_user()}"
    wanted = list(dict.fromkeys(source))
    with tx(actor=user) as conn:
        batch_id = runs.create_batch(
            conn,
            trigger="MANUAL",
            requested_by=user,
            reason=reason,
            options={"mode": mode},
            requested_sources=wanted,
        )
    results: list[tuple[str, str]] = []
    for sid in wanted:
        try:
            results.append(
                run_once(sid, requested_by=user, reason=reason, options=options, batch_id=batch_id)
            )
        except runs.RunAlreadyActive as e:
            with tx(actor=user) as conn:
                runs.record_refused(
                    conn, batch_id, [{"source_id": sid, "why": f"already running ({e.run_id})"}]
                )
            typer.echo(f"{sid}: already running ({e.run_id}) - not queued", err=True)
    if len(wanted) == 1 and results:
        _report_run(*results[0])
        return
    with tx() as conn:
        rows = fetch_all(
            conn,
            "SELECT source_id, status, duration_seconds, added, changed, removed, error_class"
            " FROM analytics.v_run_summary WHERE batch_id = %s ORDER BY source_id",
            (batch_id,),
        )
    typer.echo(f"batch {batch_id}")
    _table(rows, ["source_id", "status", "duration_seconds", "added", "changed", "removed", "error_class"])
    if any(st in ("FAILED", "QUARANTINED", "ABANDONED") for _, st in results) or len(results) < len(wanted):
        raise typer.Exit(1)


@app.command()
def reparse(
    source: str = typer.Option(..., "--source", "-s"),
    sha256: str = typer.Option(..., help="Archived raw file hash (see the source's run history)"),
    reason: str = typer.Option("re-parse after parser fix"),
) -> None:
    """Re-run validate/parse/publish from an archived file - no download (use after a parser fix)."""
    from sanctions_agent.db.engine import fetch_one, tx
    from sanctions_agent.pipeline.runner import run_once

    with tx() as conn:
        if not fetch_one(conn, "SELECT 1 FROM raw_artifact WHERE sha256 = %s", (sha256,)):
            raise typer.BadParameter(f"no archived artifact {sha256}")
    run_id, status = run_once(
        source, requested_by=f"cli:{_user()}", reason=reason, options={"reparse_sha256": sha256}
    )
    _report_run(run_id, status)


@app.command("load-manual")
def load_manual(
    source: str = typer.Option(..., "--source", "-s"),
    file: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    reason: str = typer.Option(..., help="Why a manual load was needed (recorded as evidence)"),
    dry_run: bool = typer.Option(False, help="Validate only"),
) -> None:
    """NFR-08 runbook: load a file obtained out-of-band (e.g. publisher site blocked) through the normal pipeline.
    The file is archived write-once with its hash, validated, parsed and published like any fetched file."""
    from sanctions_agent.db.engine import tx
    from sanctions_agent.pipeline.runner import run_once
    from sanctions_agent.storage.blobstore import get_blob_store, sha256_file

    sha = sha256_file(file)
    uri = get_blob_store().put_file(file, sha)
    with tx(actor=f"cli:{_user()}") as conn:
        conn.execute(
            "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes, content_type, compression)"
            " VALUES (%s, %s, %s, %s, 'gzip') ON CONFLICT DO NOTHING",
            (sha, uri, file.stat().st_size, "application/octet-stream"),
        )
    options: dict[str, Any] = {"reparse_sha256": sha, "manual_file": file.name, "manual_load": True}
    if dry_run:
        options["dry_run"] = True
    run_id, status = run_once(
        source, requested_by=f"cli:{_user()}", reason=f"MANUAL LOAD: {reason}", options=options
    )
    typer.echo(f"archived {file.name} as {sha}")
    _report_run(run_id, status)


def _report_run(run_id: str, status: str) -> None:
    from sanctions_agent.db.engine import fetch_one, tx

    with tx() as conn:
        r = fetch_one(conn, "SELECT * FROM analytics.v_run_summary WHERE run_id = %s", (run_id,))
    _echo_json(r)
    if status in ("FAILED", "QUARANTINED", "ABANDONED"):
        raise typer.Exit(1)


def _user() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "operator"


# ================================================================================ status / ask
@app.command()
def status() -> None:
    """Source health, freshness, active runs and review backlog."""
    from sanctions_agent.db.engine import fetch_all, fetch_val, tx

    with tx() as conn:
        rows = fetch_all(
            conn,
            """SELECT source_id, status, health, breaker_state, hours_since_success,
            hours_since_change, current_version_seq AS version, record_count FROM analytics.v_source_status
            ORDER BY level, priority, source_id""",
        )
        active = fetch_all(conn, "SELECT source_id, status, current_step, pct FROM analytics.v_run_progress")
        batches = fetch_all(
            conn,
            """SELECT to_char(created_at, 'YYYY-MM-DD HH24:MI') AS created_at, trigger, requested_by, status,
                      sources_run, ok, attention, failed FROM analytics.v_run_batches ORDER BY 1 DESC LIMIT 5""",
        )
        pending = fetch_val(conn, "SELECT count(*) FROM proposed_change WHERE status = 'PENDING'")
        incidents = fetch_val(conn, "SELECT count(*) FROM incident WHERE status <> 'RESOLVED'")
    _table(
        rows,
        [
            "source_id",
            "status",
            "health",
            "breaker_state",
            "hours_since_success",
            "hours_since_change",
            "version",
            "record_count",
        ],
    )
    typer.echo("\nactive runs:")
    _table(active, ["source_id", "status", "current_step", "pct"])
    typer.echo("\nrecent batches:")
    _table(
        batches,
        ["created_at", "trigger", "requested_by", "status", "sources_run", "ok", "attention", "failed"],
    )
    typer.echo(f"\npending reviews: {pending}   open incidents: {incidents}")


@app.command()
def ask(question: str, session: str = typer.Option(None, help="Continue a conversation")) -> None:
    """Ask the analyst agent about ingestion status, fields, counts and data quality (aggregates only)."""
    from sanctions_agent.agent.analyst import AnalystAgent

    res = AnalystAgent().ask(question, user=f"cli:{_user()}", session_id=session)
    typer.echo(res["answer_markdown"])
    for c in res.get("citations") or []:
        typer.echo(f"  - {c['source']} (as of {c['as_of']})")
    typer.echo(f"\n[session {res['session_id']}]")


# ================================================================================ proposals
@proposals_app.command("list")
def proposals_list(
    status_: str = typer.Option("PENDING", "--status"), kind: str = typer.Option(None)
) -> None:
    from sanctions_agent.db.engine import fetch_all, tx

    with tx() as conn:
        rows = fetch_all(
            conn,
            """SELECT change_id, kind, source_id, status, proposed_by, created_at, title
            FROM proposed_change WHERE (%s = 'ALL' OR status = %s) AND (%s::text IS NULL OR kind = %s)
            ORDER BY created_at DESC LIMIT 200""",
            (status_, status_, kind, kind),
        )
    _table(rows, ["change_id", "kind", "source_id", "status", "proposed_by", "created_at", "title"])


@proposals_app.command("show")
def proposals_show(change_id: int) -> None:
    from sanctions_agent.db.engine import fetch_one, tx

    with tx() as conn:
        _echo_json(fetch_one(conn, "SELECT * FROM proposed_change WHERE change_id = %s", (change_id,)))


def _decide(
    change_id: int, approve: bool, reviewer: str, role: str, comment: str | None, resolution: str | None
) -> None:
    from sanctions_agent import review
    from sanctions_agent.db.engine import tx

    with tx(actor=reviewer) as conn:
        _echo_json(
            review.decide(
                conn,
                change_id,
                reviewer=reviewer,
                role=role,
                approve=approve,
                comment=comment,
                resolution=resolution,
            )
        )


@proposals_app.command("approve")
def proposals_approve(
    change_id: int,
    reviewer: str = typer.Option(..., help="Your user id (maker-checker: must differ from the proposer)"),
    role: str = typer.Option("reviewer", help="reviewer | admin (config changes need admin)"),
    comment: str = typer.Option(None),
    resolution: str = typer.Option(
        None, help="Removal confirmations: CONFIRMED | STILL_LISTED_ELSEWHERE | ..."
    ),
) -> None:
    _decide(change_id, True, reviewer, role, comment, resolution)


@proposals_app.command("reject")
def proposals_reject(
    change_id: int,
    reviewer: str = typer.Option(...),
    comment: str = typer.Option(..., help="Why"),
    role: str = typer.Option("reviewer"),
) -> None:
    _decide(change_id, False, reviewer, role, comment, None)


# ================================================================================ go-live checks
@app.command("verify-sources")
def verify_sources(
    source: list[str] = typer.Option(None, "--source", "-s", help="Limit to these sources (repeatable)"),
    write_known_paths: bool = typer.Option(False, help="Add newly seen element paths to schemas/known_paths"),
    out: Path = typer.Option(None, help="Write the full JSON report here"),
) -> None:
    """Live conformance probe (run on a network that can reach the publishers): download each structured list,
    validate, parse, and report unknown element paths, fill rates vs floors, suggested floors and whether the
    server supports conditional GET (BRD Q6). Nothing is written to the database."""
    from sanctions_agent.db.engine import tx
    from sanctions_agent.http.errors import FetchError
    from sanctions_agent.pipeline.runner import PipelineRunner, _adapter
    from sanctions_agent.quality.metrics import VersionMetrics
    from sanctions_agent.sources.base import ParseStats
    from sanctions_agent.sources.registry import list_sources

    with tx() as conn:
        specs = [
            s
            for s in list_sources(conn)
            if s.kind == "STRUCTURED_LIST" and (not source or s.source_id in source)
        ]
    work = get_settings().work_dir / "verify"
    work.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    failed = False
    for s in specs:
        r: dict[str, Any] = {"adapter_type": s.adapter_type.type_id}
        report[s.source_id] = r
        fc = s.config.fetch  # type: ignore[attr-defined]
        dest = work / f"{s.source_id}.raw"
        typer.echo(f"-- {s.source_id}: GET {fc.url}")
        try:
            res = PipelineRunner._default_fetcher(s).fetch_to_file(
                fc.url, dest, max_bytes=fc.max_mb * 1024 * 1024
            )
        except FetchError as e:
            r["error"] = f"{e.error_class}: {e.detail}"
            typer.echo(f"   FETCH FAILED {r['error']}")
            failed = True
            continue
        r.update(
            final_url=res.final_url,
            http_status=res.http_status,
            size_bytes=res.size_bytes,
            sha256=res.sha256,
            redirects=len(res.redirect_chain),
            content_type=res.content_type,
            conditional_get={
                "etag": bool(res.headers.get("etag")),
                "last_modified": bool(res.headers.get("last-modified")),
            },
        )
        adapter = _adapter(s)
        try:
            r["file_issues"] = [i.__dict__ for i in adapter.validate_file(dest)]
            info = adapter.read_info(dest)
            r["publication_marker"] = info.publication_marker
            stats = ParseStats()
            metrics = VersionMetrics()
            for rec in adapter.parse(dest, stats):
                metrics.add(rec.model_dump(mode="json"))
        except Exception as e:
            r["error"] = f"PARSER_ERROR: {e!r}"
            typer.echo(f"   PARSE FAILED {r['error']}")
            failed = True
            continue
        known = adapter.known_paths
        unknown = sorted(p for p in stats.paths if known and p not in known)
        floors = dict(getattr(getattr(s.config, "validation", None), "fill_floors", {}) or {})
        fill = {f"{et}.{fld}": v for et, metric, fld, v in metrics.rows() if metric == "fill_rate"}
        r.update(
            records=metrics.record_count,
            counts_by_type=dict(metrics.counts_by_type),
            warnings=dict(stats.warnings),
            unmapped_countries=len(stats.unmapped_countries),
            unknown_paths=unknown,
            fill_rates=fill,
            below_floor={
                k: {"fill": fill.get(k), "floor": f}
                for k, f in floors.items()
                if fill.get(k) is not None and fill[k] < f
            },
            suggested_floors={k: max(0.0, round(v - 0.05, 2)) for k, v in fill.items() if v >= 0.2},
        )
        typer.echo(
            f"   {metrics.record_count} records {dict(metrics.counts_by_type)}; marker={info.publication_marker}; "
            f"unknown paths={len(unknown)}; below floor={len(r['below_floor'])}; "
            f"conditional GET={r['conditional_get']}"
        )
        if write_known_paths and unknown:
            target = get_settings().schemas_dir / "known_paths" / f"{s.adapter_type.type_id}.txt"
            merged = sorted(set(known) | set(stats.paths))
            target.write_text(
                f"# Known element paths for {s.adapter_type.type_id} (reviewed). "
                "Updated by verify-sources - review the diff.\n" + "\n".join(merged) + "\n",
                encoding="utf-8",
            )
            typer.echo(f"   wrote {len(merged)} known paths to {target} - review the diff before committing")
        dest.unlink(missing_ok=True)
    if out:
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        typer.echo(f"report written to {out}")
    if failed:
        raise typer.Exit(1)


@app.command("pin-schemas")
def pin_schemas(
    source: str = typer.Option(..., "--source", "-s"),
    url: str = typer.Option(..., help="Publisher XSD URL (must be on the source's allowed hosts)"),
) -> None:
    """Download a publisher's XSD through the same guarded client and pin it under schemas/xsd/. Then set the
    source's validation.schema_file (Sources & schedules -> Validation) so every file is validated against it."""
    from lxml import etree

    from sanctions_agent.db.engine import tx
    from sanctions_agent.pipeline.runner import PipelineRunner
    from sanctions_agent.sources.registry import get_source
    from sanctions_agent.storage.blobstore import sha256_file

    with tx() as conn:
        spec = get_source(conn, source)
    target_dir = get_settings().schemas_dir / "xsd"
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f".{source}.download"
    PipelineRunner._default_fetcher(spec).fetch_to_file(url, tmp, max_bytes=20 * 1024 * 1024)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    etree.XMLSchema(etree.parse(str(tmp), parser))  # raises if it is not a usable schema
    final = target_dir / f"{source}.xsd"
    shutil.move(str(tmp), final)
    typer.echo(f"pinned {final} sha256={sha256_file(final)}")
    typer.echo(f'set validation.schema_file = "xsd/{source}.xsd" on {source}')


# ================================================================================ EU annex
@annex_app.command("import-csv")
def annex_import_csv(
    source: str = typer.Option(..., "--source", "-s", help="eu_annex_xlii or eu_annex_iv"),
    file: Path = typer.Option(..., exists=True, dir_okay=False),
    act_url: str = typer.Option(..., help="EUR-Lex URL of the act the entries come from (evidence)"),
    celex: str = typer.Option(None),
    operation: str = typer.Option("ADD", help="ADD | REMOVE"),
    proposed_by: str = typer.Option(None, help="Defaults to cli:<user>"),
) -> None:
    """Propose annex entries from an analyst CSV (columns: name, imo, flag, country, aliases). Entries are
    checked deterministically (IMO checksum) and filed as an ANNEX_ENTRY proposal for a second person to approve."""
    from sanctions_agent.db.engine import fetch_val, tx
    from sanctions_agent.sources.l1.eu_annex import entries_from_csv, propose_entries

    with tx() as conn:
        annex = fetch_val(conn, "SELECT config->>'annex' FROM source WHERE source_id = %s", (source,))
    if annex not in ("XLII", "IV"):
        raise typer.BadParameter(f"{source} is not an EU annex source")
    entries = entries_from_csv(file, annex)
    who = proposed_by or f"cli:{_user()}"
    with tx(actor=who) as conn:
        cid = propose_entries(
            conn,
            source_id=source,
            operation=operation.upper(),
            entries=entries,
            act={"url": act_url, "celex": celex, "title": celex or act_url},
            proposed_by=who,
        )
    ok = sum(1 for e in entries if e["ok"])
    typer.echo(f"proposal #{cid}: {ok} verified, {len(entries) - ok} failed checks")
    for e in entries:
        if not e["ok"]:
            typer.echo(f"  ! {e['name']}: {'; '.join(e['problems'])}")


@annex_app.command("extract-notice")
def annex_extract_notice(notice_id: int, annex: str = typer.Option(..., help="XLII | IV")) -> None:
    """Have the extractor agent pull annex entries from a collected EUR-Lex notice (verified, then proposed)."""
    import uuid

    from agents import RunContextWrapper

    from sanctions_agent.agent.context import AgentContext
    from sanctions_agent.agent.tools import extract_annex_act
    from sanctions_agent.db.engine import tx

    if annex.upper() not in ("XLII", "IV"):
        raise typer.BadParameter("annex must be XLII or IV")
    cycle_id = str(uuid.uuid4())
    with (
        tx() as conn
    ):  # a cycle row so the extraction's tool call and token cost are traced like any agent run
        conn.execute(
            "INSERT INTO agent_cycle (cycle_id, agent_name, trigger, gate_reasons, model)"
            " VALUES (%s, 'extractor', 'MANUAL', %s, %s)",
            (cycle_id, [f"cli annex extract notice {notice_id}"], get_settings().agent_model),
        )
    status_ = "FAILED"
    try:
        ctx = AgentContext(cycle_id=cycle_id, actor=f"cli:{_user()}")
        _echo_json(extract_annex_act(RunContextWrapper(ctx), notice_id, annex.upper()))  # type: ignore[arg-type]
        status_ = "SUCCEEDED"
    finally:
        with tx() as conn:
            conn.execute(
                "UPDATE agent_cycle SET status = %s, finished_at = now() WHERE cycle_id = %s",
                (status_, cycle_id),
            )


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
