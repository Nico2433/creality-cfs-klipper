# SPDX-License-Identifier: GPL-3.0-or-later
"""Spool insert followed from the 0x30 heartbeat pushes, and CFS_TRY."""

import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import INSERT_QUIET_S, build_message, CMD_GET_BOX_STATE
from tests.conftest import make_wired_controller
from tests.mock_cfs import MockCFSHardware


def _wired():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs._run_auto_addressing()
    return hw, cfs


def _push(phases):
    return {"event": 0x30, "raw": bytes(phases)}


IDLE = {"event": 0x00, "raw": bytes([0x12, 0x42, 0x00, 0x00])}


def test_insert_in_progress_holds_the_presence_reads():
    _, cfs = _wired()
    cfs.read_material = mock.MagicMock()
    assert cfs._track_insert(0x01, _push([0, 0, 2, 0]), 10.0) is True
    assert cfs._track_insert(0x01, IDLE, 14.0) is True        # between pushes
    assert cfs._track_insert(0x01, _push([0, 0, 1, 0]), 18.0) is True
    cfs.read_material.assert_not_called()
    msgs = [c.args[0] for c in cfs.gcode.respond_info.call_args_list]
    assert msgs == ["CFS: slot3 filament detected, the box is pulling it in"]


def test_insert_complete_reads_that_slot_like_stock():
    _, cfs = _wired()
    cfs.read_material = mock.MagicMock(return_value="C:unknown;")
    cfs.read_remain = mock.MagicMock(return_value=[0xFF, 0xFF, 0x64, 0xFF])
    cfs._track_insert(0x01, _push([0, 0, 2, 0]), 10.0)
    assert cfs._track_insert(0x01, _push([0, 0, 3, 0]), 40.0) is False
    cfs.read_material.assert_called_once_with(0x01, 0x04, timeout=5.0)
    cfs.read_remain.assert_called_once_with(0x01, 0x04, timeout=5.0)
    assert cfs._slots[2]["present"] is True
    assert cfs._slots[2]["remain"] == 100
    assert cfs._track_insert(0x01, IDLE, 44.0) is False


def test_insert_without_completion_times_out():
    _, cfs = _wired()
    cfs._track_insert(0x01, _push([2, 0, 0, 0]), 10.0)
    assert cfs._track_insert(0x01, IDLE, 10.0 + INSERT_QUIET_S - 1) is True
    assert cfs._track_insert(0x01, IDLE, 10.0 + INSERT_QUIET_S + 1) is False


def test_heartbeat_skips_the_presence_read_during_an_insert():
    hw, cfs = _wired()
    hw._resp_get_box_state = lambda addr, data: build_message(
        addr, 0x30, CMD_GET_BOX_STATE, bytes([0x00, 0x02, 0x00, 0x00]))
    cfs.refresh_slot_presence = mock.MagicMock()
    cfs.heartbeat_interval = 4.
    cfs._bus_lock.test.return_value = False
    cfs._heartbeat(100.0)
    cfs.refresh_slot_presence.assert_not_called()
    assert 0x01 in cfs._inserting


class _Err(Exception):
    pass


def _gcmd(cmd):
    gcmd = mock.MagicMock()
    gcmd.get.return_value = cmd
    gcmd.error = _Err
    return gcmd


def test_cfs_try_reports_the_error_and_continues():
    _, cfs = _wired()
    cfs.gcode.run_script_from_command.side_effect = _Err("CFS_RETRUDE: jammed")
    gcmd = _gcmd("CFS_UNLOAD")
    cfs.cmd_CFS_TRY(gcmd)               # does not raise
    cfs.gcode.run_script_from_command.assert_called_once_with("CFS_UNLOAD")
    assert "CFS_UNLOAD failed (CFS_RETRUDE: jammed)" in gcmd.respond_raw.call_args.args[0]


def test_cfs_try_runs_the_command():
    _, cfs = _wired()
    gcmd = _gcmd("CFS_UNLOAD")
    cfs.cmd_CFS_TRY(gcmd)
    cfs.gcode.run_script_from_command.assert_called_once_with("CFS_UNLOAD")
    gcmd.respond_raw.assert_not_called()


def test_cfs_try_refuses_arguments():
    _, cfs = _wired()
    with pytest.raises(_Err):
        cfs.cmd_CFS_TRY(_gcmd("CFS_UNLOAD TOOL=1"))


def test_heartbeat_without_idle_presence_poll_only_polls_box_state():
    hw, cfs = _wired()
    cfs.idle_presence_poll = False
    cfs.refresh_slot_presence = mock.MagicMock()
    cfs.heartbeat_interval = 4.
    cfs._bus_lock.test.return_value = False
    cfs._heartbeat(100.0)
    cfs.refresh_slot_presence.assert_not_called()
