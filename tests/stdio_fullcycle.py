#!/usr/bin/env python3
"""Voller stdio-Zyklus MIT konfiguriertem Verkehrsunternehmen:

    FAHRPLAN_AGENCY_IDS=nia-25

- Autoimport im Hintergrund abwarten
- find_stop + stop_schedule gegen echte DB
- update_feed idempotent
- sync_status zeigt agency_ids

Aufruf: FAHRPLAN_AGENCY_IDS=<ids> python3 tests/stdio_fullcycle.py <server-binary>
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AGENCIES = os.environ.get("FAHRPLAN_AGENCY_IDS", "")
assert AGENCIES, "Dieser Test braucht FAHRPLAN_AGENCY_IDS (z.B. nia-25)"
DB = Path(os.environ.get("FAHRPLAN_DB_PATH",
                         Path.home() / ".local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db"))
if DB.exists():
    DB.unlink()

env = dict(os.environ, FAHRPLAN_AGENCY_IDS=AGENCIES)
if "FAHRPLAN_DB_PATH" not in env:
    env["FAHRPLAN_DB_PATH"] = str(DB)

proc = subprocess.Popen(
    [sys.argv[1]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, bufsize=1, env=env,
)

def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()

def request(id_, method, params=None):
    send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("stdout geschlossen")
        msg = json.loads(line)
        if msg.get("id") == id_:
            return msg

request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "e2e", "version": "0.1"}})
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

print(f"Warte auf Hintergrund-Import (Agencies: {AGENCIES}) …")
t0 = time.time()
while not DB.exists():
    assert time.time() - t0 < 300, "Import dauerte > 5 min"
    time.sleep(3)
time.sleep(2)
print(f"✓ DB nach {time.time()-t0:.0f}s da: {DB.stat().st_size/1e6:.1f} MB")

resp = request(2, "tools/call", {"name": "find_stop",
                                 "arguments": {"query": "Moers Bahnhof", "limit": 2}})
content = json.loads(resp["result"]["content"][0]["text"])
stops = content["stops"]
print(f"✓ find_stop: {len(stops)} Treffer, z.B. {stops[0]['stop_name']} (dhid {stops[0]['dhid']})")

resp = request(3, "tools/call", {"name": "stop_schedule",
                                 "arguments": {"stop_ref": "de:05170:36308", "limit": 3}})
content = json.loads(resp["result"]["content"][0]["text"])
deps = content["departures"]
print(f"✓ stop_schedule: {len(deps)} Soll-Abfahrten, erste: "
      f"{deps[0]['time']} Linie {deps[0]['line']} → {deps[0]['direction']}")

resp = request(4, "tools/call", {"name": "update_feed", "arguments": {}})
content = json.loads(resp["result"]["content"][0]["text"])
assert content["ok"] and content["updated"] is False, f"unerwartet: {content}"
print("✓ update_feed: Feed aktuell – kein Re-Import (idempotent)")

resp = request(5, "tools/call", {"name": "sync_status", "arguments": {}})
content = json.loads(resp["result"]["content"][0]["text"])
meta = content.get("meta", {})
print(f"✓ sync_status: agency_ids = {meta.get('agency_ids')}, "
      f"routes = {content.get('counts', {}).get('routes')}")

proc.terminate()
print("\n=== VOLLER ZYKLUS (stdio + Autoimport) BESTANDEN ===")
