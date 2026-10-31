# SPDX-License-Identifier: GPL-3.0-or-later
"""abortable_waits: a cancel request stops M109/M190/TEMPERATURE_WAIT/ABORTABLE_WAIT."""

import os
import sys
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import abortable_waits


class GcodeError(Exception):
    pass


class FakeGcode:
    error = GcodeError

    def __init__(self):
        self.handlers = {}
        self.gcode_help = {}
        self.respond_info = mock.MagicMock()
        self.respond_raw = mock.MagicMock()

    def register_command(self, name, func, desc=None):
        if func is None:
            return self.handlers.pop(name, None)
        assert name not in self.handlers, name
        self.handlers[name] = func
        if desc:
            self.gcode_help[name] = desc


class FakeReactor:
    def __init__(self):
        self.now = 0.
        self.on_pause = None

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime
        if self.on_pause:
            self.on_pause(self.now)
        return self.now


class FakeHeater:
    def __init__(self, reach_at):
        self.reach_at = reach_at
        self.target = 0.

    def check_busy(self, eventtime):
        return eventtime < self.reach_at


def _gcmd(**params):
    g = mock.MagicMock()
    g.get_float.side_effect = lambda k, default=None, **kw: params.get(k, default)
    g.get_int.side_effect = lambda k, default=None, **kw: params.get(k, default)
    g.get.side_effect = lambda k, default=None: params.get(k, default)
    g.error.side_effect = lambda msg: GcodeError(msg)
    return g


@pytest.fixture
def env():
    reactor = FakeReactor()
    gcode = FakeGcode()
    sdcard = mock.MagicMock()
    sdcard.is_active.return_value = True
    bed = mock.MagicMock()
    bed.heater = FakeHeater(reach_at=30.)
    heaters = mock.MagicMock()
    heaters.available_sensors = ["temperature_sensor chamber_temp"]
    heaters.heaters = {}
    heaters._get_temp.return_value = "B:20 /60"
    webhooks = mock.MagicMock()
    webhooks._endpoints = {"pause_resume/cancel": mock.MagicMock(name="cancel_cb"),
                           "gcode/script": mock.MagicMock(name="script_cb")}
    objects = {"gcode": gcode, "virtual_sdcard": sdcard, "heater_bed": bed,
               "toolhead": mock.MagicMock(), "webhooks": webhooks}
    printer = mock.MagicMock()
    printer.get_reactor.return_value = reactor
    printer.lookup_object.side_effect = lambda n, default=None: objects.get(n, default)
    printer.load_object.return_value = heaters
    printer.is_shutdown.return_value = False
    printer.get_start_args.return_value = {}
    events = {}
    printer.register_event_handler.side_effect = lambda e, cb: events.__setitem__(e, cb)
    config = mock.MagicMock()
    config.get_printer.return_value = printer
    original_cancel = mock.MagicMock(name="CANCEL_PRINT")
    for name in ("M109", "M190", "TEMPERATURE_WAIT", "SDCARD_RESET_FILE"):
        gcode.handlers[name] = mock.MagicMock(name=name)
    gcode.handlers["CANCEL_PRINT"] = original_cancel
    aw = abortable_waits.load_config(config)
    events["klippy:connect"]()
    return {"aw": aw, "gcode": gcode, "reactor": reactor, "sdcard": sdcard, "bed": bed,
            "webhooks": webhooks, "heaters": heaters, "cancel": original_cancel}


def _cancel_request(env):
    env["webhooks"]._endpoints["pause_resume/cancel"](mock.MagicMock())


def test_bed_wait_finishes_without_cancel(env):
    env["gcode"].handlers["M190"](_gcmd(S=60.))
    env["heaters"].set_temperature.assert_called_with(env["bed"].heater, 60., False)
    assert env["reactor"].now >= 30.


def test_cancel_request_stops_the_bed_wait_once(env):
    env["reactor"].on_pause = lambda now: now >= 5. and _cancel_request(env)
    with pytest.raises(GcodeError, match="Print cancelled"):
        env["gcode"].handlers["M190"](_gcmd(S=60.))
    assert env["reactor"].now < 10.
    assert env["aw"].aborting is False          # one shot
    env["reactor"].on_pause = None
    env["gcode"].handlers["M190"](_gcmd(S=60.))  # the next wait runs normally


def test_cancel_endpoint_still_reaches_klipper(env):
    _cancel_request(env)
    assert env["aw"].aborting is True


def test_cancel_script_sets_the_flag_other_scripts_do_not(env):
    script_cb = env["webhooks"]._endpoints["gcode/script"]
    req = mock.MagicMock()
    req.get_str.return_value = "G28"
    script_cb(req)
    assert env["aw"].aborting is False
    req.get_str.return_value = "  cancel_print"
    script_cb(req)
    assert env["aw"].aborting is True


