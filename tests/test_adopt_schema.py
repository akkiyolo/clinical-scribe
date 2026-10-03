"""scripts/adopt_schema.py: bring a schema created outside Alembic under migration control."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from scripts.adopt_schema import INITIAL_FOREIGN_KEYS, INITIAL_REVISION, adopt

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
ON_POSTGRES = PG_URL.startswith("postgresql")


class TestDecisions:
    def test_empty_database_is_left_to_migrations(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
        with engine.begin() as conn:
            assert "empty database" in adopt(conn)

    def test_managed_database_is_untouched(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'managed.db').as_posix()}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            conn.execute(text("CREATE TABLE users (id INTEGER)"))
            assert "nothing to do" in adopt(conn)

    def test_partial_schema_is_refused(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'partial.db').as_posix()}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (id INTEGER)"))
            with pytest.raises(SystemExit, match="Refusing to guess"):
                adopt(conn)


@pytest.mark.skipif(not ON_POSTGRES, reason="needs PostgreSQL; run with TEST_DATABASE_URL")
class TestAdoptOnPostgres:
    """Rebuild the 'created outside Alembic' state on a scratch database and upgrade it."""

    @pytest.fixture
    def legacy_url(self):
        name = f"clinicalscribe_adopt_{uuid.uuid4().hex[:8]}"
        server = make_url(PG_URL).set(drivername="postgresql+psycopg")
        admin = create_engine(server, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        url = server.set(database=name)
        try:
            self._alembic(url, "upgrade", INITIAL_REVISION)
            engine = create_engine(url)
            with engine.begin() as conn:
                # Same shape as a create_all() database: no FKs, no alembic_version.
                for table, column, _ in INITIAL_FOREIGN_KEYS:
                    conn.execute(
                        text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{table}_{column}_fkey"')
                    )
                conn.execute(text("DROP TABLE alembic_version"))
            engine.dispose()
            yield url
        finally:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            admin.dispose()

    @staticmethod
    def _alembic(url, *args):
        env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
        subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True,
            capture_output=True,
        )  # fmt: skip

    def test_legacy_schema_is_adopted_and_upgrades_to_head(self, legacy_url):
        engine = create_engine(legacy_url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, full_name, is_active) "
                    "VALUES (:id, 'kept@example.com', 'x', 'patient', 'Kept User', true)"
                ),
                {"id": uuid.uuid4()},
            )
            assert "added 14 foreign keys" in adopt(conn)
        with engine.begin() as conn:
            assert "nothing to do" in adopt(conn)  # idempotent

        self._alembic(legacy_url, "upgrade", "head")

        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            kept = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
            fks = {
                (table, tuple(fk["constrained_columns"]))
                for table in inspect(conn).get_table_names()
                for fk in inspect(conn).get_foreign_keys(table)
            }
        engine.dispose()
        assert head == "c91d5e2a7f10" and kept == 1
        assert {(t, (c,)) for t, c, _ in INITIAL_FOREIGN_KEYS} <= fks

    def test_orphaned_rows_are_reported_not_deleted(self, legacy_url):
        engine = create_engine(legacy_url)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO patient_profiles (user_id) VALUES (:missing)"),
                {"missing": uuid.uuid4()},
            )
        with engine.begin() as conn:
            with pytest.raises(SystemExit, match="patient_profiles.user_id"):
                adopt(conn)
        with engine.connect() as conn:
            assert "alembic_version" not in inspect(conn).get_table_names()
            assert conn.execute(text("SELECT count(*) FROM patient_profiles")).scalar_one() == 1
        engine.dispose()
