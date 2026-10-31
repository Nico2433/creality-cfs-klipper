# SPDX-License-Identifier: GPL-3.0-or-later
"""Let a cancel stop the long waits of a print start.

Klipper runs one G-code command at a time, and a slicer's START_PRINT line is one command:
heating, bed soak and chamber waits included. A CANCEL_PRINT from the web UI or the
touchscreen queues behind it, so the whole preparation runs before the print is cancelled.

This module notices the cancel request when it arrives (the `pause_resume/cancel` endpoint
Moonraker uses, or a `CANCEL_PRINT` / `SDCARD_RESET_FILE` sent as a script) and raises a flag.
TEMPERATURE_WAIT is replaced by an equivalent that checks the flag every second (SimpleAF's
M109/M190 macros wait with it); M109 and M190 themselves are replaced only when no gcode_macro
already overrides them,
and ABORTABLE_WAIT SECONDS= is a dwell that does the same (for soak loops in macros). A wait
that sees the flag raises an error once: the macro stops, Klipper's on_error_gcode
(CANCEL_PRINT on SimpleAF) runs, then the queued CANCEL_PRINT. CANCEL_PRINT and
SDCARD_RESET_FILE clear the flag first, so their own heating (e.g. unloading the filament)
is never cut short.

Config: [abortable_waits]  (no options)
"""

import logging

CANCEL_SCRIPTS = ("CANCEL_PRINT", "SDCARD_RESET_FILE")


