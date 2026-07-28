"""Unit tests for backend/scripts/seed_ofd_cache.py.

The script is a thin wrapper around ofd_client's own (already-tested)
download/build internals, so these tests focus on its own contract: it
writes the expected cache shape to the given path, and never lets an
exception escape (a failed build-time seed must not fail the whole
Docker build - see the script's module docstring).
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import ofd_client as ofd
from backend.scripts import seed_ofd_cache as seed_script
from backend.tests.unit.test_ofd_client import SAMPLE_ALL_JSON


@pytest.mark.asyncio
async def test_writes_expected_cache_shape(tmp_path):
    seed_path = tmp_path / "ofd_seed.json"

    with (
        patch.object(seed_script.ofd, "_download_all_json", new=AsyncMock(return_value=SAMPLE_ALL_JSON)),
        patch.object(seed_script.ofd, "_seed_cache_path", lambda: seed_path),
    ):
        rc = await seed_script._main()

    assert rc == 0
    data = json.loads(seed_path.read_text())
    assert data["cache_version"] == ofd._CACHE_VERSION
    assert ofd.canon("06938936716785") in data["gtin_index"]
    assert data["brands"] == ["Sunlu"]


@pytest.mark.asyncio
async def test_download_failure_does_not_raise(tmp_path):
    """A network hiccup at build time must not fail the whole Docker build -
    the image just ships without a seed, same as before this script existed."""
    seed_path = tmp_path / "ofd_seed.json"

    with (
        patch.object(seed_script.ofd, "_download_all_json", new=AsyncMock(side_effect=RuntimeError("offline"))),
        patch.object(seed_script.ofd, "_seed_cache_path", lambda: seed_path),
    ):
        rc = await seed_script._main()

    assert rc == 0
    assert not seed_path.exists()


@pytest.mark.asyncio
async def test_zero_entries_does_not_raise_or_write(tmp_path):
    seed_path = tmp_path / "ofd_seed.json"

    with (
        patch.object(seed_script.ofd, "_download_all_json", new=AsyncMock(return_value={})),
        patch.object(seed_script.ofd, "_seed_cache_path", lambda: seed_path),
    ):
        rc = await seed_script._main()

    assert rc == 0
    assert not seed_path.exists()
