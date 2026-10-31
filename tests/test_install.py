# SPDX-License-Identifier: GPL-3.0-or-later
"""install.sh on fake SimpleAF and mainline printer trees."""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("sh") is None,
                                reason="needs git and sh")

PRINTER_CFG = """\
[include homing.cfg]
[include fan_control.cfg]
[include client.cfg]
[include eddyng_macro.cfg]
[include cfs.cfg]

[stepper_y]
position_max: {pmax}

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# [include not_ours.cfg]
"""

MOONRAKER_CONF = """\
[server]
port: 7125

[update_manager mainsail]
type: web

# default k1 webcam config
[include webcam.conf]
"""


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "creality-cfs-klipper"
    (repo / "src").mkdir(parents=True)
    (repo / "configs").mkdir()
    shutil.copy(os.path.join(ROOT, "install.sh"), repo)
    for f in ("creality_cfs.py", "abortable_waits.py"):
        shutil.copy(os.path.join(ROOT, "src", f), repo / "src")
    for f in ("cfs_k1max.cfg", "cfs_k1max_simpleaf.cfg", "S54cfs_module"):
        shutil.copy(os.path.join(ROOT, "configs", f), repo / "configs")
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "-q", "-b", "k1-simpleaf"], check=True)
    subprocess.run(git + ["remote", "add", "origin", "https://example.invalid/cfs.git"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "t"],
                   check=True)
    (tmp_path / "klipper" / "klippy" / "extras").mkdir(parents=True)
    config = tmp_path / "printer_data" / "config"
    config.mkdir(parents=True)
    (config / "printer.cfg").write_text(PRINTER_CFG.format(pmax=307.5))
    (config / "moonraker.conf").write_text(MOONRAKER_CONF)
    (tmp_path / "init.d").mkdir()
    return tmp_path


def run(tree, *args, mode="simpleaf"):
    cmd = ["sh", str(tree / "creality-cfs-klipper" / "install.sh"), "--mode", mode,
           "--klipper-dir", str(tree / "klipper"), "--printer-data", str(tree / "printer_data"),
           "--init-dir", str(tree / "init.d"), "--no-restart", *args]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout


def read(tree, rel):
    return (tree / "printer_data" / "config" / rel).read_text()


def snapshot(tree):
    files = {}
    for base, _, names in os.walk(tree):
        for n in names:
            p = os.path.join(base, n)
            files[p] = os.readlink(p) if os.path.islink(p) else open(p, "rb").read()
    return files


def test_simpleaf_install(tree):
    out = run(tree)
    cfg = read(tree, "printer.cfg")
    assert "[include cfs.cfg]" not in cfg
    assert ("[include eddyng_macro.cfg]\n[include macros/cfs_k1max.cfg]\n"
            "[include macros/cfs_k1max_simpleaf.cfg]\n") in cfg
    assert cfg.index("macros/cfs_k1max.cfg") < cfg.index("SAVE_CONFIG")
    assert "#*# [include not_ours.cfg]" in cfg
    repo = tree / "creality-cfs-klipper"
    for m in ("creality_cfs.py", "abortable_waits.py"):
        assert os.readlink(tree / "klipper" / "klippy" / "extras" / m) == str(repo / "src" / m)
    init = (tree / "init.d" / "S54cfs_module").read_text()
    assert "SRC_DIR=%s/src" % repo in init
    assert os.access(tree / "init.d" / "S54cfs_module", os.X_OK)
    lanes = "lane_data_file: %s/cfs/cfs_lanes.json" % (tree / "printer_data" / "config")
    assert lanes in read(tree, "macros/cfs_k1max.cfg")
    assert (tree / "printer_data" / "config" / "macros" / "cfs_k1max_simpleaf.cfg").exists()
    moon = read(tree, "moonraker.conf")
    assert moon.endswith("[update_manager creality_cfs]\ntype: git_repo\npath: %s\n"
                         "origin: https://example.invalid/cfs.git\nprimary_branch: k1-simpleaf\n"
                         "managed_services: klipper\n" % repo)
    assert "WARNING" not in out