class AbortableWaits:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.heaters = self.printer.load_object(config, "heaters")
        self.aborting = False
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.gcode.register_command("ABORTABLE_WAIT", self.cmd_ABORTABLE_WAIT,
                                    desc=self.cmd_ABORTABLE_WAIT_help)

    # -- setup ---------------------------------------------------------------------------

    def _check_internals(self, webhooks):
        """Refuse to start, before wrapping anything, on a host without the internals used."""
        missing = [name for name, ok in (
            ("webhooks._endpoints", isinstance(getattr(webhooks, "_endpoints", None), dict)),
            ("heaters._get_temp", callable(getattr(self.heaters, "_get_temp", None))),
            ("gcode.gcode_help", isinstance(getattr(self.gcode, "gcode_help", None), dict)))
            if not ok]
        if not missing and "gcode/script" not in webhooks._endpoints:
            missing.append("the gcode/script endpoint")
        if missing:
            raise self.printer.config_error(
                "abortable_waits: this Klipper/Kalico lacks %s; remove [abortable_waits]"
                % ", ".join(missing))

    def _handle_connect(self):
        webhooks = self.printer.lookup_object("webhooks")
        self._check_internals(webhooks)
        # Every section is loaded by now, so the stock M109/M190/TEMPERATURE_WAIT exist.
        for name, handler, desc in (
                ("M109", self.cmd_M109, None),
                ("M190", self.cmd_M190, None),
                ("TEMPERATURE_WAIT", self.cmd_TEMPERATURE_WAIT,
                 "Wait for a temperature on a sensor (stops on a print cancel)")):
            if self.printer.lookup_object("gcode_macro " + name, None) is not None:
                continue    # a macro already wraps it (SimpleAF waits via TEMPERATURE_WAIT)
            self.gcode.register_command(name, None)
            self.gcode.register_command(name, handler, desc=desc)
        for name in CANCEL_SCRIPTS:
            self._clear_before(name)
        self._wrap_endpoint(webhooks, "pause_resume/cancel", lambda wr: True)
        self._wrap_endpoint(webhooks, "gcode/script", self._is_cancel_script)

    def _clear_before(self, name):
        original = self.gcode.register_command(name, None)
        if original is None:
            logging.warning("abortable_waits: command %s not found", name)
            return
        desc = self.gcode.gcode_help.get(name)

        def handler(gcmd):
            self.aborting = False
            original(gcmd)
        self.gcode.register_command(name, handler, desc=desc)

    def _wrap_endpoint(self, webhooks, path, is_cancel):
        original = webhooks._endpoints.get(path)
        if original is None:    # pause_resume/cancel: only with a [pause_resume] section
            logging.warning("abortable_waits: endpoint %s not found", path)
            return

        def callback(web_request):
            try:
                if is_cancel(web_request):
                    self._note_cancel()
            except Exception:
                logging.exception("abortable_waits: cancel check failed")
            return original(web_request)
        webhooks._endpoints[path] = callback

    @staticmethod
    def _is_cancel_script(web_request):
        words = web_request.get_str("script", "").split()
        return bool(words) and words[0].upper() in CANCEL_SCRIPTS

    def _note_cancel(self):
        sdcard = self.printer.lookup_object("virtual_sdcard", None)
        if sdcard is not None and sdcard.is_active() and not self.aborting:
            self.aborting = True
            logging.info("abortable_waits: cancel requested, stopping the current wait")
            self.gcode.respond_info("Cancel requested: stopping the current wait")

    def _check_abort(self):
        if self.aborting:
            self.aborting = False
            raise self.gcode.error("Print cancelled")

    # -- waits ---------------------------------------------------------------------------

    def _wait(self, still_waiting):
        """Klipper's heater wait loop (M105 report every second) plus the cancel check."""
        if self.printer.get_start_args().get("debugoutput") is not None:
            return
        toolhead = self.printer.lookup_object("toolhead")
        # Kalico's HEATER_INTERRUPT bumps this counter; keep honouring it (mainline has none)
        counter = getattr(self.gcode, "get_interrupt_counter", None)
        start = counter() if counter else None
        eventtime = self.reactor.monotonic()
        while not self.printer.is_shutdown() and still_waiting(eventtime):
            self._check_abort()
            if counter and counter() != start:
                raise self.gcode.error("Command interrupted")
            toolhead.get_last_move_time()
            self.gcode.respond_raw(self.heaters._get_temp(eventtime))
            eventtime = self.reactor.pause(eventtime + 1.)
        self._check_abort()

    def _set_and_wait(self, heater, temp):
        self._check_abort()
        self.heaters.set_temperature(heater, temp, False)
        if temp:
            self._wait(heater.check_busy)

    def cmd_M109(self, gcmd):
        temp = gcmd.get_float("S", 0.)
        index = gcmd.get_int("T", None, minval=0)
        if index is not None:
            section = "extruder%d" % (index,) if index else "extruder"
            extruder = self.printer.lookup_object(section, None)
            if extruder is None:
                if temp <= 0.:
                    return
                raise gcmd.error("Extruder not configured")
        else:
            extruder = self.printer.lookup_object("toolhead").get_extruder()
        self._set_and_wait(extruder.get_heater(), temp)

    def cmd_M190(self, gcmd):
        heater_bed = self.printer.lookup_object("heater_bed")
        self._set_and_wait(heater_bed.heater, gcmd.get_float("S", 0.))

    def cmd_TEMPERATURE_WAIT(self, gcmd):
        sensor_name = gcmd.get("SENSOR")
        if sensor_name not in self.heaters.available_sensors:
            raise gcmd.error("Unknown sensor '%s'" % (sensor_name,))
        min_temp = gcmd.get_float("MINIMUM", float("-inf"))
        max_temp = gcmd.get_float("MAXIMUM", float("inf"), above=min_temp)
        if min_temp == float("-inf") and max_temp == float("inf"):
            raise gcmd.error("Error on 'TEMPERATURE_WAIT': missing MINIMUM or MAXIMUM.")
        if sensor_name in self.heaters.heaters:
            sensor = self.heaters.heaters[sensor_name]
        else:
            sensor = self.printer.lookup_object(sensor_name)

        def outside(eventtime):
            temp, _ = sensor.get_temp(eventtime)
            return not min_temp <= temp <= max_temp
        self._wait(outside)

    cmd_ABORTABLE_WAIT_help = "Dwell SECONDS= (like G4) but stop on a print cancel"

    def cmd_ABORTABLE_WAIT(self, gcmd):
        seconds = gcmd.get_float("SECONDS", minval=0.)
        self._check_abort()
        self.printer.lookup_object("toolhead").wait_moves()
        end = self.reactor.monotonic() + seconds
        now = self.reactor.monotonic()
        while now < end and not self.printer.is_shutdown():
            self._check_abort()
            now = self.reactor.pause(min(now + 1., end))
        self._check_abort()


def load_config(config):
    return AbortableWaits(config)
