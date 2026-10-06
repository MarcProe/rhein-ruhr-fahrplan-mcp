#!/usr/bin/env python3
"""Docker-Healthcheck: prüft DB-Existenz und beantwortet einen einfachen
MCP-Ping über den HTTP-Endpoint (initialize-Request).

Ausführbar als Modul: python -m rhein_ruhr_fahrplan_mcp.healthcheck
"""

import json
import os
import sys
import urllib.request

PORT = int(os.environ.get("FAHRPLAN_MCP_PORT", "8080"))
DB = os.environ.get("FAHRPLAN_DB_PATH",
                     os.path.join(os.path.expanduser("~"),
                                  ".local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db"))

# 1) DB vorhanden? (Ohne konfigurierte Agencies ist das OK – Live-Only-Betrieb)
db_missing = not os.path.exists(DB)
if db_missing:
    print(f"HEALTHCHECK NOTE: DB fehlt ({DB}) – Live-Only-Betrieb "
          "(FAHRPLAN_AGENCY_IDS nicht gesetzt)")

# 2) MCP-HTTP-Endpoint antwortet? (Streamable-HTTP: initialize)
req = urllib.request.Request(
    f"http://127.0.0.1:{PORT}/mcp",
    data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "healthcheck", "version": "0.1"},
        },
    }).encode(),
    headers={"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status in (200, 202):
            print("HEALTHCHECK OK")
            sys.exit(0)
        print(f"HEALTHCHECK FAIL: HTTP {r.status}")
        sys.exit(1)
except Exception as e:
    print(f"HEALTHCHECK FAIL: {e}")
    sys.exit(1)
