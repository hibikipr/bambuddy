"""A cloud print's archive gets its 3MF once the file reaches the printer (#3382).

A job sent through Bambu Cloud reaches the printer as a job, and the printer
fetches its 3MF into ``/cache`` while the print already counts as started. The
print-start sweep therefore found nothing, and since a plain 550 sweep is not
retried (#3063), the print was archived without its file unless someone opened
its cover later in the print.

The printer reports the fetch: push_status carries ``print_type`` ("cloud")
and ``gcode_file_prepare_percent`` (measured on an H2C, firmware 01.02.00.00,
reaching "100" once the file is there). A sweep that found nothing while either
says the file is still on its way now schedules a retry that waits for the
fetch to finish (99 or more: P1-series printers can stop at 99) and then looks
again. A miss with neither still schedules nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app import main as main_module
from backend.app.main import _file_still_on_its_way, _wait_for_file_on_printer
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.tests.unit.test_print_start_skips_unreachable_storage_2780 import _fallback, _run_print_start


def _client() -> BambuMQTTClient:
    return BambuMQTTClient(ip_address="192.168.3.244", serial_number="TEST3382", access_code="12345678")


class TestThePrinterReportsTheFetch:
    def test_print_type_and_prepare_percent_are_kept(self):
        client = _client()
        client._update_state({"print_type": "cloud", "gcode_file_prepare_percent": "37"})
        assert client.state.print_type == "cloud"
        assert client.state.file_prepare_percent == 37

        client._update_state({"gcode_file_prepare_percent": "100"})
        assert client.state.file_prepare_percent == 100
        assert client.state.print_type == "cloud", "a frame without print_type keeps the last one"

    def test_an_unreadable_percent_keeps_the_last_good_one(self):
        client = _client()
        client._update_state({"gcode_file_prepare_percent": "55"})
        client._update_state({"gcode_file_prepare_percent": "n/a"})
        assert client.state.file_prepare_percent == 55

    def test_nothing_is_assumed_before_a_report(self):
        state = _client().state
        assert state.print_type is None
        assert state.file_prepare_percent is None


class TestWhatCountsAsOnItsWay:
    @pytest.mark.parametrize(
        ("print_type", "percent", "expected"),
        [
            ("cloud", 100, True),  # cloud: the file follows the print's start
            ("local", 40, True),  # still fetching, whatever the source
            ("local", 100, False),  # fetched; a 550 now means it is not there
            ("local", 99, False),  # a P1 can stop at 99 and never report 100
            (None, None, False),  # no evidence either way (#3063 stands)
        ],
    )
    def test_cases(self, print_type, percent, expected):
        state = SimpleNamespace(print_type=print_type, file_prepare_percent=percent)
        assert _file_still_on_its_way(state) is expected

    def test_no_state(self):
        assert _file_still_on_its_way(None) is False


@pytest.mark.asyncio
class TestPrintStart:
    async def _start(self, **state_fields):
        state = SimpleNamespace(
            current_project_url=None, sdcard=True, sdcard_reported=True, print_type=None, file_prepare_percent=None
        )
        for key, value in state_fields.items():
            setattr(state, key, value)
        added = []
        _download, _retry, _probe, schedule = await _run_print_start(state, added)
        return added, schedule

    async def test_a_cloud_print_that_missed_schedules_a_retry(self):
        added, schedule = await self._start(print_type="cloud", file_prepare_percent=12)

        assert _fallback(added).extra_data["no_3mf_reason"] is None, "the archive card's reason is unchanged"
        schedule.assert_called_once()
        assert schedule.call_args.kwargs["reason"] == main_module._RETRY_FILE_NOT_YET_ON_PRINTER
        assert schedule.call_args.kwargs["filenames"], "it retries the names the sweep tried"

    async def test_a_lan_print_that_missed_still_schedules_nothing(self):
        _added, schedule = await self._start(print_type="local", file_prepare_percent=100)
        schedule.assert_not_called()


@pytest.mark.asyncio
class TestTheWait:
    async def test_it_returns_once_the_file_is_fetched(self, monkeypatch):
        reports = iter([20, 60, 100])
        state = SimpleNamespace(file_prepare_percent=0)

        def get_status(_printer_id):
            state.file_prepare_percent = next(reports)
            return state

        polls = []

        async def sleep(seconds):
            polls.append(seconds)

        monkeypatch.setattr(main_module.printer_manager, "get_status", get_status)
        monkeypatch.setattr(main_module.asyncio, "sleep", sleep)

        await _wait_for_file_on_printer(1)
        assert len(polls) == 2, "it polled until the printer said 100"

    async def test_99_counts_as_fetched(self, monkeypatch):
        """P1-series printers can stop at 99; waiting for 100 would sit out the
        whole cap before looking."""

        async def sleep(_seconds):
            raise AssertionError("kept waiting at 99")

        monkeypatch.setattr(
            main_module.printer_manager, "get_status", lambda _pid: SimpleNamespace(file_prepare_percent=99)
        )
        monkeypatch.setattr(main_module.asyncio, "sleep", sleep)
        await _wait_for_file_on_printer(1)

    async def test_it_gives_up_waiting_and_looks_anyway(self, monkeypatch):
        clock = [0.0]
        state = SimpleNamespace(file_prepare_percent=10)

        async def sleep(seconds):
            clock[0] += seconds

        monkeypatch.setattr(main_module.printer_manager, "get_status", lambda _pid: state)
        monkeypatch.setattr(main_module.asyncio, "sleep", sleep)
        monkeypatch.setattr(main_module.time, "monotonic", lambda: clock[0])

        await asyncio.wait_for(_wait_for_file_on_printer(1), timeout=5)
        assert clock[0] >= main_module._FILE_PREPARE_WAIT_MAX_SECONDS

    async def test_a_printer_that_does_not_report_it_is_not_waited_on(self, monkeypatch):
        async def sleep(_seconds):
            raise AssertionError("waited for a report that will never come")

        monkeypatch.setattr(
            main_module.printer_manager, "get_status", lambda _pid: SimpleNamespace(file_prepare_percent=None)
        )
        monkeypatch.setattr(main_module.asyncio, "sleep", sleep)
        await _wait_for_file_on_printer(1)

    async def test_the_retry_waits_before_its_first_look(self, monkeypatch):
        order = []

        async def wait(printer_id):
            order.append("wait")

        async def sleep(_seconds):
            order.append("look")
            raise asyncio.CancelledError  # stop after the first rung

        monkeypatch.setattr(main_module, "_wait_for_file_on_printer", wait)
        monkeypatch.setattr(main_module.asyncio, "sleep", sleep)

        main_module._schedule_fallback_3mf_retry(
            printer_id=1, archive_id=1, filenames=["job.gcode.3mf"], reason=main_module._RETRY_FILE_NOT_YET_ON_PRINTER
        )
        task = main_module._fallback_3mf_retry_tasks[1]
        with pytest.raises(asyncio.CancelledError):
            await task
        assert order == ["wait", "look"]
