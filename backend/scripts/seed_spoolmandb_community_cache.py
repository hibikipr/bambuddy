#!/usr/bin/env python3
"""Bake a build-time SpoolmanDB-Community snapshot into the Docker image.

Bambuddy is frequently self-hosted air-gapped/offline. Without this, a
brand-new deployment that's never had network access has zero
SpoolmanDB-Community barcode coverage — its DATA_DIR cache starts empty, and
the runtime client's own offline fallback (`_load_stale_cached`) has nothing
to fall back to (see `spoolmandb_community_client._ensure_loaded`).

This script runs the exact same download/parse/build steps as a normal
runtime refresh (reusing the client's own internals, so the output is
byte-for-byte the same shape a real refresh would produce) and writes the
result to `spoolmandb_community_client._seed_cache_path()` — a location
inside the backend package itself, not the DATA_DIR volume, so it ships
with the image and survives regardless of what the DATA_DIR volume
contains at first boot.

Run at Docker build time (see Dockerfile), after `backend/` is copied in.
Deliberately never fails the build: a missing/stale seed just means a
fresh air-gapped install behaves exactly as it did before this script
existed (no SpoolmanDB-Community coverage until the first successful
online refresh) - strictly an enhancement, not a hard requirement.

SpoolmanDB-Community is MIT-licensed (Icezaza2543/SpoolmanDB-Community),
so redistributing a snapshot inside the image is clean. See
`seed_ofd_cache.py` for the equivalent OFD seed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from backend.app.services import spoolmandb_community_client as smdb

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seed_spoolmandb_community_cache")


async def _main() -> int:
    try:
        variants = await smdb._download_and_parse_variants()
        if not variants:
            log.warning("Parsed zero manufacturer files - skipping seed (build continues without one)")
            return 0
        gtin_index, sku_index = smdb._build_index(variants)
        brands = sorted({v["manufacturer"] for v in variants if v.get("manufacturer")})
    except Exception:
        log.warning("Failed to build SpoolmanDB-Community seed - build continues without one", exc_info=True)
        return 0

    path = smdb._seed_cache_path()
    _write_seed(path, gtin_index, sku_index, brands, variants)
    log.info(
        "Wrote SpoolmanDB-Community seed: %d GTINs, %d SKUs, %d brands -> %s",
        len(gtin_index),
        len(sku_index),
        len(brands),
        path,
    )
    return 0


def _write_seed(path: Path, gtin_index: dict, sku_index: dict, brands: list, variants: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(
            {
                "cache_version": smdb._CACHE_VERSION,
                "built_at": time.time(),
                "gtin_index": gtin_index,
                "sku_index": sku_index,
                "brands": brands,
                "variants": variants,
            }
        )
    )
    tmp_path.replace(path)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
