#!/usr/bin/env python3
"""Regression suite for issue #5 — custom endpoints must compact too.

Two halves, no framework needed:

  A. endpoint resolution — server.py and compressor.py must agree on where
     traffic goes, including when the endpoint only exists in settings.json
     (the hook writes it there and does not export it into this process).
  B. live proxy against a mock endpoint — two turns, asserting that background
     compaction reaches the endpoint and that the next turn goes upstream
     materially smaller than the conversation held locally. Run twice: once
     against a permissive endpoint, once against a strict one that rejects
     cache_control / tool_choice, which exercises the flattened fallback.

  python tests/test_custom_endpoint.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import os as _os_log_guard
# Keep the suite OUT of the production debug log: importing server.py
# installs a FileHandler on ~/.claude/rolling-context-debug.log, so every
# test run appended THRASH WARNINGS to the file a real incident is
# diagnosed from. Six of them were sitting above a genuine strike.
_os_log_guard.environ.setdefault("ROLLING_CONTEXT_LOG", "/tmp/.ferry-test-debug.log")

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy")
MOCK = os.path.join(HERE, "mock_endpoint.py")

CUSTOM = "https://api.z.ai/api/anthropic"
ANTHROPIC = "https://api.anthropic.com"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def fake_home(root, settings_env):
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"env": settings_env}, f)
    return home


def clean_env(home, **extra):
    env = dict(os.environ)
    for k in ("ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_SUMMARIZER_URL",
              "ROLLING_CONTEXT_SUMMARIZER_KEY", "ROLLING_CONTEXT_SUMMARIZER_FORMAT",
              "ROLLING_CONTEXT_MODEL", "ROLLING_CONTEXT_PORT"):
        env.pop(k, None)
    env.update(HOME=home, USERPROFILE=home)
    env.update(extra)
    return env


# ---------------------------------------------------------------- part A ----

PROBE = (
    "import json, server, compressor;"
    "print(json.dumps({'upstream': server.UPSTREAM_URL,"
    "'summarizer': compressor.SUMMARIZER_BASE_URL,"
    "'native': compressor.NATIVE_MODE,"
    "'fallback': compressor.NATIVE_FALLBACK}))"
)


def resolution_cases(root):
    cases = [
        ("plain Anthropic user is unaffected",
         {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588"}, {},
         {"upstream": ANTHROPIC, "summarizer": ANTHROPIC, "native": True, "fallback": False}),

        ("custom endpoint chained by the hook reaches the summarizer",
         {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588",
          "ROLLING_CONTEXT_UPSTREAM": CUSTOM}, {},
         {"upstream": CUSTOM, "summarizer": CUSTOM, "native": True, "fallback": True}),

        ("bare custom ANTHROPIC_BASE_URL is picked up",
         {"ANTHROPIC_BASE_URL": CUSTOM}, {},
         {"upstream": CUSTOM, "summarizer": CUSTOM, "native": True, "fallback": True}),

        ("env var still wins over settings.json",
         {"ROLLING_CONTEXT_UPSTREAM": "https://from-settings.example"},
         {"ROLLING_CONTEXT_UPSTREAM": "https://from-env.example"},
         {"upstream": "https://from-env.example", "summarizer": "https://from-env.example",
          "native": True, "fallback": True}),

        ("explicit summarizer override still disables native mode",
         {"ROLLING_CONTEXT_UPSTREAM": CUSTOM},
         {"ROLLING_CONTEXT_SUMMARIZER_URL": ANTHROPIC},
         {"upstream": CUSTOM, "summarizer": ANTHROPIC, "native": False, "fallback": False}),
    ]

    failures = 0
    for name, settings, extra, want in cases:
        home = fake_home(os.path.join(root, "res"), settings)
        out = subprocess.run([sys.executable, "-c", PROBE], cwd=PROXY,
                             env=clean_env(home, **extra),
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        got = json.loads(out.stdout.strip().splitlines()[-1])
        bad = {k: (v, got[k]) for k, v in want.items() if got[k] != v}
        print(("  PASS  " if not bad else "  FAIL  ") + name)
        for k, (want_v, got_v) in bad.items():
            print(f"          {k}: want {want_v!r}, got {got_v!r}")
        failures += bool(bad)
        shutil.rmtree(os.path.join(root, "res"), ignore_errors=True)
    return failures


# ---------------------------------------------------------------- part B ----

def conversation(pairs, tail):
    msgs = []
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"question {i} " + "x" * 400})
        msgs.append({"role": "assistant", "content": f"answer {i} " + "y" * 400})
    msgs.append({"role": "user", "content": tail})
    return msgs


def post(port, msgs):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps({"model": "glm-4.6", "max_tokens": 64,
                         "stream": True, "messages": msgs}).encode(),
        headers={"content-type": "application/json", "x-api-key": "custom-endpoint-key",
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def live_case(root, strict):
    label = "strict endpoint (rejects cache_control/tool_choice)" if strict else "permissive endpoint"
    work = os.path.join(root, "strict" if strict else "plain")
    mock_port, proxy_port = free_port(), free_port()
    log = os.path.join(work, "mock.jsonl")
    os.makedirs(work, exist_ok=True)

    home = fake_home(work, {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{proxy_port}",
                            "ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{mock_port}"})
    env = clean_env(home, ROLLING_CONTEXT_PORT=str(proxy_port),
                    ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400")

    mock = subprocess.Popen([sys.executable, MOCK, str(mock_port), log],
                            env=dict(os.environ, MOCK_STRICT="1" if strict else "0"))
    proxy = subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_port(mock_port), "mock endpoint did not start"
        assert wait_port(proxy_port), "proxy did not start"
        base = conversation(24, "turn one: do the thing")
        post(proxy_port, base)
        time.sleep(6)  # background compaction
        turn2 = base + [{"role": "assistant", "content": "did the thing"},
                        {"role": "user", "content": "turn two: keep going"}]
        post(proxy_port, turn2)
        time.sleep(1)
    finally:
        proxy.terminate()
        mock.terminate()

    reqs = [json.loads(l)["detail"] for l in open(log, encoding="utf-8")
            if json.loads(l)["kind"] == "request"]
    chat = [r for r in reqs if not r["compaction"]]
    compactions = [r for r in reqs if r["compaction"]]
    local_chars = len(json.dumps(turn2))
    sent = chat[-1]["convo_chars"] if chat else None

    checks = [
        ("compaction reached the custom endpoint", bool(compactions)),
        ("next turn carries the rolling summary", bool(chat) and chat[-1]["carries_summary"]),
        ("next turn sent upstream materially smaller",
         sent is not None and sent < local_chars * 0.75),
    ]
    print(f"  {label}")
    for name, ok in checks:
        print(("    PASS  " if ok else "    FAIL  ") + name)
    if sent is not None:
        print(f"          ({sent:,} chars sent vs {local_chars:,} held locally)")
    return sum(1 for _, ok in checks if not ok)


def main():
    root = tempfile.mkdtemp(prefix="rolling-context-test-")
    try:
        print("endpoint resolution")
        failures = resolution_cases(root)
        print("\nlive proxy against a mock endpoint")
        failures += live_case(root, strict=False)
        failures += live_case(root, strict=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
