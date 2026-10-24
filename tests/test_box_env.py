# SPDX-License-Identifier: GPL-3.0-or-later
"""Box temperature/humidity from the idle 0x0A reply (b0 = deg C, b1 = % RH)."""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import CFSBoxSensor, build_message, CMD_GET_BOX_STATE
from tests.conftest import make_wired_controller
from tests.mock_cfs import MockCFSHardware


def _wired():
    hw = MockCFSHardware(box_count=1)
    cfs, _ = make_wired_controller(hw, box_count=1)
    cfs._run_auto_addressing()
    return hw, cfs


def test_idle_reply_records_temperature_and_humidity():
    _, cfs = _wired()
    cfs.get_box_state(0x01, retries=1)          # mock replies 1c 24 00 00
    assert cfs._box_env[0x01] == (28.0, 36.0)
    assert cfs.get_status(0.)["box_env"] == {"box1": {"temperature": 28.0, "humidity": 36.0}}
    flat = cfs._flat_box_status()["T1"]
    assert flat["temperature"] == 28 and flat["dry_and_humidity"] == 36


def test_event_reply_is_not_an_env_reading():
    hw, cfs = _wired()
    # insert push: STATUS 0x30, data is a per-slot phase array
    hw._resp_get_box_state = lambda addr, data: build_message(
        addr, 0x30, CMD_GET_BOX_STATE, bytes([0x03, 0x00, 0x00, 0x00]))
    cfs.get_box_state(0x01, retries=1)
    assert 0x01 not in cfs._box_env


def test_out_of_range_values_are_ignored():
    hw, cfs = _wired()
    hw._resp_get_box_state = lambda addr, data: build_message(
        addr, 0x00, CMD_GET_BOX_STATE, bytes([0xC8, 0xFF, 0x00, 0x00]))
    cfs.get_box_state(0x01, retries=1)
    assert 0x01 not in cfs._box_env


def test_box_sensor_object():
    _, cfs = _wired()
    printer = mock.MagicMock()
    heaters = mock.MagicMock()
    heaters.available_sensors = []
    printer.load_object.return_value = heaters
    config = mock.MagicMock()
    config.get_printer.return_value = printer

    sensor = CFSBoxSensor(cfs, config, "cfs_box")

    printer.add_object.assert_called_once_with("temperature_sensor cfs_box", sensor)
    assert heaters.available_sensors == ["temperature_sensor cfs_box"]
    assert sensor.get_status(0.)["temperature"] is None

    cfs._box_env[0x01] = (21.0, 63.0)
    st = sensor.get_status(0.)
    assert st["temperature"] == 21.0 and st["humidity"] == 63.0
    assert st["measured_min_temp"] == st["measured_max_temp"] == 21.0
    assert sensor.get_temp(0.) == (21.0, 0.)


def test_park_guard_runs_before_the_park_command():
    _, cfs = _wired()
    calls = []
    handlers = {"_TOOLHEAD_PARK_PAUSE_CANCEL": lambda g: calls.append("park")}

    def register(name, func, desc=None):
        if func is None:
            return handlers.pop(name, None)
        handlers[name] = func
    cfs.gcode.register_command.side_effect = register
    cfs.gcode.run_script_from_command.side_effect = lambda s: calls.append(s)
    cfs.park_guard = ["_TOOLHEAD_PARK_PAUSE_CANCEL", "_MISSING"]
    cfs.park_guard_gcode = "_CFS_CHUTE_ESCAPE"

    cfs._install_park_guard()
    handlers["_TOOLHEAD_PARK_PAUSE_CANCEL"](mock.MagicMock())

    assert calls == ["_CFS_CHUTE_ESCAPE", "park"]
    assert "_MISSING" not in handlers
