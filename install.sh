#!/bin/sh
# Install the Creality CFS module on a K1 Max running SimpleAF or mainline Klipper/Kalico.
#
#   git clone -b k1-simpleaf https://github.com/Nico2433/creality-cfs-klipper.git <dir>
#   sh <dir>/install.sh [options]
#
# Run it from the checkout it installs: the module is linked from <dir>/src, so keep <dir>
# outside the printer config (SimpleAF: /usr/data/creality-cfs-klipper). Safe to run again: it
# updates what changed and backs up every file it edits.
#
# Options:
#   --dry-run            show what would change, change nothing
#   --uninstall          remove the links, includes, updater entry and the macros copies
#   --no-restart         do not restart Moonraker/Klipper at the end
#   --keep-config        do not overwrite macros/*.cfg files you have edited
#   --mode simpleaf|mainline   skip auto-detection
#   --klipper-dir DIR    Klipper/Kalico checkout (mainline default ~/klipper)
#   --printer-data DIR   printer_data folder (mainline default ~/printer_data)
#   --init-dir DIR       SimpleAF init scripts folder (default /etc/init.d)
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
DRY=0 UNINSTALL=0 RESTART=1 KEEP=0 MODE="" KLIPPER="" PD="" INIT_DIR=/etc/init.d
MOONRAKER_URL=${MOONRAKER_URL:-http://127.0.0.1:7125}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --no-restart) RESTART=0 ;;
    --keep-config) KEEP=1 ;;
    --mode) MODE=$2; shift ;;
    --klipper-dir) KLIPPER=$2; shift ;;
    --printer-data) PD=$2; shift ;;
    --init-dir) INIT_DIR=$2; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

say() { echo "cfs: $*"; }
die() { echo "cfs: ERROR: $*" >&2; exit 1; }

if [ -z "$MODE" ]; then
  if [ -f /usr/data/pellcorp.done ]; then MODE=simpleaf; else MODE=mainline; fi
fi
case "$MODE" in
  simpleaf) KLIPPER=${KLIPPER:-/usr/data/klipper}; PD=${PD:-/usr/data/printer_data} ;;
  mainline) KLIPPER=${KLIPPER:-$HOME/klipper}; PD=${PD:-$HOME/printer_data} ;;
  *) die "--mode must be simpleaf or mainline" ;;
esac
CONFIG=$PD/config
EXTRAS=$KLIPPER/klippy/extras
PRINTER_CFG=$CONFIG/printer.cfg
MOONRAKER_CONF=$CONFIG/moonraker.conf
BACKUP=$PD/cfs-backups/$(date +%Y%m%d_%H%M%S)
MODULES="creality_cfs.py abortable_waits.py"
if [ "$MODE" = simpleaf ]; then
  CFGS="cfs_k1max.cfg cfs_k1max_simpleaf.cfg"
else
  CFGS="cfs_k1max.cfg"
fi

