"""Open Filament Database (OFD) barcode lookup client.

The OFD (https://openfilamentdatabase.org) publishes a data dump of retail
spool barcodes (GTINs) joined to brand / material / colour / weight. This
client downloads it, builds a barcode -> fields index, and caches both the
raw dump and the built index on disk with a 24h TTL — refreshed lazily on
the next lookup once stale, since this backend has no scheduler/cron.

Each OFD ``sizes`` row can carry a ``gtin`` (retail barcode) AND/OR an
``article_number`` (manufacturer SKU — what SpoolmanDB-Community calls
``codes``) AND a ``spool_refill`` flag, independently of each other. Multiple
``sizes`` rows (one per package weight) share one ``variant_id`` (one per
colour), so this client groups all codes sharing a ``variant_id`` together —
a hit on any one of them (via `lookup`/`lookup_article`) also returns every
sibling code for that colour, letting a scan of an *unfamiliar* code still
resolve once *any* of its siblings has been seen before (see `_resolve_barcode`
in `backend/app/api/routes/inventory.py`).

Ported from the standalone `filament_to_bambuddy` companion app's `ofd.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

from backend.app.core.paths import resolve_data_dir

logger = logging.getLogger(__name__)

OFD_ALL_URL = "https://api.openfilamentdatabase.org/json/all.json"
OFD_TTL_SECONDS = 24 * 3600

# Generous cap against a malicious/broken upstream serving an oversized or
# infinite response — the real dump is a small fraction of this. Mirrors the
# same guard already on the SpoolmanDB-Community client's tarball download.
_MAX_ALL_JSON_BYTES = 128 * 1024 * 1024

# Bump whenever the on-disk cache shape changes, so an old cache file (e.g.
# pre-dating article_number/variant-code support) is treated as stale and
# rebuilt instead of being misread.
_CACHE_VERSION = 2

# In-process cache so we don't rebuild the index on every request.
_gtin_index: dict[str, dict] | None = None
_article_index: dict[str, dict] | None = None
_variant_codes: dict[str, list[dict]] | None = None
_brands: list[str] | None = None
_index_loaded_at = 0.0
_refresh_lock = asyncio.Lock()


def _cache_path() -> Path:
    return resolve_data_dir() / "ofd_cache.json"


def _seed_cache_path() -> Path:
    """Path to the build-time seed snapshot baked into the image itself
    (not the DATA_DIR volume) — see `backend/scripts/seed_ofd_cache.py` and
    its own module docstring for why this exists and how it's produced.
    """
    return Path(__file__).parent / "ofd_seed.json"


def canon(barcode: str) -> str:
    """Canonical GTIN form for matching: digits only, leading zeros stripped.

    Makes a UPC-A (12-digit) barcode and its EAN-13 (leading-zero) form
    compare equal. Also used to canonicalize `Spool.barcode` on write so the
    native-inventory lookup path compares like-for-like against OFD.
    """
    digits = re.sub(r"\D", "", barcode or "")
    return digits.lstrip("0") or "0"


def _hex_to_rgba(color_hex) -> str | None:
    if isinstance(color_hex, list):
        color_hex = color_hex[0] if color_hex else None
    if not isinstance(color_hex, str):
        return None
    h = color_hex.lstrip("#")
    if len(h) == 6 and re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        return h.upper() + "FF"  # RRGGBBAA, opaque
    return None


def _subtype_from(filament_name: str, material: str) -> str | None:
    """Best-effort subtype: the filament name minus the material word."""
    if not filament_name:
        return None
    s = filament_name
    if material:
        s = re.sub(rf"\b{re.escape(material)}\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" -+")
    return s or None


def _build_index(all_json: dict) -> tuple[dict[str, dict], dict[str, dict], dict[str, list[dict]]]:
    """Build (gtin_index, article_index, variant_codes) from the OFD all.json dump.

    ``gtin_index`` / ``article_index`` map a canonicalized code to
    ``{"fields": {...}, "variant_id": str}`` — fields are computed per
    *size* row (e.g. `label_weight` legitimately differs across package
    sizes of the same colour), so each code keeps its own accurate fields.

    ``variant_codes`` maps ``variant_id`` -> every code (GTIN or SKU/article,
    across every package size) sharing that colour, so a hit on any one code
    can recover its siblings for cross-referencing and storage.
    """
    brands = {b["id"]: b for b in all_json.get("brands", []) if "id" in b}
    filaments = {f["id"]: f for f in all_json.get("filaments", []) if "id" in f}
    variants = {v["id"]: v for v in all_json.get("variants", []) if "id" in v}

    gtin_index: dict[str, dict] = {}
    article_index: dict[str, dict] = {}
    variant_codes: dict[str, list[dict]] = {}

    for size in all_json.get("sizes", []):
        gtin = size.get("gtin")
        article = size.get("article_number")
        if not gtin and not article:
            continue

        variant_id = size.get("variant_id")
        variant = variants.get(variant_id)
        if not variant:
            continue
        # Always key/store variant_id as a string: dict keys become strings
        # after a JSON cache round-trip regardless of the source type, so
        # storing anything else here would silently break get()-lookups the
        # moment the cache is reloaded from disk.
        variant_id = str(variant_id)
        fil = filaments.get(variant.get("filament_id"))
        if not fil:
            continue
        brand = brands.get(fil.get("brand_id"))
        material = fil.get("material") or ""

        fields: dict = {"material": material} if material else {}
        if brand and brand.get("name"):
            fields["brand"] = brand["name"]
        sub = _subtype_from(fil.get("name", ""), material)
        if sub:
            fields["subtype"] = sub
        if variant.get("name"):
            fields["color_name"] = variant["name"]
        rgba = _hex_to_rgba(variant.get("color_hex"))
        if rgba:
            fields["rgba"] = rgba
        if size.get("filament_weight"):
            try:
                fields["label_weight"] = int(round(float(size["filament_weight"])))
            except (TypeError, ValueError):
                pass
        for src, dst in (
            ("min_print_temperature", "nozzle_temp_min"),
            ("max_print_temperature", "nozzle_temp_max"),
        ):
            if fil.get(src) is not None:
                try:
                    fields[dst] = int(fil[src])
                except (TypeError, ValueError):
                    pass

        is_refill = bool(size.get("spool_refill"))
        codes_for_variant = variant_codes.setdefault(variant_id, [])

        if gtin:
            canonical_gtin = canon(gtin)
            gtin_index[canonical_gtin] = {"fields": fields, "variant_id": variant_id}
            if not any(c["code"] == canonical_gtin for c in codes_for_variant):
                codes_for_variant.append({"code": canonical_gtin, "kind": "gtin", "is_refill": is_refill})
        if article:
            normalized_article = article.strip().upper()
            article_index[normalized_article] = {"fields": fields, "variant_id": variant_id}
            if not any(c["code"] == normalized_article for c in codes_for_variant):
                codes_for_variant.append({"code": normalized_article, "kind": "sku", "is_refill": is_refill})

    return gtin_index, article_index, variant_codes


def _read_cache_file() -> dict | None:
    """Parse the cache file and check its version, ignoring TTL. Returns the
    raw dict, or None if missing/corrupt/wrong-version — those are the only
    conditions that make a cache file truly unusable; staleness alone does not."""
    path = _cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("cache_version") != _CACHE_VERSION:
            return None
        return data
    except Exception:
        return None


def _cache_tuple(data: dict) -> tuple[dict, dict, dict, list]:
    return (
        data.get("gtin_index", {}),
        data.get("article_index", {}),
        data.get("variant_codes", {}),
        data.get("brands", []),
    )


def _load_cached() -> tuple[dict, dict, dict, list] | None:
    """Return the cache contents if present and fresh (within TTL), else None."""
    data = _read_cache_file()
    if data is None:
        return None
    if time.time() - data.get("built_at", 0) > OFD_TTL_SECONDS:
        return None
    return _cache_tuple(data)


def _load_stale_cached() -> tuple[dict, dict, dict, list] | None:
    """Return the cache contents regardless of TTL — a last-resort fallback
    for when a refresh attempt fails (e.g. offline), so a working-but-old
    index is still served instead of dropping to "no match" for everything."""
    data = _read_cache_file()
    return None if data is None else _cache_tuple(data)


def _load_seed_cache() -> tuple[dict, dict, dict, list] | None:
    """Return the build-time seed snapshot baked into the image, or None if
    missing/corrupt/wrong-version. Last-resort fallback for a brand-new,
    never-online deployment: the DATA_DIR volume is empty (no stale cache to
    fall back to) and the network refresh just failed, so this is the only
    thing standing between "no coverage at all" and some (build-time-stale)
    coverage. See `backend/scripts/seed_ofd_cache.py`.
    """
    path = _seed_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("cache_version") != _CACHE_VERSION:
            return None
        return _cache_tuple(data)
    except Exception:
        return None


def _write_cache_file(
    gtin_index: dict, article_index: dict, variant_codes: dict, brands: list, built_at: float
) -> None:
    """Atomically write the DATA_DIR cache file (temp file + rename, so a
    reader never observes a half-written file even if the process is killed
    mid-write)."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(
            {
                "cache_version": _CACHE_VERSION,
                "built_at": built_at,
                "gtin_index": gtin_index,
                "article_index": article_index,
                "variant_codes": variant_codes,
                "brands": brands,
            }
        )
    )
    tmp_path.replace(path)


