#!/usr/bin/env python3
"""Root's word on supervision (/etc/ferry/USER.env) beats anything the mind controls.

A mind that could flip itself unsupervised would make its SessionStart hook start
a second proxy that fights the unit for the port (Bastion, 2026-11-03). So
FERRY_SUPERVISED=1 in the root-owned env file must win over preferences.json and
over the process env -- and its absence must change nothing (the control).
"""
import json
import os
import pwd
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
import ferry_config  # noqa: E402

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"  ({ctx})" if ctx else ""))


tmp = Path(tempfile.mkdtemp(prefix="ferry-root-sup-"))
home, cfg, rootdir = tmp / "home", tmp / "cfg", tmp / "etc-ferry"
for d in (home, cfg, rootdir):
    d.mkdir()
os.environ["HOME"] = str(home)
os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
os.environ.pop("FERRY_SUPERVISED", None)
ferry_config.ROOT_ENV_DIR = str(rootdir)
root_env = rootdir / (pwd.getpwuid(os.getuid()).pw_name + ".env")
(home / "preferences.json").write_text(json.dumps({"ferry": {"port": 5799, "supervised": False}}))

v, s = ferry_config.resolve()
check("control: no root file, prefs supervised=false -> unsupervised", v["supervised"] == "0", (v, s))

root_env.write_text("FERRY_SUPERVISED=1\n")
v, s = ferry_config.resolve()
check("root file FERRY_SUPERVISED=1 beats prefs supervised=false", v["supervised"] == "1", v)
check("source is reported as root", s["supervised"] == "root", s)

os.environ["FERRY_SUPERVISED"] = "0"
v, s = ferry_config.resolve()
check("process env FERRY_SUPERVISED=0 cannot override root", v["supervised"] == "1", (v, s))
os.environ.pop("FERRY_SUPERVISED")

root_env.write_text('FERRY_SUPERVISED="1"\n')
check("quoted value is honoured", ferry_config.root_says_supervised())

root_env.write_text("FERRY_SUPERVISED=0\n")
v, s = ferry_config.resolve()
check("root file saying 0 forces nothing (prefs decide)", v["supervised"] == "0" and s["supervised"] != "root", (v, s))

root_env.write_text("# FERRY_SUPERVISED=1\nOTHER=1\n")
check("a commented-out line does not count", not ferry_config.root_says_supervised())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
