# SPDX-License-Identifier: GPL-3.0-or-later
"""
test_afc_bridge.py: the AFC-compatible view (afc_compat) used by Fluidd's AFC panel.

Status objects, lane-data commands and their persistence, tool mapping, auto-refill
target selection and the load/unload/refill scripts the bridge runs.
"""

import sys
import os
import json
import unittest.mock as mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import CFSAfcBridge, parse_slicer_filaments

from tests.mock_cfs import MockCFSHardware
from tests.conftest import make_wired_controller


def _bridge(tmp_path, present=(True, True, True, True)):
    hw = MockCFSHardware(box_count=1)
    cfs, ser = make_wired_controller(hw, box_count=1, retry_count=1)
    cfs._run_auto_addressing()
    cfs._slots = {i: {"present": p, "material": "unknown", "remain": 100}
                  for i, p in enumerate(present)}
    cfs._toolhead_filament_detected = lambda: False
    data_file = str(tmp_path / "cfs_lanes.json")
    config = mock.MagicMock()
    config.get.side_effect = lambda k, d=None: {"lane_data_file": data_file}.get(k, d)
    bridge = CFSAfcBridge(cfs, config)
    return bridge, cfs, hw, data_file


def _gcmd(**params):
    gcmd = mock.MagicMock()
    gcmd.get.side_effect = lambda k, d=None, **kw: params.get(k, d)
    gcmd.get_int.side_effect = lambda k, d=None, **kw: int(params[k]) if k in params else d
    gcmd.get_float.side_effect = lambda k, d=None, **kw: float(params[k]) if k in params else d
    gcmd.error.side_effect = lambda m: Exception(m)
    return gcmd


def _scripts(cfs):
    return [c.args[0] for c in cfs.gcode.run_script_from_command.call_args_list]


class TestObjectsAndCommands:
    def test_registers_afc_objects(self, tmp_path):
        bridge, cfs, hw, _ = _bridge(tmp_path)
        names = [c.args[0] for c in cfs.printer.add_object.call_args_list]
        for n in ("AFC", "AFC_CFS CFS", "AFC_hub CFS", "AFC_extruder extruder",
                  "AFC_lane slot1", "AFC_lane slot4"):
            assert n in names

    def test_registers_afc_gcode_surface(self, tmp_path):
        bridge, cfs, hw, _ = _bridge(tmp_path)
        cmds = [c.args[0] for c in cfs.gcode.register_command.call_args_list]
        for c in ("SET_COLOR", "SET_MATERIAL", "SET_MAP", "SET_RUNOUT", "CHANGE_TOOL",
                  "TOOL_UNLOAD", "LANE_UNLOAD", "AFC_SET_SPOOL_TEMP", "CFS_AUTO_REFILL"):
            assert c in cmds


class TestLaneStatus:
    def test_ready_and_empty_slots(self, tmp_path):
        bridge, cfs, hw, _ = _bridge(tmp_path, present=(True, False, True, True))
        s0, s1 = bridge._lane_status(0), bridge._lane_status(1)
        assert (s0["status"], s0["filament_status"], s0["load"], s0["prep"]) == \
            ("Loaded", "Ready", True, True)
        assert (s1["status"], s1["filament_status"], s1["load"]) == ("None", "Not Ready", False)
        assert s0["map"] == "T0" and s0["lane"] == 1 and s0["name"] == "slot1"

    def test_tooled_slot_and_afc_current(self, tmp_path):
        bridge, cfs, hw, _ = _bridge(tmp_path)
        cfs._active_tool = 2
        cfs._toolhead_filament_detected = lambda: True
        assert bridge._lane_status(2)["status"] == "Tooled"
        afc = bridge._afc_status(0)
        assert afc["current_load"] == "slot3" and afc["current_lane"] == "slot3"
        assert afc["lanes"] == ["slot1", "slot2", "slot3", "slot4"]
        assert afc["units"] == ["CFS CFS"]
        assert bridge._extruder_status(0)["lane_loaded"] == "slot3"

    def test_weight_from_box_remain(self, tmp_path):
        bridge, cfs, hw, _ = _bridge(tmp_path)
        cfs._slots[0]["remain"] = 40
        assert bridge._lane_status(0)["weight"] == 400.0


