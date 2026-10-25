"""A cover that was not on the printer yet is looked for again.

A print started from Bambu Cloud reaches the printer as a job, and the printer
downloads its 3MF into ``/cache`` while the print already reports as started.
The printer card asks for the cover as soon as the print shows up, so
the first sweep can find nothing -- and #1420's negative cache then replayed
that miss until the print ended, though the file sat at
``/cache/<subtask>.gcode.3mf`` the whole time (H2C, firmware 01.02.00.00, a
2.2 MB file Bambuddy's own FTP client downloads at once when asked again).

A sweep that found nothing now holds for ``_COVER_MISS_RETRY_SECONDS``. The
misses that cannot change during a print -- storage the printer says FTPS
cannot read, a 3MF with no thumbnail -- still hold until the next print starts.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

import backend.app.api.routes.printers as printers_mod
from backend.app.api.routes.printers import _cover_miss_cached, _produce_cover_image

pytestmark = pytest.mark.asyncio

SUBTASK = "0.2mm layer, 2 walls, 15% infill"
KEY = (SUBTASK, "default")
COVER_BYTES = b"\x89PNG\r\n\x1a\nplate-1-thumbnail"


@pytest.fixture(autouse=True)
def _clear_cover_state():
    printers_mod._cover_cache.clear()
    printers_mod._cover_404_cache.clear()
    printers_mod._cover_inflight.clear()
    yield
    printers_mod._cover_cache.clear()
    printers_mod._cover_404_cache.clear()
    printers_mod._cover_inflight.clear()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def _produce(tmp_path: Path, *, file_on_printer: bool, verdict=None, clock: _Clock):
    """Run the cover lookup against a printer that does or does not have the file."""

    async def download(ip_address, access_code, remote_paths, local_path, **kwargs):
        if not file_on_printer:
            return None
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(local_path, "w") as zf:
            zf.writestr("Metadata/plate_1.png", COVER_BYTES)
        return remote_paths[1]

    async def no_recovery(printer_id, name, path):
        return False

    async def noop(*_):
        return None

    printer = SimpleNamespace(id=1, ip_address="192.168.3.244", access_code="x", model="H2C", name="H2C")
    verdict = verdict or SimpleNamespace(reachable=True, probe_filename=None, reason=None)
    with (
        patch.object(printers_mod.settings, "archive_dir", tmp_path / "archive"),
        patch.object(printers_mod.printer_manager, "get_status", MagicMock(return_value=SimpleNamespace())),
        patch.object(printers_mod, "print_file_reachable_over_ftp", MagicMock(return_value=verdict)),
        patch.object(printers_mod, "get_cached_3mf", lambda *_: None),
        patch.object(printers_mod, "cache_3mf_download", lambda *_: None),
        patch.object(printers_mod, "ftps_handshake_blocked", lambda *_: False),
        patch.object(printers_mod, "download_file_try_paths_async", download),
        patch("backend.app.main.try_recover_fallback_archive", no_recovery),
        patch.object(printers_mod.asyncio, "sleep", noop),
        patch.object(printers_mod.time, "monotonic", clock),
    ):
        return await _produce_cover_image(printer, 1, SUBTASK, None, "default", None, KEY)


class TestAFileStillOnItsWay:
    async def test_the_miss_runs_out_and_the_cover_is_found(self, tmp_path):
        clock = _Clock()
        with pytest.raises(HTTPException) as missed:
            await _produce(tmp_path, file_on_printer=False, clock=clock)
        assert missed.value.status_code == 404

        with patch.object(printers_mod.time, "monotonic", clock):
            assert _cover_miss_cached(1, KEY), "a refresh straight after must not sweep again (#1420)"
            clock.now += printers_mod._COVER_MISS_RETRY_SECONDS
            assert not _cover_miss_cached(1, KEY), "the miss outlived its window"
            assert KEY not in printers_mod._cover_404_cache[1], "an expired miss is dropped"

        # The printer has finished fetching the file from the cloud.
        assert await _produce(tmp_path, file_on_printer=True, clock=clock) == COVER_BYTES


class TestMissesThatCannotChangeStillHold:
    async def test_storage_ftps_cannot_read(self, tmp_path):
        clock = _Clock()
        verdict = SimpleNamespace(reachable=False, probe_filename=None, reason="internal_storage")
        with pytest.raises(HTTPException):
            await _produce(tmp_path, file_on_printer=False, verdict=verdict, clock=clock)

        clock.now += 24 * 3600
        with patch.object(printers_mod.time, "monotonic", clock):
            assert _cover_miss_cached(1, KEY)

    async def test_a_probe_the_printer_predicted_would_miss(self, tmp_path):
        clock = _Clock()
        verdict = SimpleNamespace(reachable=False, probe_filename=f"{SUBTASK}.gcode.3mf", reason="internal_storage")
        with pytest.raises(HTTPException):
            await _produce(tmp_path, file_on_printer=False, verdict=verdict, clock=clock)

        clock.now += 24 * 3600
        with patch.object(printers_mod.time, "monotonic", clock):
            assert _cover_miss_cached(1, KEY)

    async def test_print_start_still_clears_everything(self, tmp_path):
        printers_mod._remember_cover_miss(1, KEY)
        printers_mod._remember_cover_miss(1, (SUBTASK, "top"), retry_after=60)
        printers_mod.clear_cover_cache(1)
        assert not _cover_miss_cached(1, KEY)
        assert not _cover_miss_cached(1, (SUBTASK, "top"))
