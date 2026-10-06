#!/usr/bin/env python3
"""E2E-Test für get_default_start: 4 Fälle über echten stdio-Handshake.

Aufruf: python3 tests/test_default_start.py /pfad/zum/rhein-ruhr-fahrplan-mcp
"""
import json
import os
import subprocess
import sys


def run(binary, env_extra, expect_desc):
    env = {k: v for k, v in os.environ.items()
           if k not in ("FAHRPLAN_AGENCY_IDS", "FAHRPLAN_DEFAULT_START",
                        "FAHRPLAN_DB_PATH", "FAHRPLAN_OPERATOR")}
    env.update(env_extra)
    proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)

    def req(id_, method, params=None):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method,
                                     "params": params or {}}) + "\n")
        proc.stdin.flush()
        while True:
            m = json.loads(proc.stdout.readline())
            if m.get("id") == id_:
                return m

    req(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "t", "version": "0.1"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                  "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    tools = req(2, "tools/list")["result"]["tools"]
    assert any(t["name"] == "get_default_start" for t in tools), "Tool fehlt in tools/list!"
    r = json.loads(req(3, "tools/call", {"name": "get_default_start",
                                         "arguments": {}})
                  ["result"]["content"][0]["text"])
    print(f"[{expect_desc}] ok={r.get('ok')} set={r.get('set')} "
          f"dhid={r.get('dhid')} name={r.get('name')} source={r.get('source')}")
    proc.terminate()
    return r


# Fall 1: ohne Env → klarer Hinweis
r1 = run(sys.argv[1], {}, "Fall 1: ohne Env")
assert r1["ok"] is False and r1["set"] is False, r1
assert "FAHRPLAN_DEFAULT_START" in r1["error"], r1

# Fall 2: DHID → Auflösung (GTFS-Index fehlt hier → EFA-Fallback)
r2 = run(sys.argv[1], {"FAHRPLAN_DEFAULT_START": "de:05170:36308"},
         "Fall 2: DHID Moers Bahnhof")
assert r2["ok"] and r2["set"], r2
assert "36308" in (r2["dhid"] or ""), r2
assert "Bahnhof" in (r2["name"] or ""), r2

# Fall 3: EFA-ID → Auflösung
r3 = run(sys.argv[1], {"FAHRPLAN_DEFAULT_START": "20036308"},
         "Fall 3: EFA-ID Moers Bahnhof")
assert r3["ok"] and r3["set"] and r3["name"] and "36308" in (r3["dhid"] or ""), r3

# Fall 4: ungültige DHID → Fehler mit find_stop-Tipp
r4 = run(sys.argv[1], {"FAHRPLAN_DEFAULT_START": "de:99:99999"},
         "Fall 4: ungültige DHID")
assert r4["ok"] is False and "find_stop" in r4["error"], r4

print("\n=== ALLE 4 FÄLLE BESTANDEN ===")
