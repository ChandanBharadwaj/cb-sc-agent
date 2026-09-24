"""FAA releasable aircraft database (public domain): registrant, address, Mode S for US-registered
aircraft named on OFAC lists. The daily bulk ZIP is archived; only matched rows are kept."""

from __future__ import annotations

import csv
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from sanctions_agent.db.engine import tx
from sanctions_agent.enrichment.base import Enricher, EnrichResult, Subject, archive_raw, compact
from sanctions_agent.http.client import retry_call


def _rows(zf: zipfile.ZipFile, name: str) -> Any:
    member = next((n for n in zf.namelist() if n.upper().endswith(name)), None)
    if member is None:
        return iter(())
    with zf.open(member) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))
        for r in reader:
            yield {(k or "").strip(): (v or "").strip() for k, v in r.items()}


class FaaRegistryEnricher(Enricher):
    provider = "faa_registry"
    licence = "US federal data - public domain"

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.by_tail: dict[str, dict[str, Any]] = {}
        self.by_serial: dict[str, list[dict[str, Any]]] = {}

    def prepare(self, subjects: list[Subject]) -> dict[str, Any]:
        tails = {
            compact(i["value"]).removeprefix("N")
            for s in subjects
            for i in s.ids("AIRCRAFT_TAIL")
            if compact(i["value"]).startswith("N")
        }
        serials = {compact(i["value"]) for s in subjects for i in s.ids("AIRCRAFT_MSN")}
        url = (
            self.config.download_url or f"{self.config.base_url.rstrip('/')}/database/ReleasableAircraft.zip"
        )
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "faa.zip"
            res = retry_call(
                lambda: self.fetcher.fetch_to_file(url, dest, max_bytes=400 * 1024 * 1024), self.retry
            )
            with tx() as conn:
                archive_raw(conn, dest.read_bytes(), "application/zip")
            with zipfile.ZipFile(dest) as zf:
                models = {r.get("CODE"): r for r in _rows(zf, "ACFTREF.TXT")}
                for r in _rows(zf, "MASTER.TXT"):
                    n, serial = compact(r.get("N-NUMBER")), compact(r.get("SERIAL NUMBER"))
                    if n in tails or serial in serials:
                        ref = models.get(r.get("MFR MDL CODE")) or {}
                        row = {
                            "n_number": "N" + n,
                            "serial": r.get("SERIAL NUMBER"),
                            "manufacturer": ref.get("MFR"),
                            "model": ref.get("MODEL"),
                            "year": r.get("YEAR MFR"),
                            "registrant": r.get("NAME"),
                            "street": r.get("STREET"),
                            "city": r.get("CITY"),
                            "state": r.get("STATE"),
                            "country": r.get("COUNTRY"),
                            "mode_s_hex": r.get("MODE S CODE HEX"),
                            "status_code": r.get("STATUS CODE"),
                            "cert_issue_date": r.get("CERT ISSUE DATE"),
                        }
                        if n in tails:
                            self.by_tail[n] = row
                        self.by_serial.setdefault(serial, []).append(row)
        return {
            "file_sha256": res.sha256,
            "matched_tails": len(self.by_tail),
            "serial_hits": len(self.by_serial),
        }

    def enrich(self, subject: Subject) -> EnrichResult:
        for t in subject.ids("AIRCRAFT_TAIL"):
            n = compact(t["value"])
            if n.startswith("N") and n.removeprefix("N") in self.by_tail:
                row = self.by_tail[n.removeprefix("N")]
                return EnrichResult(
                    "AUTO_ACCEPTED",
                    "TAIL_NUMBER",
                    row["n_number"],
                    1.0,
                    row,
                    f"https://registry.faa.gov/aircraftinquiry/Search/NNumberResult?nNumberTxt={row['n_number']}",
                )
        model = ((subject.doc.get("aircraft") or {}).get("model") or "").upper()
        for m in subject.ids("AIRCRAFT_MSN"):
            for row in self.by_serial.get(compact(m["value"]), []):
                same_model = bool(
                    model
                    and row.get("model")
                    and (row["model"].upper() in model or model in row["model"].upper())
                )
                return EnrichResult(
                    "AUTO_ACCEPTED" if same_model else "NEEDS_REVIEW",
                    "SERIAL_NUMBER",
                    row["n_number"],
                    1.0 if same_model else 0.9,
                    row,
                    f"https://registry.faa.gov/aircraftinquiry/Search/NNumberResult?nNumberTxt={row['n_number']}",
                    review_note=None
                    if same_model
                    else "serial numbers repeat across manufacturers - confirm model",
                )
        return EnrichResult("NO_MATCH", "TAIL_OR_SERIAL", score=0.0)
