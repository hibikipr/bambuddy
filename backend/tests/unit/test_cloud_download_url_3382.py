"""A cloud print's download link is not a storage verdict (#3382).

Since #1820 the printer's own project_file echo on the report topic sets the
print's destination URL. For a Bambu Cloud job that URL is where the printer
downloads the file from -- measured on an H2C (firmware 01.02.00.00):
``https://or-cloud-upload-prod.s3.us-west-2.amazonaws.com/users/.../<id>_3.3mf?X-Amz-...``.
Read as "not ftp, so not external storage", it made every cloud print skip the
3MF lookup and the cover as ``internal_storage``, with nothing to probe for
either, since the name ends in a query string. The file was at
``/cache/<subtask>.gcode.3mf`` all along.

Also here: a fallback archive kept only what followed the last ``/`` of the
print's name, titling "CLICKER VERSION OREO / LOVE" as " LOVE".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app import main as main_module
from backend.app.services.print_storage import print_file_reachable_over_ftp, url_is_external_storage
from backend.tests.unit.test_print_start_skips_unreachable_storage_2780 import _fallback, _run_print_start

S3_URL = (
    "https://or-cloud-upload-prod.s3.us-west-2.amazonaws.com/users/3230087186/models/"
    "20261026060704.585/USd8c6c53ad6b49b_1094707475_3.3mf?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=0"
)


def _state(url, **extra):
    return SimpleNamespace(current_project_url=url, sdcard=True, sdcard_reported=True, **extra)


class TestTheDownloadLinkSaysNothing:
    @pytest.mark.parametrize("url", [S3_URL, S3_URL.replace("https://", "http://"), "HTTPS://example.com/x.3mf"])
    def test_it_is_no_evidence(self, url):
        assert url_is_external_storage(url) is None

    def test_other_unknown_schemes_still_are(self):
        # Unchanged: a scheme nobody has seen must not read as fine.
        assert url_is_external_storage("sftp://Benchy.3mf") is False

    def test_the_print_counts_as_reachable(self):
        assert print_file_reachable_over_ftp(_state(S3_URL)).reachable

    def test_an_empty_slot_still_answers(self):
        verdict = print_file_reachable_over_ftp(
            SimpleNamespace(current_project_url=S3_URL, sdcard=False, sdcard_reported=True)
        )
        assert not verdict.reachable


@pytest.mark.asyncio
class TestPrintStart:
    async def test_a_cloud_print_is_looked_for(self):
        added = []
        download, _retry, _probe, _schedule = await _run_print_start(_state(S3_URL), added)
        download.assert_called()
        assert _fallback(added).extra_data["no_3mf_reason"] is None, "not written off as internal storage"

    async def test_a_name_with_a_slash_keeps_its_title(self):
        real = main_module.on_print_start

        async def named(printer_id, data):
            return await real(printer_id, {**data, "subtask_name": "CLICKER VERSION OREO / LOVE"})

        added = []
        with patch.object(main_module, "on_print_start", named):
            await _run_print_start(_state(S3_URL), added)
        assert _fallback(added).print_name == "CLICKER VERSION OREO / LOVE"