class TestLaneData:
    def test_set_color_material_persist(self, tmp_path):
        bridge, cfs, hw, data_file = _bridge(tmp_path)
        bridge.cmd_SET_COLOR(_gcmd(LANE="slot2", COLOR="ff1e1e"))
        bridge.cmd_SET_MATERIAL(_gcmd(LANE="slot2", MATERIAL="PLA"))
        bridge.cmd_AFC_SET_SPOOL_TEMP(_gcmd(LANE="slot2", EXTRUDER_TEMP="215"))
        saved = json.load(open(data_file))
        assert saved["slot2"]["color"] == "#FF1E1E"
        assert saved["slot2"]["material"] == "PLA"
        assert saved["slot2"]["extruder_temp"] == 215.0
        again, *_ = _bridge(tmp_path)          # a restart reloads the file
        assert again._lane_status(1)["color"] == "#FF1E1E"
        assert again._lane_status(1)["material"] == "PLA"

    def test_bad_color_rejected(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        with pytest.raises(Exception, match="hex"):
            bridge.cmd_SET_COLOR(_gcmd(LANE="slot1", COLOR="red"))

    def test_unknown_lane_rejected(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        with pytest.raises(Exception, match="Unknown lane"):
            bridge.cmd_SET_MATERIAL(_gcmd(LANE="lane9", MATERIAL="PLA"))

    def test_set_map_swaps(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        bridge.cmd_SET_MAP(_gcmd(LANE="slot3", MAP="T0"))
        assert bridge.lanes[2]["map"] == "T0"
        assert bridge.lanes[0]["map"] == "T2"          # slot1 took slot3's old tool
        assert bridge.tool_map()["T0"] == 2 and bridge.tool_map()["T2"] == 0

    def test_set_runout(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        bridge.cmd_SET_RUNOUT(_gcmd(LANE="slot1", RUNOUT="slot2"))
        assert bridge.lanes[0]["runout_lane"] == "slot2"
        bridge.cmd_SET_RUNOUT(_gcmd(LANE="slot1", RUNOUT="NONE"))
        assert bridge.lanes[0]["runout_lane"] is None
        with pytest.raises(Exception):
            bridge.cmd_SET_RUNOUT(_gcmd(LANE="slot1", RUNOUT="slot1"))


class TestRefill:
    def _same(self, bridge, idx, mat="PLA", col="#FFFFFF"):
        bridge.lanes[idx]["material"], bridge.lanes[idx]["color"] = mat, col

    def test_explicit_runout_lane_wins(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        bridge.lanes[0]["runout_lane"] = "slot4"
        assert bridge.refill_target(0) == 3

    def test_explicit_runout_lane_must_have_filament(self, tmp_path):
        bridge, *_ = _bridge(tmp_path, present=(True, True, True, False))
        bridge.lanes[0]["runout_lane"] = "slot4"
        assert bridge.refill_target(0) is None

    def test_same_material_and_colour(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        self._same(bridge, 0); self._same(bridge, 1, col="#FF0000"); self._same(bridge, 2)
        assert bridge.refill_target(0) == 2

    def test_case_insensitive_match(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        self._same(bridge, 0, "pla", "#ffffff"); self._same(bridge, 3, "PLA", "#FFFFFF")
        assert bridge.refill_target(0) == 3

    def test_no_match_without_colour(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        bridge.lanes[0]["material"] = bridge.lanes[1]["material"] = "PLA"
        assert bridge.refill_target(0) is None

    def test_auto_refill_loads_and_resumes(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        self._same(bridge, 0); self._same(bridge, 2)
        bridge.lanes[2]["extruder_temp"] = 215.0
        bridge.cmd_CFS_AUTO_REFILL(_gcmd(FROM="0"))
        assert _scripts(cfs) == ["CFS_TOOL TOOL=2 TEMP=215", "RESUME"]
        # T0 now points at the refill slot, so the slicer's next T0 stays on slot3
        assert bridge.tool_map()["T0"] == 2 and bridge.lanes[0]["map"] == "T2"

    def test_auto_refill_without_target_stays_paused(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        gcmd = _gcmd(FROM="0")
        bridge.cmd_CFS_AUTO_REFILL(gcmd)
        assert _scripts(cfs) == []
        assert bridge._afc_status(0)["message"]["type"] == "warning"

    def test_auto_refill_unknown_source(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        bridge.cmd_CFS_AUTO_REFILL(_gcmd(FROM="-1"))
        assert _scripts(cfs) == []


class TestLoadUnload:
    def test_change_tool_runs_cfs_tool(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        bridge.cmd_CHANGE_TOOL(_gcmd(LANE="slot4"))
        assert _scripts(cfs) == ["CFS_TOOL TOOL=3"]

    def test_tool_unload(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        bridge.cmd_TOOL_UNLOAD(_gcmd(LANE="slot2"))
        bridge.cmd_TOOL_UNLOAD(_gcmd())
        assert _scripts(cfs) == ["CFS_UNLOAD TOOL=1", "CFS_UNLOAD"]

    def test_lane_unload_only_for_slot_in_path(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        cfs._active_tool = 1
        bridge.cmd_LANE_UNLOAD(_gcmd(LANE="slot1"))
        bridge.cmd_LANE_UNLOAD(_gcmd(LANE="slot2"))
        assert _scripts(cfs) == ["CFS_UNLOAD TOOL=1"]


class TestPresenceRefresh:
    def test_refresh_from_channel_0_bitmask(self, tmp_path):
        bridge, cfs, hw, _ = _bridge(tmp_path)
        cfs.refresh_slot_presence(0x01)            # mock answers ch 0x00 with 0x01
        assert [cfs._slots[i]["present"] for i in range(4)] == [True, False, False, False]


ORCA_TAIL = """; filament used [mm] = 1200.50, 0.00, 340.10, 15.00
; filament used [g] = 3.58, 0.00, 1.01, 0.04
; CONFIG_BLOCK_START
; filament_colour = #FFFFFF;#00FF00;#000000;#FFFFFF
; filament_type = PLA;PETG;PLA;PLA
; CONFIG_BLOCK_END
"""


def _real_slots(bridge):
    """The user's CFS: slot1 PLA white, slot2 PETG white, slot3 PLA pink, slot4 PLA black."""
    for idx, (mat, col) in enumerate((("PLA", "#FFFFFF"), ("PETG", "#FFFFFF"),
                                      ("PLA", "#FFC0CB"), ("PLA", "#000000"))):
        bridge.lanes[idx]["material"], bridge.lanes[idx]["color"] = mat, col


class TestSlicerParse:
    def test_orca_block(self):
        f = parse_slicer_filaments(ORCA_TAIL)
        assert [(x["tool"], x["material"], x["color"], x["used"]) for x in f] == [
            (0, "PLA", "#FFFFFF", True), (1, "PETG", "#00FF00", False),
            (2, "PLA", "#000000", True), (3, "PLA", "#FFFFFF", True)]

    def test_alpha_and_no_usage(self):
        f = parse_slicer_filaments("; filament_colour = #ffc0cbff\n; filament_type = PLA\n")
        assert f == [{"tool": 0, "material": "PLA", "color": "#FFC0CB", "used": True}]

    def test_no_slicer_data(self):
        assert parse_slicer_filaments("G1 X1\n; layer 2\n") == []


class TestAutoMap:
    def test_maps_by_material_and_colour(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        _real_slots(bridge)
        chosen, warnings, errors = bridge.auto_map(parse_slicer_filaments(ORCA_TAIL))
        # T0 and T3 both want white PLA: they share slot1; black goes to slot4
        assert chosen == {"T0": 0, "T2": 3, "T3": 0} and not warnings and not errors
        bridge.apply_map(chosen)
        tm = bridge.tool_map()
        assert (tm["T0"], tm["T2"], tm["T3"]) == (0, 3, 0)

    def test_white_petg_never_used_for_pla(self, tmp_path):
        bridge, *_ = _bridge(tmp_path, present=(False, True, True, True))
        _real_slots(bridge)
        text = "; filament_type = PLA\n; filament_colour = #FFFFFF\n"
        chosen, warnings, errors = bridge.auto_map(parse_slicer_filaments(text))
        # closest PLA is pink; PETG white is skipped
        assert chosen == {"T0": 2} and warnings and not errors

    def test_missing_material_is_an_error(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        cfs.refresh_slot_presence = lambda addr: None
        _real_slots(bridge)
        path = tmp_path / "x.gcode"
        path.write_text("; filament_type = ABS\n; filament_colour = #FF0000\n")
        with pytest.raises(Exception, match="ABS"):
            bridge.cmd_CFS_AUTO_MAP(_gcmd(FILE=str(path)))
        assert bridge._afc_status(0)["message"]["type"] == "error"

    def test_dry_run_changes_nothing(self, tmp_path):
        bridge, cfs, *_ = _bridge(tmp_path)
        cfs.refresh_slot_presence = lambda addr: None
        _real_slots(bridge)
        path = tmp_path / "x.gcode"
        path.write_text("; filament_type = PLA\n; filament_colour = #000000\n")
        bridge.cmd_CFS_AUTO_MAP(_gcmd(FILE=str(path), DRY="1"))
        assert bridge.tool_map()["T0"] == 0
        bridge.cmd_CFS_AUTO_MAP(_gcmd(FILE=str(path)))
        assert bridge.tool_map()["T0"] == 3

    def test_distinct_filaments_get_distinct_slots(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        _real_slots(bridge)
        text = "; filament_type = PLA;PLA\n; filament_colour = #FFFFFF;#F0F0F0\n"
        chosen, warnings, errors = bridge.auto_map(parse_slicer_filaments(text))
        assert chosen["T0"] == 0 and chosen["T1"] != 0

    def test_aliases_persist_and_set_map_clears(self, tmp_path):
        bridge, cfs, hw, data_file = _bridge(tmp_path)
        _real_slots(bridge)
        bridge.apply_map({"T0": 0, "T3": 0})
        assert json.load(open(data_file))["_aliases"] == {"T3": 0}
        bridge2 = CFSAfcBridge(cfs, _cfg(data_file))
        assert bridge2.tool_map()["T3"] == 0
        bridge2.cmd_SET_MAP(_gcmd(LANE="slot2", MAP="T3"))
        assert bridge2.tool_map()["T3"] == 1


def _cfg(data_file):
    config = mock.MagicMock()
    config.get.side_effect = lambda k, d=None: {"lane_data_file": data_file}.get(k, d)
    return config


class TestMaterialNames:
    def test_plus_variant_matches(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        _real_slots(bridge)
        text = "; filament_type = PLA+\n; filament_colour = #FFFFFF\n"
        chosen, warnings, errors = bridge.auto_map(parse_slicer_filaments(text))
        assert chosen == {"T0": 0} and not warnings and not errors

    def test_cf_is_a_different_material(self, tmp_path):
        bridge, *_ = _bridge(tmp_path)
        _real_slots(bridge)
        text = "; filament_type = PLA-CF\n; filament_colour = #000000\n"
        assert bridge.auto_map(parse_slicer_filaments(text))[2]
