# Slicer G-code for the K1 Max + CFS on SimpleAF

SimpleAF's `START_PRINT` does the whole start: home, heat, bed warp stabilisation, Eddy tap,
adaptive mesh, park, line purge. `cfs_k1max_simpleaf.cfg` (included after `cfs_k1max.cfg`)
hooks into it to map the slicer tools to slots, handle the chamber and load the first tool
before the line purge. The slicer only has to pass the parameters. `END_PRINT` returns the
filament to the box.

Parameters `START_PRINT` reads:

| Parameter | Used for |
|---|---|
| `EXTRUDER_TEMP` | nozzle temperature (also the first load) |
| `BED_TEMP` | bed temperature |
| `CHAMBER_TEMP` | chamber target (`M141`): the exhaust fan runs above it; side fan while heating if colder. 0 = default |
| `CHAMBER_MIN` | minimal chamber temperature (`M191`): wait for it after the bed is stable, before the Eddy tap. 0 = no wait |
| `INITIAL_TOOL` | slicer tool loaded before the line purge (slot from the Fluidd tool map / auto-map) |

`M141 S<t>` / `M191 S<t>` follow the usual Klipper/Marlin meaning (set target / set and wait) and
replace SimpleAF's versions; `S0` puts the exhaust target back to its default (35 C). An `M191`
sent before `START_PRINT`, while the bed is still cold, is remembered and waited for inside it.

Per colour change, `T<n>` reads two optional values set just before it:
`_CFS_SETTINGS next_temp` (new filament temperature) and `next_purge` (mm purged at the chute;
default `change_purge_len`, 30 mm).

## OrcaSlicer

Printer settings → Machine G-code.

Machine start G-code:

```
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single] CHAMBER_TEMP=[chamber_temperature] CHAMBER_MIN=[chamber_minimal_temperature[initial_no_support_extruder]] INITIAL_TOOL=[initial_no_support_extruder]
```

Chamber values come from the filament settings (Cooling → Chamber temperature: target and
minimal). Leave "Activate temperature control" off: START_PRINT handles it. Orca adds `G90`, `G21`, `M83`, the first `T` and `G92 E0` after it by itself.

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

Same placeholders as Orca (it is an Orca fork), but it has only a chamber target (no minimal).
It sends `M141 S<target>` before the start G-code and `M141 S0` at the end by itself; both work
with the macros above. Machine start G-code:

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
START_PRINT EXTRUDER_TEMP=[first_layer_temperature[initial_tool]] BED_TEMP=[first_layer_bed_temperature[initial_tool]] CHAMBER_TEMP=[chamber_temperature[initial_tool]] CHAMBER_MIN=[chamber_minimal_temperature[initial_tool]] INITIAL_TOOL=[initial_tool]
```

PrusaSlicer names the chamber settings like Orca (filament: Chamber temperature / Minimal
chamber temperature). Using them in the start G-code keeps it from adding its own M141/M191.

End G-code `END_PRINT`. Tool change G-code:

```
SET_GCODE_VARIABLE MACRO=_CFS_SETTINGS VARIABLE=next_temp VALUE={temperature[next_extruder]}
```

PrusaSlicer has no flush length per change, so the chute purges `change_purge_len` (30 mm);
keep the wipe tower on to purge the rest. Untested on hardware (no multi-tool PrusaSlicer
profile built yet).
