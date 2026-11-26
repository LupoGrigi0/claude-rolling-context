"""One place that knows how to point "home" at a temp directory, on every OS.

Setting HOME alone isolates a test on Linux and macOS. It does NOT on Windows:
os.path.expanduser / Path.home() read USERPROFILE there and ignore HOME, so a
suite that set only HOME was silently reading -- and in test_metrics' case
WRITING -- the real user's ~/preferences.json and ~/ferry-data. Lodestone-8ec9
measured both on Windows 11 / Python 3.14, 2026-11-26.

On POSIX this returns exactly {"HOME": home}: behaviour there is unchanged.
"""
import os


def home_env(home):
    """-> the env entries that make `home` the home directory on this platform."""
    home = str(home)
    env = {"HOME": home}
    if os.name == "nt":
        env["USERPROFILE"] = home
    return env


def home_keys():
    """-> the env var names home_env() sets here (for save/restore)."""
    return ("HOME", "USERPROFILE") if os.name == "nt" else ("HOME",)
