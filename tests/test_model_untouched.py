#!/usr/bin/env python3
"""The MODEL is never Ferry's business (Lupo, 2026-11-03).

Ferry has one route (ROLLING_CONTEXT_UPSTREAM) and no model setting for the
mind's traffic. Whatever model the client names rides in each request to the
upstream untouched -- so a mind can switch models mid-session (agent-heavy
model -> a vision model) without touching Ferry.

A real proxy in curation=ferry mode, in front of a mock upstream, is sent
requests naming two different models in turn, with a decoy ANTHROPIC_MODEL in
the proxy's env. The mock records what arrived. Trigger/target are tiny so the
curation path (which rewrites the payload) runs too, not only pass-through.

Ports: OS-chosen (port 0), asserted outside every reserved range. The only
processes stopped are the two Popen children this file started.

  python3 tests/test_model_untouched.py
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

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy")
MOCK = os.path.join(HERE, "mock_endpoint.py")
RESERVED = [(5588, 5588), (5610, 5613), (21000, 21005), (21097, 21097),
            (21099, 21099), (22000, 22999)]

passed = failed = 0


def check(desc, cond, ctx=None):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {desc}")
    else:
        failed += 1
        print(f"  FAIL {desc}" + (f"\n       {ctx}" if ctx is not None else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert not any(lo <= port <= hi for lo, hi in RESERVED), f"port {port} is reserved"
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


def post(port, model, msgs):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps({"model": model, "max_tokens": 64, "stream": True,
                         "messages": msgs}).encode(),
        headers={"content-type": "application/json", "x-api-key": "test-key",
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def conversation(pairs, tail):
    msgs = []
    for i in range(pairs):
        msgs.append({"role": "user", "content": f"question {i} " + "x" * 400})
        msgs.append({"role": "assistant", "content": f"answer {i} " + "y" * 400})
    msgs.append({"role": "user", "content": tail})
    return msgs


root = tempfile.mkdtemp(prefix="ferry-model-test-")
home = os.path.join(root, "home")
os.makedirs(os.path.join(home, ".claude"))
log = os.path.join(root, "mock.jsonl")
mock_port, proxy_port = free_port(), free_port()
env = {k: v for k, v in os.environ.items()
       if not k.startswith(("ROLLING_CONTEXT_", "FERRY_", "ANTHROPIC_"))
       and k != "CLAUDE_CONFIG_DIR"}
env.update(HOME=home, USERPROFILE=home,
           ROLLING_CONTEXT_PORT=str(proxy_port),
           ROLLING_CONTEXT_UPSTREAM=f"http://127.0.0.1:{mock_port}",
           ROLLING_CONTEXT_CURATION="ferry",
           FERRY_DATA=os.path.join(root, "ferry-data"),
           ROLLING_CONTEXT_TRIGGER="1000", ROLLING_CONTEXT_TARGET="400",
           ROLLING_CONTEXT_LOG=os.path.join(root, "debug.log"),
           ANTHROPIC_MODEL="decoy-model-from-env")

mock = subprocess.Popen([sys.executable, MOCK, str(mock_port), log],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
proxy = subprocess.Popen([sys.executable, "server.py"], cwd=PROXY, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sent = ["model-alpha-x", "model-beta-y", "model-alpha-x"]
try:
    check("mock upstream listening", wait_port(mock_port))
    check("proxy listening", wait_port(proxy_port))
    convo = conversation(24, "turn 0")
    for i, model in enumerate(sent):
        post(proxy_port, model, convo)
        time.sleep(1.5)
        convo = convo + [{"role": "assistant", "content": f"reply {i}"},
                         {"role": "user", "content": f"turn {i + 1}"}]
finally:
    proxy.terminate()
    mock.terminate()
    proxy.wait(10)
    mock.wait(10)

try:
    rows = [json.loads(l) for l in open(log, encoding="utf-8")]
except OSError:
    rows = []
reqs = [r["detail"] for r in rows if r["kind"] == "request"]
chat = [r for r in reqs if not r["compaction"]]
got = [r["model"] for r in chat]
check(f"upstream received {len(sent)} chat requests", len(chat) == len(sent), got)
check("each request reached upstream naming exactly the model the client sent",
      got == sent, got)
check("the decoy ANTHROPIC_MODEL in the proxy's env never appears",
      all(r["model"] != "decoy-model-from-env" for r in reqs), [r["model"] for r in reqs])
check("curation actually ran (the path that rewrites the payload)",
      any(r["messages"] < 49 for r in chat), [r["messages"] for r in chat])
shutil.rmtree(root, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
