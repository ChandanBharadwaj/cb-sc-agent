"""Data-level validation gates (FR-07): fill-rate floors, count bounds, schema drift, duplicates, checksums.

Outcomes:
* ``QUARANTINE`` - the file cannot be trusted; the last good version stays live (NFR-08)
* ``HOLD``       - plausible but unusually large change; a human must release it (circuit breaker)
* ``PASS``       - publish

Removals and additions are treated asymmetrically on purpose: a spike of removals is the classic
signature of a parser/ID bug (and unblocking a sanctioned party is a violation), so it holds when
EITHER the absolute or the percentage threshold is exceeded. Holding additions delays screening
of new designations, so additions hold only when BOTH thresholds are exceeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sanctions_agent.quality.metrics import VersionMetrics
from sanctions_agent.sources.base import Issue
from sanctions_agent.sources.config_models import ValidationConfig


@dataclass
class DiffPreview:
    previous_count: int
    added: int
    changed: int
    removed: int
    unchanged: int


@dataclass
class ValidationResult:
    outcome: str  # PASS | HOLD | QUARANTINE
    issues: list[Issue] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    primary_error: str | None = None

    def fail(self, issue: Issue, error_class: str) -> None:
        self.issues.append(issue)
        if self.outcome != "QUARANTINE":
            self.outcome = "QUARANTINE"
            self.primary_error = error_class


def validate_data(
    *,
    cfg: ValidationConfig,
    metrics: VersionMetrics,
    unknown_paths: list[str],
    duplicates: int,
    skipped_records: int,
    parse_warnings: dict[str, int],
    diff: DiffPreview | None,
    previous_fill: dict[tuple[str, str], float],
) -> ValidationResult:
    res = ValidationResult(outcome="PASS")
    n = metrics.record_count

    # --- volume ------------------------------------------------------------------------------
    if n < cfg.min_records:
        res.fail(
            Issue("COUNT_ANOMALY", "FAIL", f"only {n} records parsed; minimum is {cfg.min_records}", count=n),
            "COUNT_ANOMALY",
        )
    if skipped_records:
        share = skipped_records / max(1, n + skipped_records)
        sev = "FAIL" if share > 0.01 else "WARN"
        issue = Issue(
            "PARSE_WARNING",
            sev,
            f"{skipped_records} records could not be parsed ({share:.2%})",
            count=skipped_records,
        )
        if sev == "FAIL":
            res.fail(issue, "PARSER_ERROR")
        else:
            res.issues.append(issue)
    if duplicates:
        res.issues.append(
            Issue(
                "DUPLICATE_KEY", "WARN", f"{duplicates} duplicate stable keys (first kept)", count=duplicates
            )
        )
    for cat, c in parse_warnings.items():
        if cat not in ("PARSE_WARNING",):
            res.issues.append(
                Issue(
                    cat if cat in _CATS else "PARSE_WARNING", "WARN", f"{c} parser warnings: {cat}", count=c
                )
            )

    # --- schema drift ------------------------------------------------------------------------
    if unknown_paths:
        issue = Issue(
            "SCHEMA_DRIFT",
            "FAIL" if cfg.drift_policy == "quarantine" else "WARN",
            f"{len(unknown_paths)} element paths not seen before",
            count=len(unknown_paths),
            detail={"paths": unknown_paths[:50]},
        )
        if cfg.drift_policy == "quarantine":
            res.fail(issue, "SCHEMA_DRIFT")
        else:
            res.issues.append(issue)

    # --- fill-rate floors --------------------------------------------------------------------
    fills: dict[str, Any] = {}
    for key, floor in sorted(cfg.fill_floors.items()):
        et, fld = key.split(".", 1)
        rate = metrics.fill_rate(et, fld)
        fills[key] = {"rate": rate, "floor": floor}
        if rate is None:
            continue  # no records of this entity type in this list
        prev = previous_fill.get((et, fld))
        if rate < floor:
            res.fail(
                Issue(
                    "FILL_RATE_BELOW_FLOOR",
                    "FAIL",
                    f"{key} fill rate {rate:.1%} is below floor {floor:.0%}",
                    field=key,
                    detail={"rate": rate, "floor": floor, "previous": prev},
                ),
                "FILL_RATE_BELOW_FLOOR",
            )
        elif rate < floor + cfg.fill_warn_margin:
            res.issues.append(
                Issue(
                    "FILL_RATE_BELOW_FLOOR",
                    "WARN",
                    f"{key} fill rate {rate:.1%} is within {cfg.fill_warn_margin:.0%} of floor {floor:.0%}",
                    field=key,
                    detail={"rate": rate, "floor": floor, "previous": prev},
                )
            )

    # --- identifiers / normalisation ---------------------------------------------------------
    if metrics.imo_total and metrics.imo_valid < metrics.imo_total:
        bad = metrics.imo_total - metrics.imo_valid
        res.issues.append(
            Issue(
                "INVALID_CHECKSUM",
                "WARN",
                f"{bad} of {metrics.imo_total} IMO numbers fail the checksum",
                count=bad,
                field="VESSEL.imo",
            )
        )
    if metrics.lei_total and metrics.lei_valid < metrics.lei_total:
        bad = metrics.lei_total - metrics.lei_valid
        res.issues.append(
            Issue(
                "INVALID_CHECKSUM",
                "WARN",
                f"{bad} of {metrics.lei_total} LEIs fail the checksum",
                count=bad,
                field="ORGANIZATION.lei",
            )
        )
    unmapped = sum(metrics.unmapped_countries.values())
    if unmapped:
        res.issues.append(
            Issue(
                "UNMAPPED_COUNTRY",
                "WARN",
                f"{unmapped} country values could not be mapped to ISO codes",
                count=unmapped,
                detail={"top_values": metrics.unmapped_countries.most_common(10)},
            )
        )
    unknown_dob = metrics.dob_precision.get("UNKNOWN", 0)
    if unknown_dob:
        res.issues.append(
            Issue(
                "DATE_UNPARSEABLE",
                "WARN",
                f"{unknown_dob} people have only unparseable birth dates",
                count=unknown_dob,
                field="PERSON.dob",
            )
        )

    # --- large-change circuit breaker --------------------------------------------------------
    if diff is not None and diff.previous_count > 0:
        prev = diff.previous_count
        removed_pct = 100.0 * diff.removed / prev
        added_pct = 100.0 * diff.added / prev
        hold_reasons = []
        if diff.removed > cfg.max_removed_abs or removed_pct > cfg.max_removed_pct:
            hold_reasons.append(f"{diff.removed} removals ({removed_pct:.1f}% of {prev})")
        if diff.added > cfg.max_added_abs and added_pct > cfg.max_added_pct:
            hold_reasons.append(f"{diff.added} additions ({added_pct:.1f}% of {prev})")
        if hold_reasons:
            res.issues.append(
                Issue(
                    "COUNT_ANOMALY",
                    "WARN",
                    "large change held for review: " + "; ".join(hold_reasons),
                    count=diff.removed + diff.added,
                    detail={
                        "added": diff.added,
                        "removed": diff.removed,
                        "changed": diff.changed,
                        "previous_count": prev,
                    },
                )
            )
            if res.outcome == "PASS":
                res.outcome = "HOLD"
                res.primary_error = "COUNT_ANOMALY"

    res.report = {
        "outcome": res.outcome,
        "record_count": n,
        "counts_by_type": dict(metrics.counts_by_type),
        "fill_floors": fills,
        "diff_preview": diff.__dict__ if diff else None,
        "unknown_paths": unknown_paths[:50],
        "issues": [
            {
                "category": i.category,
                "severity": i.severity,
                "message": i.message,
                "count": i.count,
                "field": i.field,
            }
            for i in res.issues
        ],
    }
    return res


_CATS = {
    "SCHEMA_DRIFT",
    "FILL_RATE_BELOW_FLOOR",
    "COUNT_ANOMALY",
    "UNMAPPED_COUNTRY",
    "INVALID_CHECKSUM",
    "DATE_UNPARSEABLE",
    "DUPLICATE_KEY",
    "PUBLICATION_MARKER_REGRESSION",
    "ENRICHMENT_REVIEW_BACKLOG",
    "SCHEMA_INVALID",
    "PARSE_WARNING",
    "MISSING_REQUIRED_FIELD",
}
