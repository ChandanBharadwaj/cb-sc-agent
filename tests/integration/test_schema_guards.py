import psycopg
import pytest

from sanctions_agent.db.engine import fetch_val, tx


def _seed_source(conn) -> None:
    conn.execute(
        "INSERT INTO source (source_id, display_name, adapter_type, level, kind, cadence, status)"
        " VALUES ('t_src', 'Test', 'un_xml', 1, 'STRUCTURED_LIST', '2 hours', 'ACTIVE')"
    )


def test_raw_artifact_is_append_only(db):
    with tx() as conn:
        conn.execute(
            "INSERT INTO raw_artifact (sha256, blob_uri, size_bytes) VALUES (%s, 'x', 1)", ("a" * 64,)
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tx() as conn:
        conn.execute("UPDATE raw_artifact SET size_bytes = 2")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tx() as conn:
        conn.execute("DELETE FROM raw_artifact")


def test_record_version_only_closes_once(db):
    with tx() as conn:
        _seed_source(conn)
        rv = fetch_val(
            conn,
            "INSERT INTO record_version (source_id, source_key, entity_type, valid_from_seq, content_hash, doc)"
            " VALUES ('t_src', 'k1', 'PERSON', 1, %s, '{}') RETURNING record_version_id",
            ("b" * 64,),
        )
        conn.execute("UPDATE record_version SET valid_to_seq = 2 WHERE record_version_id = %s", (rv,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tx() as conn:
        conn.execute("UPDATE record_version SET valid_to_seq = 3 WHERE record_version_id = %s", (rv,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege), tx() as conn:
        conn.execute("UPDATE record_version SET primary_name = 'x' WHERE record_version_id = %s", (rv,))


def test_audit_trigger_records_actor(db):
    with tx(actor="alice") as conn:
        _seed_source(conn)
        conn.execute("UPDATE source SET status = 'PAUSED' WHERE source_id = 't_src'")
    with tx() as conn:
        actor = fetch_val(conn, "SELECT actor FROM audit_log WHERE table_name = 'source' AND op = 'UPDATE'")
    assert actor == "alice"


def test_one_active_run_per_source(db):
    insert = """INSERT INTO ingestion_run (source_id, run_kind, trigger, requested_by, batch_id)
                VALUES ('t_src', 'LIST_INGEST', 'MANUAL', 'u', %s)"""
    with tx() as conn:
        _seed_source(conn)
        batch = fetch_val(
            conn, "INSERT INTO run_batch (trigger, requested_by) VALUES ('MANUAL', 'u') RETURNING batch_id"
        )
        conn.execute(insert, (batch,))
    with pytest.raises(psycopg.errors.UniqueViolation), tx() as conn:
        conn.execute(insert, (batch,))


def test_every_run_belongs_to_a_batch(db):
    with tx() as conn:
        _seed_source(conn)
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                "INSERT INTO ingestion_run (source_id, run_kind, trigger, requested_by)"
                " VALUES ('t_src', 'LIST_INGEST', 'MANUAL', 'u')"
            )