[ -d "$EXTRAS" ] || die "no Klipper extras folder at $EXTRAS (use --klipper-dir)"
[ -f "$PRINTER_CFG" ] || die "no printer.cfg at $PRINTER_CFG (use --printer-data)"
case "$REPO/" in
  "$CONFIG"/*) die "the checkout $REPO is inside the printer config; clone it elsewhere" ;;
esac
[ -f "$REPO/src/creality_cfs.py" ] || die "run install.sh from the module checkout"

say "mode $MODE, klipper $KLIPPER, config $CONFIG, module $REPO"
[ "$DRY" = 1 ] && say "dry run: nothing is changed"

CHANGED_KLIPPER=0 CHANGED_MOONRAKER=0

# Run a command, or only print it in a dry run.
run() {
  if [ "$DRY" = 1 ]; then echo "  would run: $*"; else "$@"; fi
}

backup() {
  [ -e "$1" ] || return 0
  run mkdir -p "$BACKUP"
  run cp -p "$1" "$BACKUP/$(basename "$1")"
}

# Replace $1 with the file $2 when they differ (backing $1 up first).
install_file() {
  dest=$1 new=$2
  if [ -f "$dest" ] && cmp -s "$dest" "$new"; then
    rm -f "$new"; return 1
  fi
  backup "$dest"
  say "write $dest"
  # cat keeps the destination's owner and mode (mktemp files are 0600)
  if [ "$DRY" = 0 ]; then cat "$new" > "$dest"; fi
  rm -f "$new"
  return 0
}

# printer.cfg: drop every include of ours (also the old cfs.cfg and root-level layouts), then add
# the block after the last other [include] above SAVE_CONFIG. On SimpleAF that is after
# fan_control.cfg and client.cfg, which the overlay must follow.
edit_printer_cfg() {
  tmp=$(mktemp)
  BLOCK=$1 awk '
    function ours(l) {
      return l ~ /^\[include (macros\/)?cfs_k1max(_simpleaf)?\.cfg\][ \t]*$/ ||
             l ~ /^\[include cfs\.cfg\][ \t]*$/
    }
    { line[NR] = $0 }
    END {
      block = ENVIRON["BLOCK"]; last = 0; save = 0
      for (i = 1; i <= NR; i++) {
        if (!save && line[i] ~ /^#\*# <-+ SAVE_CONFIG/) save = i
        if (!save && !ours(line[i]) && line[i] ~ /^\[include /) last = i
      }
      if (block != "" && last == 0) print block
      for (i = 1; i <= NR; i++) {
        if (ours(line[i])) continue
        print line[i]
        if (block != "" && i == last) print block
      }
    }' "$PRINTER_CFG" > "$tmp"
  if install_file "$PRINTER_CFG" "$tmp"; then CHANGED_KLIPPER=1; fi
}

# moonraker.conf: remove our [update_manager creality_cfs] section, then append the new one.
edit_moonraker_conf() {
  [ -f "$MOONRAKER_CONF" ] || { say "no moonraker.conf, skipping the updater entry"; return 0; }
  tmp=$(mktemp)
  # the section ends at the next header or blank line, so comments after it are kept
  SECTION=$1 awk '
    BEGIN { section = ENVIRON["SECTION"] }
    /^\[update_manager creality_cfs\]/ { skip = 1; next }
    skip && /^[ \t]*$/ { skip = 0; next }
    skip && /^\[/ { skip = 0 }
    skip { next }
    { line[++n] = $0 }
    END {
      while (n > 0 && line[n] ~ /^[ \t]*$/) n--
      for (i = 1; i <= n; i++) print line[i]
      if (section != "") { print ""; print section }
    }' "$MOONRAKER_CONF" > "$tmp"
  if install_file "$MOONRAKER_CONF" "$tmp"; then CHANGED_MOONRAKER=1; fi
}

link_modules() {
  if [ "$MODE" = simpleaf ]; then
    # A SimpleAF/Klipper update can drop the links; the init script recreates them on boot.
    tmp=$(mktemp)
    sed -e "s|^SRC_DIR=.*|SRC_DIR=$REPO/src|" -e "s|^DST_DIR=.*|DST_DIR=$EXTRAS|" \
      "$REPO/configs/S54cfs_module" > "$tmp"
    if install_file "$INIT_DIR/S54cfs_module" "$tmp"; then CHANGED_KLIPPER=1; fi
    run chmod 755 "$INIT_DIR/S54cfs_module"
  fi
  for m in $MODULES; do
    if [ "$(readlink "$EXTRAS/$m" 2>/dev/null || true)" != "$REPO/src/$m" ]; then
      say "link $EXTRAS/$m -> $REPO/src/$m"
      [ -e "$EXTRAS/$m" ] && [ ! -L "$EXTRAS/$m" ] && backup "$EXTRAS/$m"
      run rm -f "$EXTRAS/$m" "$EXTRAS/${m}c"
      run ln -s "$REPO/src/$m" "$EXTRAS/$m"
      CHANGED_KLIPPER=1
    fi
  done
}

copy_configs() {
  run mkdir -p "$CONFIG/macros" "$CONFIG/cfs"
  for f in $CFGS; do
    dest=$CONFIG/macros/$f tmp=$(mktemp)
    # lane_data_file follows this printer's config folder
    sed "s|^lane_data_file: .*|lane_data_file: $CONFIG/cfs/cfs_lanes.json|" \
      "$REPO/configs/$f" > "$tmp"
    if [ "$KEEP" = 1 ] && [ -f "$dest" ] && ! cmp -s "$dest" "$tmp"; then
      say "keep your edited $dest (new version not copied: --keep-config)"
      rm -f "$tmp"; continue
    fi
    if install_file "$dest" "$tmp"; then CHANGED_KLIPPER=1; fi
  done
}

check_position_max() {
  pmax=$(awk '
    /^\[/ { in_y = ($0 ~ /^\[stepper_y\]/) }
    in_y && /^position_max[ \t]*[:=]/ { sub(/^[^:=]*[:=][ \t]*/, ""); sub(/[ \t#;].*/, ""); v = $0 }
    END { print v }' "$PRINTER_CFG")
  if [ -n "$pmax" ] && awk -v p="$pmax" 'BEGIN { exit !(p < 306) }'; then
    say "WARNING: [stepper_y] position_max is $pmax; the cut presses at Y306, set it to 307.5"
  fi
}

restart_services() {
  [ "$RESTART" = 1 ] && [ "$DRY" = 0 ] || return 0
  [ "$CHANGED_KLIPPER" = 1 ] || [ "$CHANGED_MOONRAKER" = 1 ] || { say "nothing changed, no restart"; return 0; }
  if command -v curl >/dev/null 2>&1 && curl -sf -m 5 "$MOONRAKER_URL/server/info" >/dev/null; then
    if [ "$CHANGED_MOONRAKER" = 1 ]; then
      say "restart Moonraker"
      curl -sf -m 20 -X POST "$MOONRAKER_URL/machine/services/restart?service=moonraker" >/dev/null || true
      i=0; while [ $i -lt 30 ] && ! curl -sf -m 5 "$MOONRAKER_URL/server/info" >/dev/null; do sleep 2; i=$((i + 1)); done
    fi
    say "restart Klipper"
    curl -sf -m 20 -X POST "$MOONRAKER_URL/machine/services/restart?service=klipper" >/dev/null || true
    i=0
    while [ $i -lt 40 ]; do
      sleep 3; i=$((i + 1))
      state=$(curl -s -m 5 "$MOONRAKER_URL/printer/info" | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')
      [ "$state" = ready ] || [ "$state" = error ] && break
    done
    say "Klipper: ${state:-unknown}"
    if [ "$UNINSTALL" = 0 ] && [ "$state" = ready ]; then
      if curl -s -m 5 "$MOONRAKER_URL/printer/objects/query?creality_cfs=is_connected" | grep -q '"is_connected": *true'; then
        say "CFS box connected"
      else
        say "CFS box not connected yet: check the cable and CFS_STATUS"
      fi
    fi
  else
    say "Moonraker not reachable: restart Moonraker and Klipper yourself"
  fi
}

if [ "$UNINSTALL" = 1 ]; then
  for m in $MODULES; do
    if [ -L "$EXTRAS/$m" ]; then say "unlink $EXTRAS/$m"; run rm -f "$EXTRAS/$m" "$EXTRAS/${m}c"; CHANGED_KLIPPER=1; fi
  done
  if [ "$MODE" = simpleaf ] && [ -f "$INIT_DIR/S54cfs_module" ]; then
    backup "$INIT_DIR/S54cfs_module"; run rm -f "$INIT_DIR/S54cfs_module"
  fi
  edit_printer_cfg ""
  edit_moonraker_conf ""
  for f in cfs_k1max.cfg cfs_k1max_simpleaf.cfg; do
    if [ -f "$CONFIG/macros/$f" ]; then backup "$CONFIG/macros/$f"; run rm -f "$CONFIG/macros/$f"; fi
  done
  restart_services
  say "uninstalled. Backups: $BACKUP. Slot data kept in $CONFIG/cfs/; delete $REPO yourself."
  exit 0
fi

link_modules
copy_configs
includes=""
for f in $CFGS; do
  includes="${includes:+$includes
}[include macros/$f]"
done
edit_printer_cfg "$includes"
origin=$(git -C "$REPO" remote get-url origin 2>/dev/null || true)
branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
if [ -n "$origin" ] && [ -n "$branch" ] && [ "$branch" != HEAD ]; then
  edit_moonraker_conf "[update_manager creality_cfs]
type: git_repo
path: $REPO
origin: $origin
primary_branch: $branch
managed_services: klipper"
else
  say "not a git checkout on a branch: no Moonraker updater entry"
fi
check_position_max
restart_services
[ -d "$BACKUP" ] && say "backups of the edited files: $BACKUP"
if [ "$MODE" = simpleaf ]; then
  say "SimpleAF regenerates printer.cfg and moonraker.conf on its updates: save your config overrides first"
fi
say "slicer start/change G-code: $REPO/configs/SLICER_GCODE_K1MAX.md"
[ "$MODE" = mainline ] && say "call CFS_PRINT_START/CFS_PRINT_END/CFS_PRINT_CANCEL from your print macros (INSTALL.md)"
say "done"