def test_no_flag_when_not_printing(env):
    env["sdcard"].is_active.return_value = False
    _cancel_request(env)
    assert env["aw"].aborting is False


def test_cancel_print_clears_the_flag_before_running(env):
    env["aw"].aborting = True
    seen = []
    env["cancel"].side_effect = lambda g: seen.append(env["aw"].aborting)
    env["gcode"].handlers["CANCEL_PRINT"](_gcmd())
    assert seen == [False]


def test_abortable_wait_dwells_and_stops_on_cancel(env):
    env["gcode"].handlers["ABORTABLE_WAIT"](_gcmd(SECONDS=15.))
    assert env["reactor"].now == pytest.approx(15.)
    env["reactor"].on_pause = lambda now: now >= 3. and _cancel_request(env)
    with pytest.raises(GcodeError):
        env["gcode"].handlers["ABORTABLE_WAIT"](_gcmd(SECONDS=15.))
    assert env["reactor"].now < 20.


def test_temperature_wait_returns_inside_the_range(env):
    sensor = mock.MagicMock()
    sensor.get_temp.side_effect = lambda t: (20. + t, 0.)
    objects_sensor = {"temperature_sensor chamber_temp": sensor}
    printer = env["aw"].printer
    base = printer.lookup_object.side_effect
    printer.lookup_object.side_effect = lambda n, d=None: objects_sensor.get(n) or base(n, d)
    env["gcode"].handlers["TEMPERATURE_WAIT"](
        _gcmd(SENSOR="temperature_sensor chamber_temp", MINIMUM=30.))
    assert env["reactor"].now >= 10.


def test_m109_m190_macros_are_left_alone(env):
    """SimpleAF overrides M109/M190 with macros that wait via TEMPERATURE_WAIT: keep them."""
    printer = env["aw"].printer
    gcode = FakeGcode()
    macro_m190 = mock.MagicMock(name="macro M190")
    for name in ("M109", "TEMPERATURE_WAIT", "CANCEL_PRINT", "SDCARD_RESET_FILE"):
        gcode.handlers[name] = mock.MagicMock(name=name)
    gcode.handlers["M190"] = macro_m190
    objects = {"gcode": gcode, "gcode_macro M190": mock.MagicMock(),
               "webhooks": env["webhooks"], "virtual_sdcard": env["sdcard"]}
    printer.lookup_object.side_effect = lambda n, d=None: objects.get(n, d)
    events = {}
    printer.register_event_handler.side_effect = lambda e, cb: events.__setitem__(e, cb)
    config = mock.MagicMock()
    config.get_printer.return_value = printer
    abortable_waits.load_config(config)
    events["klippy:connect"]()
    assert gcode.handlers["M190"] is macro_m190
    assert gcode.handlers["M109"].__self__.__class__.__name__ == "AbortableWaits"


class ConfigError(Exception):
    pass


@pytest.mark.parametrize("break_it", ["endpoints", "get_temp", "script"])
def test_missing_internal_is_a_config_error_and_wraps_nothing(env, break_it):
    printer = env["aw"].printer
    printer.config_error = ConfigError
    gcode = FakeGcode()
    for name in ("M109", "M190", "TEMPERATURE_WAIT", "CANCEL_PRINT", "SDCARD_RESET_FILE"):
        gcode.handlers[name] = mock.MagicMock(name=name)
    before = dict(gcode.handlers)
    webhooks = mock.MagicMock()
    webhooks._endpoints = {"gcode/script": mock.MagicMock()}
    heaters = mock.MagicMock()
    if break_it == "endpoints":
        webhooks = object()
    elif break_it == "get_temp":
        heaters._get_temp = None
    else:
        webhooks._endpoints = {}
    objects = {"gcode": gcode, "webhooks": webhooks}
    printer.lookup_object.side_effect = lambda n, d=None: objects.get(n, d)
    printer.load_object.return_value = heaters
    events = {}
    printer.register_event_handler.side_effect = lambda e, cb: events.__setitem__(e, cb)
    config = mock.MagicMock()
    config.get_printer.return_value = printer
    abortable_waits.load_config(config)
    with pytest.raises(ConfigError, match="abortable_waits"):
        events["klippy:connect"]()
    assert {k: v for k, v in gcode.handlers.items() if k != "ABORTABLE_WAIT"} == before


def test_kalico_heater_interrupt_stops_the_wait(env):
    count = [0]
    env["gcode"].get_interrupt_counter = lambda: count[0]
    env["reactor"].on_pause = lambda now: now >= 5. and count.__setitem__(0, 1)
    with pytest.raises(GcodeError, match="interrupted"):
        env["gcode"].handlers["M190"](_gcmd(S=60.))
    assert env["reactor"].now < 10.
