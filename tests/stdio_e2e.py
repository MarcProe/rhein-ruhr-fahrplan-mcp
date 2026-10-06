#!/usr/bin/env python3
"""stdio-End-to-End-Test: fährt echten MCP-Handshake über stdin/stdout.

Szenario 1 (Default, Live-Only): keine FAHRPLAN_AGENCY_IDS gesetzt.
  - Live-Tools funktionieren (EFA)
  - GTFS-Tools liefern klaren Konfigurationshinweis (kein Crash)
"""
import json
import subprocess
import sys
import time

proc = subprocess.Popen(
    [sys.argv[1]],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)

def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line)

def request(id_, method, params=None):
    send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    while True:
        msg = recv()
        if msg is None:
            raise RuntimeError("Server hat stdout geschlossen")
        if msg.get("id") == id_:
            return msg

# 1) initialize
t0 = time.time()
resp = request(1, "initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "stdio-test", "version": "0.1"},
})
assert "result" in resp, f"initialize fehlgeschlagen: {resp}"
print(f"✓ initialize ({time.time()-t0:.2f}s), server: {resp['result']['serverInfo']['name']}")

# 2) initialized
send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 3) tools/list
resp = request(2, "tools/list")
tools = resp["result"]["tools"]
names = sorted(t["name"] for t in tools)
print(f"✓ tools/list: {len(tools)} Tools: {', '.join(names)}")
assert len(tools) == 8, "8 Tools erwartet"

# 4) Live-Tool (Default, ohne DB): next_departures – muss funktionieren
resp = request(3, "tools/call", {
    "name": "next_departures",
    "arguments": {"stop_ref": "de:05170:36308", "limit": 3},
})
content = json.loads(resp["result"]["content"][0]["text"])
deps = content.get("departures", [])
assert content.get("ok") and deps, f"Live-Tool ohne DB fehlgeschlagen: {content}"
print(f"✓ next_departures (Live-Only-Default): {len(deps)} Abfahrten, erste: "
      f"{deps[0]['line']} → {deps[0]['direction']}")

# 5) GTFS-Tool ohne Konfiguration: find_stop → KLARER Konfigurationshinweis
resp = request(4, "tools/call", {
    "name": "find_stop",
    "arguments": {"query": "Moers Bahnhof", "limit": 1},
})
payload = json.loads(resp["result"]["content"][0]["text"])
assert payload.get("ok") is False, "find_stop ohne Konfiguration sollte ok=false"
err = payload.get("error", "")
assert "FAHRPLAN_AGENCY_IDS" in err, f"Hinweis fehlt: {err[:200]}"
assert "README" in err, f"README-Verweis fehlt: {err[:200]}"
print(f"✓ find_stop ohne Konfiguration: klarer Hinweis "
      f"(FAHRPLAN_AGENCY_IDS + README-Verweis)")

# 6) update_feed ohne Konfiguration: ebenfalls klaren Hinweis, kein Import
resp = request(5, "tools/call", {"name": "update_feed", "arguments": {}})
payload = json.loads(resp["result"]["content"][0]["text"])
assert payload.get("ok") is False, "update_feed ohne Konfiguration sollte ok=false"
assert "FAHRPLAN_AGENCY_IDS" in payload.get("error", ""), "Hinweis fehlt"
print("✓ update_feed ohne Konfiguration: kein Import, Hinweis statt 500 MB")

# 7) stop_schedule ohne Konfiguration: Hinweis
resp = request(6, "tools/call", {
    "name": "stop_schedule",
    "arguments": {"stop_ref": "de:05170:36308", "limit": 3},
})
payload = json.loads(resp["result"]["content"][0]["text"])
assert payload.get("ok") is False and "FAHRPLAN_AGENCY_IDS" in payload.get("error", "")
print("✓ stop_schedule ohne Konfiguration: klarer Hinweis")

# 8) stdout-Kanal darf NUR Protokoll enthalten – stderr separat prüfen
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
err = proc.stderr.read()
assert "INFO" in err, "Erwartet: Logs auf stderr"
print("✓ Logging sauber auf stderr, stdout nur MCP-Protokoll")

print("\n=== ALLE stdio-TESTS BESTANDEN (Live-Only-Default) ===")
