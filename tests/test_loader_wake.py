# SPDX-License-Identifier: GPL-3.0-or-later
"""Cold boot: the box comes up in its loader and must be woken (stock loader_check), and a
box that restarts while Klipper runs must be re-addressed."""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import (
    BoxAddressEntry,
    CMD_LOADER_TO_APP,
    CMD_SET_SLAVE_ADDR,
    HEARTBEAT_LOST_REINIT,
    LOADER_WAKE_PASSES,
)
from tests.conftest import make_wired_controller
from tests.mock_cfs import MockCFSHardware


def _wake_frames(hw):
    return [p for f, p in hw._received if f == CMD_LOADER_TO_APP]


def test_cold_boot_loader_box_is_woken_and_readdressed():
    hw = MockCFSHardware(box_count=1)
    hw.set_loader(0)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs.loader_wake = True

    assert cfs._run_auto_addressing() == 1

    entry = cfs._box_table[0]
    assert entry.online == BoxAddressEntry.ONLINE_ONLINE
    assert entry.mode == BoxAddressEntry.MODE_APP
    assert hw.is_box_addressed(0)
    # step-1 wake + one wake after the loader box was addressed, to 0xFF with [01]
    wakes = _wake_frames(hw)
    assert len(wakes) == 2
    assert wakes[1]["addr"] == 0xFF and wakes[1]["data"] == b"\x01"
    # addressed twice: once in the loader, once after the app started
    assert hw.get_received_funcs().count(CMD_SET_SLAVE_ADDR) >= 2
    # the app answers operational commands now
    assert cfs.get_box_state(0x01, retries=1) is not None


def test_app_box_needs_no_extra_wake():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs.loader_wake = True

    cfs._run_auto_addressing()

    assert len(_wake_frames(hw)) == 1


def test_stuck_loader_gives_up_after_bounded_passes():
    hw = MockCFSHardware(box_count=1)
    hw.set_loader(0)
    hw._resp_loader_to_app = lambda addr, data: None   # box never leaves its loader
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs.loader_wake = True

    cfs._run_auto_addressing()

    assert len(_wake_frames(hw)) == LOADER_WAKE_PASSES      # step 1 + passes - 1
    assert cfs._box_table[0].mode == BoxAddressEntry.MODE_LOADER


def test_online_box_answering_discovery_is_readdressed():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs.loader_wake = True
    cfs._run_auto_addressing()
    hw.restart_box(0)

    assert cfs._run_auto_addressing() == 1
    assert hw.is_box_addressed(0)
    assert cfs._box_table[0].online == BoxAddressEntry.ONLINE_ONLINE


def test_heartbeat_reconnects_after_silent_ticks():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs.loader_wake = True
    cfs._run_auto_addressing()
    cfs._connected.add(0x01)
    cfs.heartbeat_interval = 4.
    cfs._bus_lock = mock.MagicMock()
    cfs._bus_lock.test.return_value = False
    cfs.get_box_state = mock.MagicMock(return_value=None)
    cfs._run_auto_addressing = mock.MagicMock(return_value=1)
    cfs.reactor.register_callback.reset_mock()

    for _ in range(HEARTBEAT_LOST_REINIT - 1):
        cfs._heartbeat(0.)
    cfs._run_auto_addressing.assert_not_called()

    cfs._heartbeat(0.)
    cfs._run_auto_addressing.assert_called_once()
    assert 0x01 not in cfs._connected
    cfs.reactor.register_callback.assert_called_with(cfs._connect_probe)


def test_heartbeat_answer_resets_miss_count():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs.loader_wake = True
    cfs._run_auto_addressing()
    cfs._bus_lock = mock.MagicMock()
    cfs._bus_lock.test.return_value = False
    cfs._run_auto_addressing = mock.MagicMock(return_value=1)
    answers = iter([None, None, {"ok": 1}] + [None] * (HEARTBEAT_LOST_REINIT - 1))
    cfs.get_box_state = mock.MagicMock(side_effect=lambda *a, **k: next(answers))

    for _ in range(2 + HEARTBEAT_LOST_REINIT):
        cfs._heartbeat(0.)
    cfs._run_auto_addressing.assert_not_called()
