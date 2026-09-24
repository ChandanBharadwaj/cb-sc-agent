# Source licence register (NFR-06 / NFR-07)

Every source is recorded here and in the database. `source.licence` and `source.licence_status` are shown on
each source's page in the console. A source whose terms forbid production use is never enabled
automatically. Such sources ship **DISABLED** with the reason recorded (`status_reason`).

`licence_status` values:
* `OK`: pipeline use allowed.
* `ATTRIBUTION_REQUIRED`: allowed, and downstream outputs must credit the source.
* `LEGAL_REVIEW_REQUIRED`: in use or available, pending a Legal opinion.
* `NOT_PERMITTED`: must not be enabled.

## Level 1: official lists

| Source id | Publisher / file | Licence | Status | Obligations and notes |
|---|---|---|---|---|
| `ofac_sdn`, `ofac_cons` | US Treasury OFAC, Sanctions List Service (SDN / Consolidated Advanced XML) | US federal data, public domain | OK | SDN is the operative US list |
| `un_sc` | UN Security Council Consolidated List (`scsanctions.un.org`) | UN website terms ("personal, non-commercial use") | **LEGAL_REVIEW_REQUIRED** | BRD Q5: Legal to confirm internal compliance use. No re-publication outside screening until cleared |
| `uk_fcdo` | FCDO UK Sanctions List (XML) | Open Government Licence v3.0 | OK | Attribution statement in any published output |
| `eu_fsf` | European Commission Financial Sanctions File (FSF XML) | Commission reuse policy (the FSF CSV is CC BY 4.0) | ATTRIBUTION_REQUIRED | EU law is authentic only in the Official Journal; the FSF is a consolidated convenience file |
| `us_csl` | trade.gov Consolidated Screening List (bulk JSON) | US federal data, public domain | OK | Explicitly **non-authoritative**. We keep only BIS/State lists (OFAC is taken direct). BIS/State lists derive force from the Federal Register |
| `eu_annex_xlii`, `eu_annex_iv` | EU Reg. 833/2014 Annex XLII (vessels) / Annex IV (entities), built from Official Journal acts | EU reuse policy, attribution | ATTRIBUTION_REQUIRED | Entries are extracted from OJ acts, verified and **approved by a person**. The EU Sanctions Map (`www.sanctionsmap.eu`) is used only for leads: no terms, not authoritative |

## Level 2: notices and legal evidence

| Source id | Source | Licence | Status | Notes |
|---|---|---|---|---|
| `fr_notices` | Federal Register API (OFAC, BIS documents) | US public domain | OK | |
| `ofac_recent_actions` | OFAC Recent Actions (HTML) | Official US government site | OK | Also read from linked Treasury press releases for listing reasons |
| `un_list_updates` | UN list-updates log, narrative summaries | UN website terms | **LEGAL_REVIEW_REQUIRED** | Used as de-listing evidence; review before re-publishing text |
| `uk_notices` | gov.uk search/content API (financial sanctions notices) | Open Government Licence v3.0 | OK | |
| `eurlex_oj` | EUR-Lex OJ L-series RSS and act texts | EU reuse, attribution | ATTRIBUTION_REQUIRED | The only cryptographically signed publisher (signed OJ since 2013) |
| `eu_fsf_rss` | EU FSF RSS (early-pull signal only) | Commission reuse policy | OK | Triggers an early FSF pull; nothing is stored beyond the signal |

## Level 2: free enrichment

| Source id | Source | Licence | Status | Personal data (NFR-07) |
|---|---|---|---|---|
| `gleif` | GLEIF API (LEI, legal form, registration, parents) | CC0 1.0 | OK | Organisations only |
| `companies_house` | UK Companies House API (company profile) | Open Government Licence v3.0 | OK (needs `SANCTIONS_COMPANIES_HOUSE_API_KEY`) | The batch job stores **company profiles only**. Officers and PSC (people) are fetched **on demand per hit**, logged in `enrichment_request` with the requester and reason |
| `faa_registry` | FAA releasable aircraft database (daily ZIP) | US public domain | OK | Only rows matching listed aircraft (by serial or tail number) are stored |
| `wikidata` | Wikidata SPARQL | CC0 1.0 | OK | Vessels (by IMO) and organisations in batch. **People only on demand** |
| `icij` | ICIJ Offshore Leaks database | ODbL 1.0 / CC BY-SA (**share-alike**) | **LEGAL_REVIEW_REQUIRED, DISABLED** | Leads only, always human-reviewed. Enable only after Legal confirms share-alike obligations do not attach to our outputs |
| `internal_ais` | Our own AIS / vessel master | Internal | DISABLED (interface stub) | Connect to the internal service, then activate |

## Manual-only (never automated)

These sources' terms bar automated or commercial reuse, so analysts use them by hand; the platform does
not fetch them: Equasis, IMO GISIS, Paris/Tokyo MoU inspection databases, class and P&I registers, national
company registers (EGRUL, NECIPS, MCA, ACRA), EU beneficial-owner registers (legitimate-interest requests
only), investigative shadow-fleet sources (Ukraine GUR portal, KSE Institute, C4ADS), GDELT/news.

**Not to be used in production without a licence:** Global Fishing Watch, OpenSky, MarineTraffic, OpenSanctions
bulk data (CC BY-NC), OpenCorporates free keys. Paid Level 3 sources are out of scope for this service.

## Personal data (GDPR, NFR-07)

* **List data** (names, dates of birth, identifiers of listed persons) is processed for legal-obligation
  screening.
* **Enrichment about people** (officers, PSC, Wikidata people) is never bulk-copied. It is fetched per
  hit on request from a reviewer (`POST /api/enrich/on-demand`), and every request is logged with the
  user and reason. Record the lawful basis in the DPIA.
* **The Q&A assistant** can only read aggregate views. The database role `sanctions_analyst` has no
  access to record-level tables.
* **Retention:** raw files are write-once, for audit (BRD §9.4). Staging rows are purged after publish.
  HELD / QUARANTINED staging is kept 7 days. Agent traces are partitioned monthly so they can be dropped
  on schedule.

## Open items for Legal (BRD Q5)

1. UN website terms ("personal, non-commercial"): internal compliance screening use of `un_sc` and
   `un_list_updates`.
2. ICIJ share-alike (ODbL / CC BY-SA): whether enabling `icij` as reviewer-only leads creates obligations.
3. Attribution wording for EU and UK data in any externally shared output.
