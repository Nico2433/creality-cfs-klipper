# Slicer G-code for the K1 Max + CFS on SimpleAF

SimpleAF's `START_PRINT` does the whole start: home, heat, bed warp stabilisation, Eddy tap,
adaptive mesh, park, line purge. `cfs.cfg` hooks into it to map the slicer tools to slots,
handle the chamber and load the first tool before the line purge. The slicer only has to pass
the parameters. `END_PRINT` returns the filament to the box.

Parameters `START_PRINT` reads:

| Parameter | Used for |
|---|---|
| `EXTRUDER_TEMP` | nozzle temperature (also the first load) |
| `BED_TEMP` | bed temperature |
| `CHAMBER_TEMP` | exhaust fan target; side fan while heating if the chamber is colder. 0 = leave as is |
| `INITIAL_TOOL` | slicer tool loaded before the line purge (slot from the Fluidd tool map / auto-map) |

Per colour change, `T<n>` reads two optional values set just before it:
`_CFS_SETTINGS next_temp` (new filament temperature) and `next_purge` (mm purged at the chute;
default `change_purge_len`, 30 mm).

## OrcaSlicer

Printer settings → Machine G-code.

Machine start G-code:

```
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single] CHAMBER_TEMP=[chamber_temperature] INITIAL_TOOL=[initial_no_support_extruder]
```

Orca adds `G90`, `G21`, `M83`, the first `T` and `G92 E0` after it by itself.

Machine end G-code:

```
END_PRINT
```

Change filament G-code (Orca emits the `T` after it):

```
SET_GCODE_VARIABLE MACRO=_CFS_SETTINGS VARIABLE=next_temp VALUE={new_filament_temp}
SET_GCODE_VARIABLE MACRO=_CFS_SETTINGS VARIABLE=next_purge VALUE={flush_length}
```

`flush_length` comes from the flushing volumes (Orca's flush dialog), so a white→black change
purges more than black→white. With the prime tower on, the tower purges as well: lower
"Prime volume" or turn the tower off if the chute purge is enough.

## Creality Print (6.x / 7.x)

Same placeholders as Orca (it is an Orca fork). Machine start G-code:

```
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single] CHAMBER_TEMP=[chamber_temperature] INITIAL_TOOL=[initial_no_support_extruder]
```

End G-code `END_PRINT`, and the same two `SET_GCODE_VARIABLE` lines as the change filament
G-code. Its stock K1 Max change G-code (spiral lift, move to X38 Y230) is not needed: `T<n>`
does its own lift and moves.

## PrusaSlicer (2.8+)

Printer settings → General: Extruders 4, "Single Extruder Multi Material" on.
Custom G-code → Start G-code:

```
START_PRINT EXTRUDER_TEMP=[first_layer_temperature[initial_tool]] BED_TEMP=[first_layer_bed_temperature[initial_tool]] CHAMBER_TEMP=[chamber_temperature[initial_tool]] INITIAL_TOOL=[initial_tool]
```

End G-code `END_PRINT`. Tool change G-code:

```
SET_GCODE_VARIABLE MACRO=_CFS_SETTINGS VARIABLE=next_temp VALUE={temperature[next_extruder]}
```

PrusaSlicer has no flush length per change, so the chute purges `change_purge_len` (30 mm);
keep the wipe tower on to purge the rest. Untested on hardware (no multi-tool PrusaSlicer
profile built yet).