async def _download_all_json() -> dict:
    """Download and parse OFD's all.json dump, with a running size cap."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        chunks = bytearray()
        # Streamed with a running size cap rather than client.get(...).json() —
        # the latter buffers the whole response with no ceiling, so a large or
        # slow upstream response (malicious or just broken) could OOM the
        # backend on the 24h auto-refresh.
        async with client.stream("GET", OFD_ALL_URL) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > _MAX_ALL_JSON_BYTES:
                    raise ValueError(f"OFD all.json exceeded {_MAX_ALL_JSON_BYTES} byte cap - aborting download")
        return json.loads(bytes(chunks))


async def _refresh() -> tuple[dict, dict, dict, list]:
    """Download all.json; build the indexes + brand-name list; cache all of it."""
    all_json = await _download_all_json()
    gtin_index, article_index, variant_codes = _build_index(all_json)
    brands = sorted({b["name"] for b in all_json.get("brands", []) if b.get("name")})
    if not gtin_index and not article_index:
        # A 200 that parses to zero entries (e.g. upstream's dump shape
        # changes) must not overwrite a good cache with an empty one and
        # silently return "no match" for everyone for a full TTL. _ensure_loaded's
        # except-path already logs + serves the stale cache on any refresh
        # failure, so raising here reuses that fallback instead of caching this.
        raise RuntimeError("OFD refresh parsed zero entries - keeping previous cache")
    try:
        _write_cache_file(gtin_index, article_index, variant_codes, brands, time.time())
    except Exception:
        logger.warning("Failed to write OFD cache file", exc_info=True)
    return gtin_index, article_index, variant_codes, brands


async def _ensure_loaded(force: bool = False) -> None:
    global _gtin_index, _article_index, _variant_codes, _brands, _index_loaded_at
    if _gtin_index is not None and not force and (time.time() - _index_loaded_at) < OFD_TTL_SECONDS:
        return
    async with _refresh_lock:
        # Re-check after acquiring the lock — another request may have
        # already refreshed while we were waiting.
        if _gtin_index is not None and not force and (time.time() - _index_loaded_at) < OFD_TTL_SECONDS:
            return
        loaded = None if force else _load_cached()
        if loaded is None:
            try:
                loaded = await _refresh()
            except Exception:
                # Offline/upstream-down: a stale-but-working index beats no
                # index at all. Only re-raise if there's truly nothing on
                # disk to fall back to (e.g. first-ever startup with no
                # network) — that's the one case where the caller must know
                # the lookup couldn't be attempted at all.
                loaded = _load_stale_cached()
                if loaded is not None:
                    logger.warning("OFD refresh failed; serving stale disk cache instead", exc_info=True)
                else:
                    # Nothing in DATA_DIR either - a brand-new, never-online
                    # deployment. Fall back to the build-time seed snapshot
                    # (if the image was built with one) rather than leaving
                    # every lookup with zero coverage forever.
                    loaded = _load_seed_cache()
                    if loaded is None:
                        raise
                    logger.warning("OFD offline with no disk cache; serving build-time seed instead")
                    # Persist it into DATA_DIR so future boots read it as a
                    # normal stale cache instead of re-reading the seed file
                    # every time, and so the next successful online refresh
                    # naturally replaces it via the usual TTL path.
                    try:
                        _write_cache_file(*loaded, built_at=time.time())
                    except Exception:
                        logger.warning("Failed to persist seed cache into DATA_DIR", exc_info=True)
        _gtin_index, _article_index, _variant_codes, _brands = loaded
        _index_loaded_at = time.time()


async def get_gtin_index() -> dict[str, dict]:
    """Return the canonical-GTIN -> {fields, variant_id} index (memory -> disk cache -> download)."""
    await _ensure_loaded()
    return _gtin_index or {}


async def get_article_index() -> dict[str, dict]:
    """Return the normalized-article-number -> {fields, variant_id} index."""
    await _ensure_loaded()
    return _article_index or {}


async def get_brands() -> list[str]:
    """Return the OFD brand-name list (for data-driven brand detection in OCR parsing)."""
    try:
        await _ensure_loaded()
    except Exception:
        logger.warning("OFD brand list unavailable", exc_info=True)
        return []
    return _brands or []


def _codes_for_variant(variant_id: str) -> list[dict]:
    return list(_variant_codes.get(variant_id, [])) if _variant_codes else []


async def lookup(barcode: str) -> tuple[dict, list[dict]] | None:
    """Resolve a GTIN barcode: (fields, all_codes) for its colour, or None if not found.

    ``all_codes`` includes every GTIN/SKU sibling (other package sizes, the
    refill code, the manufacturer article number) sharing the same colour.
    """
    idx = await get_gtin_index()
    entry = idx.get(canon(barcode))
    if not entry:
        return None
    return entry["fields"], _codes_for_variant(entry["variant_id"])


async def lookup_article(code: str) -> tuple[dict, list[dict]] | None:
    """Resolve a manufacturer SKU/article number the same way `lookup` resolves a GTIN."""
    idx = await get_article_index()
    entry = idx.get((code or "").strip().upper())
    if not entry:
        return None
    return entry["fields"], _codes_for_variant(entry["variant_id"])


async def refresh_database() -> int:
    """Force a re-download of the OFD dump regardless of TTL. Returns the combined entry count."""
    await _ensure_loaded(force=True)
    gtin_idx = await get_gtin_index()
    article_idx = await get_article_index()
    return len(gtin_idx) + len(article_idx)
