#!/usr/bin/env python3
"""Bake a build-time OFD (Open Filament Database) snapshot into the image.

Bambuddy is frequently self-hosted air-gapped/offline. Without this, a
brand-new deployment that's never had network access has zero OFD barcode
coverage — its DATA_DIR cache starts empty, and the runtime client's own
offline fallback (`_load_stale_cached`) has nothing to fall back to (see
`ofd_client._ensure_loaded`).

This script runs the exact same download/build steps as a normal runtime
refresh (reusing the client's own internals, so the output is
byte-for-byte the same shape a real refresh would produce) and writes the
result to `ofd_client._seed_cache_path()` — a location inside the backend
package itself, not the DATA_DIR volume, so it ships with the image and
survives regardless of what the DATA_DIR volume contains at first boot.

Run at Docker build time (see Dockerfile), after `backend/` is copied in.
Deliberately never fails the build: a missing/stale seed just means a
fresh air-gapped install behaves exactly as it did before this script
existed (no OFD coverage until the first successful online refresh) -
strictly an enhancement, not a hard requirement.

OFD (github.com/OpenFilamentCollective/open-filament-database) is
MIT-licensed - both the code and the data itself, per the repo's own
README: "The data is free to use, redistribute, and embed in commercial
products; attribution is appreciated but not required." Redistributing a
snapshot inside the image is clean, same as SpoolmanDB-Community's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from backend.app.services import ofd_client as ofd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seed_ofd_cache")


async def _main() -> int:
    try:
        all_json = await ofd._download_all_json()
        gtin_index, article_index, variant_codes = ofd._build_index(all_json)
        brands = sorted({b["name"] for b in all_json.get("brands", []) if b.get("name")})
        if not gtin_index and not article_index:
            log.warning("Parsed zero entries - skipping seed (build continues without one)")
            return 0
    except Exception:
        log.warning("Failed to build OFD seed - build continues without one", exc_info=True)
        return 0

    path = ofd._seed_cache_path()
    _write_seed(path, gtin_index, article_index, variant_codes, brands)
    log.info(
        "Wrote OFD seed: %d GTINs, %d article numbers, %d brands -> %s",
        len(gtin_index),
        len(article_index),
        len(brands),
        path,
    )
    return 0


def _write_seed(path: Path, gtin_index: dict, article_index: dict, variant_codes: dict, brands: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(
            {
                "cache_version": ofd._CACHE_VERSION,
                "built_at": time.time(),
                "gtin_index": gtin_index,
                "article_index": article_index,
                "variant_codes": variant_codes,
                "brands": brands,
            }
        )
    )
    tmp_path.replace(path)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