def test_second_run_changes_nothing(tree):
    run(tree)
    before = snapshot(tree)
    run(tree)
    assert snapshot(tree) == before


def test_updater_entry_is_replaced_and_comments_kept(tree):
    conf = tree / "printer_data" / "config" / "moonraker.conf"
    conf.write_text(MOONRAKER_CONF.replace(
        "# default k1", "[update_manager creality_cfs]\ntype: git_repo\npath: /old\n\n# default k1"))
    run(tree)
    moon = conf.read_text()
    assert moon.count("[update_manager creality_cfs]") == 1
    assert "/old" not in moon
    assert "# default k1 webcam config\n[include webcam.conf]" in moon
    # replaced where it was, so a later run does not move it
    assert moon.index("creality_cfs") < moon.index("# default k1 webcam config")


def test_mainline_install(tree):
    run(tree, mode="mainline")
    cfg = read(tree, "printer.cfg")
    assert "[include macros/cfs_k1max.cfg]" in cfg
    assert "simpleaf" not in cfg
    assert not (tree / "init.d" / "S54cfs_module").exists()
    assert not (tree / "printer_data" / "config" / "macros" / "cfs_k1max_simpleaf.cfg").exists()


def test_old_root_layout_moves_to_macros(tree):
    p = tree / "printer_data" / "config" / "printer.cfg"
    p.write_text(p.read_text().replace(
        "[include cfs.cfg]", "[include cfs_k1max.cfg]\n[include cfs_k1max_simpleaf.cfg]"))
    run(tree)
    cfg = p.read_text()
    assert "[include cfs_k1max.cfg]" not in cfg
    assert cfg.count("[include macros/cfs_k1max.cfg]") == 1


def test_dry_run_changes_nothing(tree):
    before = snapshot(tree)
    out = run(tree, "--dry-run")
    assert snapshot(tree) == before
    assert "would run" in out


def test_keep_config_keeps_an_edited_copy(tree):
    run(tree)
    dest = tree / "printer_data" / "config" / "macros" / "cfs_k1max.cfg"
    dest.write_text(dest.read_text() + "\n# mine\n")
    (tree / "creality-cfs-klipper" / "configs" / "cfs_k1max.cfg").write_text("# new version\n")
    run(tree, "--keep-config")
    assert dest.read_text().endswith("# mine\n")


def test_position_max_warning(tree):
    (tree / "printer_data" / "config" / "printer.cfg").write_text(PRINTER_CFG.format(pmax=300))
    assert "position_max is 300" in run(tree)


def test_uninstall_reverts(tree):
    run(tree)
    run(tree, "--uninstall")
    cfg = read(tree, "printer.cfg")
    assert "cfs" not in cfg.split("#*#")[0]
    assert "creality_cfs" not in read(tree, "moonraker.conf")
    assert "# default k1 webcam config" in read(tree, "moonraker.conf")
    assert not os.path.lexists(tree / "klipper" / "klippy" / "extras" / "creality_cfs.py")
    assert not (tree / "init.d" / "S54cfs_module").exists()
    assert not (tree / "printer_data" / "config" / "macros" / "cfs_k1max.cfg").exists()
    assert list((tree / "printer_data" / "cfs-backups").iterdir())


def test_checkout_inside_config_is_refused(tree):
    inside = tree / "printer_data" / "config" / "cfs-module"
    shutil.copytree(tree / "creality-cfs-klipper", inside, symlinks=True)
    res = subprocess.run(["sh", str(inside / "install.sh"), "--mode", "simpleaf",
                          "--klipper-dir", str(tree / "klipper"),
                          "--printer-data", str(tree / "printer_data"), "--no-restart"],
                         capture_output=True, text=True)
    assert res.returncode != 0
    assert "inside the printer config" in res.stderr
