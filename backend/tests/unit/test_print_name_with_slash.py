"""A print whose name holds a ``/`` is found under the name the printer gave it.

The printer cannot store a ``/`` in a file name and writes it as ``2f``: a Bambu
Cloud print named ``CLICKER VERSION OREO / LOVE`` landed on an H2C (firmware
01.02.00.00) as ``/cache/CLICKER VERSION OREO 2f LOVE.gcode.3mf``. Bambuddy built
its candidate paths from the raw name, so the cover, the print-start archive and
the skip-objects list all looked inside a directory called
``CLICKER VERSION OREO `` and never found the file.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import backend.app.api.routes.printers as printers_mod
from backend.app.api.routes.printers import _produce_cover_image
from backend.app.services.print_storage import printer_file_name

SUBTASK = "CLICKER VERSION OREO / LOVE"
ON_PRINTER = "CLICKER VERSION OREO 2f LOVE.gcode.3mf"
COVER_BYTES = b"\x89PNG\r\n\x1a\nplate-1-thumbnail"


class TestPrinterFileName:
    def test_a_slash_becomes_2f(self):
        assert printer_file_name(SUBTASK) == "CLICKER VERSION OREO 2f LOVE"

    def test_what_the_printer_keeps_is_left_alone(self):
        # Seen as-is on the same printer, in /cache/.
        assert printer_file_name("0.2mm layer, 2 walls, 15% infill") == "0.2mm layer, 2 walls, 15% infill"


@pytest.fixture(autouse=True)
def _clear_cover_state():
    printers_mod._cover_cache.clear()
    printers_mod._cover_404_cache.clear()
    printers_mod._cover_inflight.clear()
    yield
    printers_mod._cover_cache.clear()
    printers_mod._cover_404_cache.clear()
    printers_mod._cover_inflight.clear()


@pytest.mark.asyncio
async def test_the_cover_is_found_under_the_printers_name(tmp_path):
    asked: list[str] = []

    async def download(ip_address, access_code, remote_paths, local_path, **kwargs):
        asked.extend(remote_paths)
        wanted = f"/cache/{ON_PRINTER}"
        if wanted not in remote_paths:
            return None
        # The temp file sits directly in archive/temp, not in a directory the
        # raw name's slash would have made up.
        assert local_path.parent == tmp_path / "archive" / "temp"
        with zipfile.ZipFile(local_path, "w") as zf:
            zf.writestr("Metadata/plate_1.png", COVER_BYTES)
        return wanted

    async def no_recovery(printer_id, name, path):
        return False

    printer = SimpleNamespace(id=1, ip_address="192.168.3.244", access_code="x", model="H2C", name="H2C")
    with (
        patch.object(printers_mod.settings, "archive_dir", tmp_path / "archive"),
        patch.object(printers_mod.printer_manager, "get_status", MagicMock(return_value=SimpleNamespace())),
        patch.object(
            printers_mod,
            "print_file_reachable_over_ftp",
            MagicMock(return_value=SimpleNamespace(reachable=True, probe_filename=None, reason=None)),
        ),
        patch.object(printers_mod, "get_cached_3mf", lambda *_: None),
        patch.object(printers_mod, "cache_3mf_download", lambda *_: None),
        patch.object(printers_mod, "ftps_handshake_blocked", lambda *_: False),
        patch.object(printers_mod, "download_file_try_paths_async", download),
        patch("backend.app.main.try_recover_fallback_archive", no_recovery),
    ):
        image = await _produce_cover_image(printer, 1, SUBTASK, None, "default", None, (SUBTASK, "default"))

    assert image == COVER_BYTES
    assert not any(" / " in path for path in asked), "a path was built from the raw name"


@pytest.mark.asyncio
async def test_the_downloaded_file_reaches_the_prints_fallback_archive(tmp_path):
    """The cover endpoint offers its download to the running print's empty
    archive, which is registered under the raw name, slash and all."""
    import backend.app.main as main_mod

    archive = SimpleNamespace(id=77, file_path=None, deleted_at=None)

    class _Result:
        def scalar_one_or_none(self):
            return archive

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *_):
            return _Result()

    recovered: list[int] = []

    async def recover(archive_id, path, printer_id, *, price=True):
        recovered.append(archive_id)
        return True

    with (
        patch.dict(main_mod._active_prints, {(1, SUBTASK): 77}, clear=True),
        patch.object(main_mod, "async_session", lambda: _Session()),
        patch.object(main_mod, "_recover_fallback_archive", recover),
    ):
        assert await main_mod.try_recover_fallback_archive(1, ON_PRINTER, tmp_path / ON_PRINTER)

    assert recovered == [77]
