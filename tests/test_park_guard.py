# SPDX-License-Identifier: GPL-3.0-or-later
"""park_guard: run park_guard_gcode before the client's park commands (SimpleAF, opt-in)."""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from tests.conftest import make_wired_controller
from tests.mock_cfs import MockCFSHardware


def _cfs_with_handlers():
    cfs, _ = make_wired_controller(MockCFSHardware(box_count=1), box_count=1)
    calls = []
    handlers = {"_TOOLHEAD_PARK_PAUSE_CANCEL": lambda g: calls.append("park")}

    def register(name, func, desc=None):
        if func is None:
            return handlers.pop(name, None)
        handlers[name] = func
    cfs.gcode.register_command.reset_mock()
    cfs.gcode.register_command.side_effect = register
    cfs.gcode.run_script_from_command.side_effect = lambda s: calls.append(s)
    return cfs, handlers, calls


def test_park_guard_runs_before_the_park_command():
    cfs, handlers, calls = _cfs_with_handlers()
    cfs.park_guard = ["_TOOLHEAD_PARK_PAUSE_CANCEL", "_MISSING"]
    cfs.park_guard_gcode = "_CFS_CHUTE_ESCAPE"

    cfs._install_park_guard()
    handlers["_TOOLHEAD_PARK_PAUSE_CANCEL"](mock.MagicMock())

    assert calls == ["_CFS_CHUTE_ESCAPE", "park"]
    assert "_MISSING" not in handlers


def test_park_guard_off_by_default_wraps_nothing():
    cfs, handlers, calls = _cfs_with_handlers()
    assert cfs.park_guard == [] and cfs.park_guard_gcode == ""
    cfs.park_guard = ["_TOOLHEAD_PARK_PAUSE_CANCEL"]    # commands listed, but no guard macro

    cfs._install_park_guard()
    handlers["_TOOLHEAD_PARK_PAUSE_CANCEL"](mock.MagicMock())

    cfs.gcode.register_command.assert_not_called()
    assert calls == ["park"]
