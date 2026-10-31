# SPDX-License-Identifier: GPL-3.0-or-later
"""K1 guards: defaults keep upstream addressing, the heartbeat stays out of a running
choreography, no re-addressing mid-print, CFS_DETECT keeps the tool on no answer."""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import CMD_LOADER_TO_APP, HEARTBEAT_LOST_REINIT
from tests.conftest import make_wired_controller
from tests.mock_cfs import MockCFSHardware


def _wired():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs._run_auto_addressing()
    cfs.heartbeat_interval = 4.
    cfs._bus_lock.test.return_value = False
    return hw, cfs


def test_loader_wake_off_keeps_upstream_addressing():
    hw = MockCFSHardware(box_count=1)
    hw.set_loader(0)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs._run_auto_addressing()
    # only upstream's step-1 wake: no wake/re-address passes for a loader-mode reply
    assert len([p for f, p in hw._received if f == CMD_LOADER_TO_APP]) == 1


def test_heartbeat_waits_for_a_running_choreography():
    hw, cfs = _wired()
    seen = []

    def body(gcmd):
        before = hw.get_received_funcs().count(0x0A)
        cfs._heartbeat(100.0)
        seen.append(hw.get_received_funcs().count(0x0A) - before)
    cfs._exclusive(body)(mock.MagicMock())
    assert seen == [0]
    assert cfs._op_depth == 0
    before = hw.get_received_funcs().count(0x0A)
    cfs._heartbeat(104.0)
    assert hw.get_received_funcs().count(0x0A) == before + 1


def test_exclusive_releases_on_error():
    _, cfs = _wired()

    def boom(gcmd):
        raise RuntimeError("jam")
    try:
        cfs._exclusive(boom)(mock.MagicMock())
    except RuntimeError:
        pass
    assert cfs._op_depth == 0


def test_silent_box_is_not_readdressed_mid_print():
    _, cfs = _wired()
    cfs.get_box_state = mock.MagicMock(return_value=None)
    cfs._reconnect_boxes = mock.MagicMock()
    stats = mock.MagicMock()
    stats.get_status.return_value = {"state": "printing"}
    cfs.printer.lookup_object.side_effect = (
        lambda name, default=None: stats if name == "print_stats" else default)
    for i in range(HEARTBEAT_LOST_REINIT):
        cfs._heartbeat(100.0 + 4 * i)
    cfs._reconnect_boxes.assert_not_called()

    stats.get_status.return_value = {"state": "standby"}
    for i in range(HEARTBEAT_LOST_REINIT):
        cfs._heartbeat(200.0 + 4 * i)
    cfs._reconnect_boxes.assert_called_once()


def test_cfs_detect_keeps_the_tool_when_the_box_does_not_answer():
    _, cfs = _wired()
    cfs._active_tool = 2
    cfs.get_hardware_status = mock.MagicMock(return_value=-1)
    cfs._toolhead_filament_detected = mock.MagicMock(return_value=False)
    gcmd = mock.MagicMock()
    gcmd.get_int.return_value = 1
    cfs.cmd_CFS_DETECT(gcmd)
    assert cfs._active_tool == 2
    assert "did not answer" in gcmd.respond_info.call_args.args[0]
