"""Unit tests for backend/scripts/seed_spoolmandb_community_cache.py.

The script is a thin wrapper around spoolmandb_community_client's own
(already-tested) download/parse/build internals, so these tests focus on
its own contract: it writes the expected cache shape to the given path,
and never lets an exception escape (a failed build-time seed must not fail
the whole Docker build - see the script's module docstring).
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services import spoolmandb_community_client as smdb
from backend.scripts import seed_spoolmandb_community_cache as seed_script


@pytest.mark.asyncio
async def test_writes_expected_cache_shape(tmp_path):
    seed_path = tmp_path / "spoolmandb_community_seed.json"
    variants = [
        {
            "manufacturer": "Bambu Lab",
            "material": "PLA",
            "eans": ["6975337031345"],
            "eans_refill": [],
            "codes": ["CA19001"],
        }
    ]

    with (
        patch.object(seed_script.smdb, "_download_and_parse_variants", new=AsyncMock(return_value=variants)),
        patch.object(seed_script.smdb, "_seed_cache_path", lambda: seed_path),
    ):
        rc = await seed_script._main()

    assert rc == 0
    data = json.loads(seed_path.read_text())
    assert data["cache_version"] == smdb._CACHE_VERSION
    assert smdb.canon("6975337031345") in data["gtin_index"]
    assert "CA19001" in data["sku_index"]
    assert data["brands"] == ["Bambu Lab"]
    assert data["variants"] == variants


@pytest.mark.asyncio
async def test_download_failure_does_not_raise(tmp_path):
    """A network hiccup at build time must not fail the whole Docker build -
    the image just ships without a seed, same as before this script existed."""
    seed_path = tmp_path / "spoolmandb_community_seed.json"

    with (
        patch.object(
            seed_script.smdb, "_download_and_parse_variants", new=AsyncMock(side_effect=RuntimeError("offline"))
        ),
        patch.object(seed_script.smdb, "_seed_cache_path", lambda: seed_path),
    ):
        rc = await seed_script._main()

    assert rc == 0
    assert not seed_path.exists()


@pytest.mark.asyncio
async def test_zero_variants_does_not_raise_or_write(tmp_path):
    seed_path = tmp_path / "spoolmandb_community_seed.json"

    with (
        patch.object(seed_script.smdb, "_download_and_parse_variants", new=AsyncMock(return_value=[])),
        patch.object(seed_script.smdb, "_seed_cache_path", lambda: seed_path),
    ):
        rc = await seed_script._main()

    assert rc == 0
    assert not seed_path.exists()
