# SPDX-License-Identifier: GPL-3.0-or-later
"""
test_k1_parity.py: the K1-family stock-parity options and the CFS_FEEDER / CFS_BUFFER
commands.

  - load_finalize=False            -> no 0x10 [slot][07][03] after the settle stage
  - release_feeder_after_load=False -> no 0x0F 00 at the end of a load
  - retrude_pull_len / _velocity   -> the unload's single toolhead pull
  - cut_switch_button              -> CFS_CUT confirms the cutter switch, backs off first
  - CFS_FEEDER / CFS_BUFFER        -> 0x0F engage/release and one 0x05 read

Driven through the wired MockCFSHardware transport like the other choreography tests.
"""

import sys
import os
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import (
    CMD_CTRL_CONNECTION_MOTOR_ACTION,
    CMD_EXTRUDE_PROCESS,
    CMD_GET_BUFFER_STATE,
    EXTRUDE_SUB_FINALIZE,
    SLOT_T0,
)

from tests.mock_cfs import MockCFSHardware
from tests.conftest import make_wired_controller


def _wired():
    hw = MockCFSHardware(box_count=1)
    cfs, ser = make_wired_controller(hw, box_count=1, retry_count=1)
    cfs._run_auto_addressing()
    return cfs, hw


def _gcmd(ints=None, floats=None):
    ints = ints or {}
    floats = floats or {}
    gcmd = mock.MagicMock()
    gcmd.get_int.side_effect = lambda k, d=None, **kw: ints.get(k, d)
    gcmd.get_float.side_effect = lambda k, d=None, **kw: floats.get(k, d)
    gcmd.error.side_effect = lambda m: Exception(m)
    return gcmd


def _scripts(cfs):
    return [c.args[0] for c in cfs.gcode.run_script_from_command.call_args_list]


def _infos(gcmd):
    return [c.args[0] for c in gcmd.respond_info.call_args_list]


def _frames(hw, func):
    """Data payloads of every received frame with this function code."""
    return [bytes(p.get("data", b"")) for f, p in hw.get_received_messages() if f == func]


def _latching_sensor(cfs):
    reads = iter([False, True])
    cfs._toolhead_filament_detected = lambda: next(reads, True)


def _with_button(cfs, state):
    """Make printer.lookup_object() resolve [gcode_button cfs_cut_switch] to a stub."""
    button = mock.MagicMock()
    button.get_status.return_value = {"state": state}
    original = cfs.printer.lookup_object.side_effect

    def _lookup(name, default=mock.sentinel.no_default):
        if name == "gcode_button cfs_cut_switch":
            return button
        return original(name, default)

    cfs.printer.lookup_object.side_effect = _lookup
    cfs.cut_switch_button = "cfs_cut_switch"


def _k1_cut_geometry(cfs):
    cfs.cut_switch_pin = "nozzle_mcu:PA8"
    cfs.pre_cut_pos_x = 38.0
    cfs.pre_cut_pos_y = 230.0
    cfs.cut_pos_x = None
    cfs.cut_pos_y = 306.0
    cfs.cut_pos_x_max = None
    cfs.cut_velocity = 7000.0


class TestDefaultsKeepHiBehaviour:
    def test_defaults(self):
        cfs, hw = _wired()
        assert cfs.load_finalize is True
        assert cfs.release_feeder_after_load is True
        assert cfs.retrude_pull_len == 15.0
        assert cfs.retrude_pull_velocity == 360.0
        assert cfs.cut_switch_button is None

    def test_load_sends_finalize_and_release_by_default(self):
        cfs, hw = _wired()
        _latching_sensor(cfs)
        cfs.load_process(_gcmd(floats={"TEMP": 220.0}), 0x01, SLOT_T0)
        stages = [d[1] for d in _frames(hw, CMD_EXTRUDE_PROCESS) if len(d) >= 2]
        assert EXTRUDE_SUB_FINALIZE in stages
        assert _frames(hw, CMD_CTRL_CONNECTION_MOTOR_ACTION)[-1] == b"\x00"


class TestK1Load:
    def test_no_finalize_when_disabled(self):
        cfs, hw = _wired()
        cfs.load_finalize = False
        _latching_sensor(cfs)
        cfs.load_process(_gcmd(floats={"TEMP": 220.0}), 0x01, SLOT_T0)
        stages = [d[1] for d in _frames(hw, CMD_EXTRUDE_PROCESS) if len(d) >= 2]
        assert 0x06 in stages                      # settle still sent
        assert EXTRUDE_SUB_FINALIZE not in stages  # K1 stock never sends 07

    def test_feeder_stays_engaged_when_release_disabled(self):
        cfs, hw = _wired()
        cfs.release_feeder_after_load = False
        _latching_sensor(cfs)
        cfs.load_process(_gcmd(floats={"TEMP": 220.0}), 0x01, SLOT_T0)
        assert _frames(hw, CMD_CTRL_CONNECTION_MOTOR_ACTION) == [b"\x01"]


