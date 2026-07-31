"""Regression test for the spool_code backfill migration running through the
real run_migrations entrypoint, not in isolation.

test_spool_code_backfill_migration.py drives _migrate_backfill_spool_codes
directly against a two-table (spool, spool_code) engine, which doesn't cover
the ordering dependency on run_migrations having already added the barcode
column and created the spool_code table earlier in the same pass — this test
exercises the full migration sequence against the complete schema instead.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.app.core.database import run_migrations


def _register_all_models():
    """run_migrations touches multiple tables; the full schema must exist."""
    from backend.app.models import (  # noqa: F401
        ams_history,
        ams_label,
        api_key,
        archive,
        color_catalog,
        external_link,
        filament,
        group,
        kprofile_note,
        maintenance,
        notification,
        notification_template,
        print_log,
        print_queue,
        printer,
        project,
        project_bom,
        settings,
        slot_preset,
        smart_plug,
        smart_plug_energy_snapshot,
        spool,
        spool_assignment,
        spool_catalog,
        spool_code,
        spool_k_profile,
        spool_usage_history,
        spoolbuddy_device,
        user,
        user_email_pref,
        virtual_printer,
    )


async def _engine():
    from backend.app.core.database import Base

    _register_all_models()

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return eng


async def _insert_spool_via_orm(engine, *, spool_id: int, barcode: str | None) -> None:
    from backend.app.models.spool import Spool

    async with AsyncSession(engine) as session:
        session.add(Spool(id=spool_id, material="PLA", label_weight=1000, barcode=barcode))
        await session.commit()


async def _codes_for(engine, spool_id: int) -> list[tuple]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT code, kind, is_primary FROM spool_code WHERE spool_id = :id"), {"id": spool_id}
        )
        return result.all()


async def test_backfill_runs_through_full_migration_sequence(monkeypatch):
    """Covers the ordering dependency: spool.barcode and spool_code both need
    to exist by the time the backfill step runs, within one run_migrations
    pass on a schema that predates both."""
    # Pin the SQLite branch regardless of the ambient DATABASE_URL — this test
    # always drives run_migrations against a hand-built SQLite engine, but
    # is_sqlite() reads the *configured* database_url, not the engine actually
    # in use. Without this, a Postgres dev/CI environment takes the Postgres
    # branch (e.g. the color_catalog.hex_color ALTER COLUMN) against a SQLite
    # connection and fails.
    monkeypatch.setattr("backend.app.core.database.is_sqlite", lambda: True)
    engine = await _engine()
    try:
        await _insert_spool_via_orm(engine, spool_id=1, barcode="6938936716785")
        await _insert_spool_via_orm(engine, spool_id=2, barcode="ALZMNTABS01")

        async with engine.begin() as conn:
            await run_migrations(conn)

        gtin_rows = await _codes_for(engine, 1)
        assert len(gtin_rows) == 1
        assert gtin_rows[0] == ("6938936716785", "gtin", True)

        sku_rows = await _codes_for(engine, 2)
        assert len(sku_rows) == 1
        assert sku_rows[0] == ("ALZMNTABS01", "sku", True)
    finally:
        await engine.dispose()


async def test_backfill_through_run_migrations_is_idempotent(monkeypatch):
    monkeypatch.setattr("backend.app.core.database.is_sqlite", lambda: True)
    engine = await _engine()
    try:
        await _insert_spool_via_orm(engine, spool_id=1, barcode="6938936716785")

        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.begin() as conn:
            await run_migrations(conn)  # second pass should be a no-op

        rows = await _codes_for(engine, 1)
        assert len(rows) == 1
    finally:
        await engine.dispose()


async def test_spool_code_kind_check_constraint_enforced_after_migration(monkeypatch):
    """Simulates a pre-existing SQLite DB from before the kind CHECK
    constraint existed: spool_code created without it, one valid row already
    in place. run_migrations must recreate the table with the constraint
    baked in, preserving that row, and reject a bogus kind afterward."""
    monkeypatch.setattr("backend.app.core.database.is_sqlite", lambda: True)
    engine = await _engine()
    try:
        await _insert_spool_via_orm(engine, spool_id=1, barcode=None)
        async with engine.begin() as conn:
            # Recreate spool_code the "old" way - no CHECK constraint - and
            # seed it with a row that must survive the migration's rebuild.
            await conn.execute(text("DROP TABLE spool_code"))
            await conn.execute(
                text("""
                CREATE TABLE spool_code (
                    id INTEGER PRIMARY KEY,
                    spool_id INTEGER NOT NULL REFERENCES spool(id) ON DELETE CASCADE,
                    code VARCHAR(64) NOT NULL,
                    kind VARCHAR(16) NOT NULL,
                    is_refill BOOLEAN DEFAULT 0,
                    is_primary BOOLEAN DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (spool_id, code)
                )
            """)
            )
            await conn.execute(
                text("INSERT INTO spool_code (spool_id, code, kind, is_primary) VALUES (1, 'PRE-EXISTING', 'sku', 1)")
            )

        async with engine.begin() as conn:
            await run_migrations(conn)

        rows = await _codes_for(engine, 1)
        assert rows == [("PRE-EXISTING", "sku", True)]

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO spool_code (spool_id, code, kind, is_primary) VALUES (1, 'BOGUS', 'bogus', 0)")
                )
    finally:
        await engine.dispose()


async def test_spool_code_kind_check_migration_is_idempotent(monkeypatch):
    monkeypatch.setattr("backend.app.core.database.is_sqlite", lambda: True)
    engine = await _engine()
    try:
        async with engine.begin() as conn:
            await run_migrations(conn)
        async with engine.begin() as conn:
            await run_migrations(conn)  # second pass should be a no-op

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO spool_code (spool_id, code, kind, is_primary) VALUES (1, 'X', 'bogus', 0)")
                )
    finally:
        await engine.dispose()
