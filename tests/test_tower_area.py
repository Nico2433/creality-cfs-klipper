# SPDX-License-Identifier: GPL-3.0-or-later
"""first_layer_extra_area: the prime tower on the first layer, outside the declared objects."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from creality_cfs import first_layer_extra_area

HEADER = """EXCLUDE_OBJECT_DEFINE NAME=part CENTER=110,110 POLYGON=[[100,100],[120,100],[120,120],[100,120]]
START_PRINT EXTRUDER_TEMP=220
G1 X5 Y5 E10 ; start G-code purge, before the first layer
M83
"""


def test_tower_outside_the_objects():
    text = HEADER + """;LAYER_CHANGE
G1 X101 Y101
G1 X119 Y119 E1.0
G1 X160 Y250
G1 X200 Y250 E2.0
G1 X200 Y262 E0.5 ; tower wall
G1 X150 Y150 E-0.8 ; wipe/retract is not extrusion
G1 X10 Y10 ; travel only
;LAYER_CHANGE
G1 X290 Y290 E1.0 ; second layer is ignored
"""
    assert first_layer_extra_area(text) == (200., 250., 200., 262.)


def test_single_object_has_no_extra_area():
    text = HEADER + """;LAYER_CHANGE
G1 X101 Y101
G1 X119 Y119 E1.0
G1 X121 Y121 E1.0 ; within the margin around the object
"""
    assert first_layer_extra_area(text) is None


def test_absolute_extrusion_mode():
    text = HEADER.replace("M83", "M82") + """G92 E0
;LAYER_CHANGE
G1 X160 Y250 E1.0
G1 X170 Y255 E1.0 ; same E: travel in absolute mode
G1 X200 Y260 E2.0
"""
    assert first_layer_extra_area(text) == (160., 250., 200., 260.)