class TestRetrudePull:
    def test_configured_pull(self):
        cfs, hw = _wired()
        cfs.retrude_pull_len = 20.0
        cfs.retrude_pull_velocity = 300.0
        cfs._toolhead_pull()
        assert "G1 E-20.000 F300" in _scripts(cfs)


class TestCutSwitchConfirm:
    def test_pressed_confirms_and_backs_off(self):
        cfs, hw = _wired()
        _k1_cut_geometry(cfs)
        _with_button(cfs, "PRESSED")
        gcmd = _gcmd(ints={"BOX": 1}, floats={"TEMP": 220.0})
        cfs.cmd_CFS_CUT(gcmd)
        scripts = _scripts(cfs)
        i_ram = scripts.index("G0 Y306.000 F7000")
        i_back = scripts.index("G0 Y230.000 F7000")
        assert i_ram < scripts.index("M400") < i_back   # read after the ram settles
        assert any("PRESSED at the cut position" in s for s in _infos(gcmd))
        hw.assert_command_received(CMD_GET_BUFFER_STATE)

    def test_released_raises_after_backing_off(self):
        cfs, hw = _wired()
        _k1_cut_geometry(cfs)
        _with_button(cfs, "RELEASED")
        gcmd = _gcmd(ints={"BOX": 1}, floats={"TEMP": 220.0})
        with pytest.raises(Exception, match="did not read PRESSED"):
            cfs.cmd_CFS_CUT(gcmd)
        assert "G0 Y230.000 F7000" in _scripts(cfs)    # toolhead left the cutter first
        assert cfs._cut_state is False

    def test_no_button_configured_skips_check(self):
        cfs, hw = _wired()
        _k1_cut_geometry(cfs)
        gcmd = _gcmd(ints={"BOX": 1}, floats={"TEMP": 220.0})
        cfs.cmd_CFS_CUT(gcmd)
        assert not any("cutter switch" in s for s in _infos(gcmd))
        assert "G0 Y230.000 F7000" in _scripts(cfs)

    def test_missing_button_object_skips_check(self):
        cfs, hw = _wired()
        _k1_cut_geometry(cfs)
        cfs.cut_switch_button = "not_there"
        cfs.cmd_CFS_CUT(_gcmd(ints={"BOX": 1}, floats={"TEMP": 220.0}))
        assert "G0 Y230.000 F7000" in _scripts(cfs)


class TestFeederAndBufferCommands:
    def test_feeder_engage_and_release(self):
        cfs, hw = _wired()
        cfs.cmd_CFS_FEEDER(_gcmd(ints={"ENABLE": 1}))
        cfs.cmd_CFS_FEEDER(_gcmd(ints={"ENABLE": 0}))
        assert _frames(hw, CMD_CTRL_CONNECTION_MOTOR_ACTION) == [b"\x01", b"\x00"]

    def test_buffer_reports_state(self):
        cfs, hw = _wired()
        gcmd = _gcmd()
        cfs.cmd_CFS_BUFFER(gcmd)
        hw.assert_command_received(CMD_GET_BUFFER_STATE)
        assert any(s.startswith("CFS_BUFFER: box 1: middle") for s in _infos(gcmd))

    def test_commands_refuse_when_disconnected(self):
        cfs, hw = _wired()
        cfs.is_connected = False
        with pytest.raises(Exception, match="not connected"):
            cfs.cmd_CFS_FEEDER(_gcmd(ints={"ENABLE": 1}))
        with pytest.raises(Exception, match="not connected"):
            cfs.cmd_CFS_BUFFER(_gcmd())


class TestHeartbeat:
    def test_off_by_default(self):
        cfs, hw = _wired()
        assert cfs.heartbeat_interval == 0.

    def test_polls_online_box_when_bus_free(self):
        cfs, hw = _wired()
        cfs.heartbeat_interval = 4.
        cfs._bus_lock.test.return_value = False
        before = hw.get_received_funcs().count(0x0A)
        nxt = cfs._heartbeat(100.0)
        assert nxt == 104.0
        assert hw.get_received_funcs().count(0x0A) == before + 1

    def test_skips_while_bus_busy(self):
        cfs, hw = _wired()
        cfs.heartbeat_interval = 4.
        cfs._bus_lock.test.return_value = True
        before = len(hw.get_received_funcs())
        assert cfs._heartbeat(100.0) == 104.0
        assert len(hw.get_received_funcs()) == before

    def test_stops_after_shutdown(self):
        cfs, hw = _wired()
        cfs.heartbeat_interval = 4.
        cfs._shutdown = True
        assert cfs._heartbeat(100.0) is cfs.reactor.NEVER
