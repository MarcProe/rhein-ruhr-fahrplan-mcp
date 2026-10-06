# rhein-ruhr-fahrplan-mcp

[![PyPI](https://img.shields.io/pypi/v/rhein-ruhr-fahrplan-mcp.svg)](https://pypi.org/project/rhein-ruhr-fahrplan-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/rhein-ruhr-fahrplan-mcp.svg)](https://pypi.org/project/rhein-ruhr-fahrplan-mcp/)
[![wheel](https://img.shields.io/pypi/wheel/rhein-ruhr-fahrplan-mcp.svg)](https://pypi.org/project/rhein-ruhr-fahrplan-mcp/)
[![Downloads](https://img.shields.io/pypi/dm/rhein-ruhr-fahrplan-mcp.svg)](https://pypi.org/project/rhein-ruhr-fahrplan-mcp/)
[![MCP](https://img.shields.io/badge/MCP-Model_Context_Protocol-blue)](https://modelcontextprotocol.io)
[![GTFS + EFA](https://img.shields.io/badge/data-GTFS%20%2B%20EFA-9cf)](#architektur)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

MCP-Server für **ÖPNV-Fahrplandaten im Rhein-Ruhr-Gebiet** (Datenbasis: Open Data des Verkehrsverbunds Rhein-Ruhr). Auf PyPI: <https://pypi.org/project/rhein-ruhr-fahrplan-mcp/>

Zwei Datenpfeiler, beide keyless:

| Pfeiler | Quelle | Inhalt |
|---|---|---|
| **Sollfahrplan** (optional) | VRR-GTFS (Open Data ÖPNV, CKAN-API) → SQLite | Nur die konfigurierten Verkehrsunternehmen (`FAHRPLAN_AGENCY_IDS`); z. B. NIAG: 123 Routen, 6.975 Fahrten, 3.869 Haltestellen |
| **Echtzeit** (immer aktiv) | VRR-EFA `efa.vrr.de` | Live-Abfahrten/-Ankünfte mit Prognose, Verspätungen, Steige, Störungshinweise – im gesamten Verbundgebiet |

**Default = Live-Only:** Ohne Konfiguration startet der Server sofort und liefert alle Live-Tools über die EFA-Schnittstelle. GTFS-Tools (Sollplan, Haltestellen-Index) melden dann einen klaren Hinweis mit Einrichtungsanleitung – bewusst so, weil ein ungefilterter Import die komplette Datenbank (~500 MB, ~7 Mio. Stop-Times) anlegen würde.

## Tools (9)

| Tool | Quelle | Zweck |
|---|---|---|
| `next_departures` | EFA (live) | Live-Abfahrten mit Echtzeitprognose |
| `next_arrivals` | EFA (live) | Live-Ankünfte mit Echtzeitprognose |
| `plan_connection` | EFA (live) | Verbindungen von A nach B im Gesamtnetz |
| `line_disruptions` | EFA (live) | Aktuelle Störungen/Baustellen/Umleitungen |
| `find_stop` | GTFS → EFA-Fallback | Haltestelle finden (DHID auflösen) |
| `stop_schedule` | GTFS | Sollfahrplan (auch morgen/übermorgen) |
| `sync_status` | GTFS | Feed-Status: Version, Gültigkeit, Zeilenzahlen |
| `update_feed` | GTFS | Feed aktualisieren (nur bei neuer Ressource – idempotent) |
| `get_default_start` | GTFS → EFA | Konfigurierte Standard-Start-Haltestelle (`FAHRPLAN_DEFAULT_START`) auflösen |

## Schnellstart

### Variante A: uvx (stdio, ohne Docker)

```bash
uvx rhein-ruhr-fahrplan-mcp
```

Oder dauerhaft installiert:

```bash
uv tool install rhein-ruhr-fahrplan-mcp
rhein-ruhr-fahrplan-mcp
```

- **Startet sofort** (Live-Only). Für den GTFS-Sollplan einmalig die Umgebungsvariablen setzen (siehe unten) und `update_feed` aufrufen bzw. Server neu starten – der Import läuft dann automatisch im Hintergrund (~1–2 min, 39 MB Download).
- **Datenbank-Pfad fest:** `~/.local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db` (XDG, cwd-unabhängig; via `FAHRPLAN_DB_PATH` überschreibbar).

mcphub/Client-Registrierung (stdio-Typ):

```json
{
  "fahrplan-mcp": {
    "type": "stdio",
    "command": "uvx",
    "args": ["rhein-ruhr-fahrplan-mcp"],
    "env": {"FAHRPLAN_AGENCY_IDS": "nia-25"}
  }
}
```

(`env` optional – ohne Agencies reiner Live-Only-Betrieb.)

### Variante B: Docker (Streamable HTTP)

```bash
docker compose up -d --build
```

Im Hub als Streamable-HTTP-Server registrieren (Netz-Anbindung via `compose.override.yaml`, siehe [Deployment](#deployment)):

```
http://fahrplan-mcp:8080/mcp
```

## Konfiguration: Verkehrsunternehmen (FAHRPLAN_AGENCY_IDS)

Der GTFS-Import filtert auf die agency_ids des VRR-Feeds. Setze eine oder mehrere:

```bash
# z. B. nur NIAG (Kreise Kleve & Wesel):
export FAHRPLAN_AGENCY_IDS=nia-25

# oder mehrere Unternehmen:
export FAHRPLAN_AGENCY_IDS=nia-25,swk-02,dvg-20
```

in `compose.yaml`:

```yaml
    environment:
      - FAHRPLAN_AGENCY_IDS=nia-25
      # - FAHRPLAN_OPERATOR=NIAG   # optional: Live-Abfahrten auch filtern
```

Ohne `FAHRPLAN_AGENCY_IDS`:
- Live-Tools (next_departures, next_arrivals, plan_connection, line_disruptions) funktionieren **normal** (ganzes Gebiet, alle Unternehmen)
- GTFS-Tools (find_stop mit Index, stop_schedule, sync_status) und der Auto-Import antworten mit einem **Konfigurationshinweis** statt zu importieren

## Verkehrsunternehmen (Auswahl)

Die agency_ids stammen aus dem VRR-GTFS-Feed (`agency.txt`, Stand August 2026). Häufige Unternehmen:

| agency_id | Unternehmen | Gebiet/Angebot |
|---|---|---|
| `nia-25` | Niederrheinische Verkehrsbetriebe | Bus, Kreise Kleve & Wesel |
| `dvg-20` | Duisburger Verkehrsgesellschaft | Bus/Tram Duisburg |
| `swk-02` | SWK MOBIL | Krefeld |
| `new-80` | NEW mobil und aktiv M'gladbach | Mönchengladbach |
| `vvg-65` | NEW mobil und aktiv Viersen | Viersen |
| `btm-70`, `rbg-70` | Rheinbahn | Düsseldorf |
| `bgs-00`, `bgs-30`–`bgs-34` | BOGESTRA | Bochum/Gelsenkirchen |
| `btm-13`, `eva-10`–`eva-12` | Ruhrbahn | Essen/Mülheim |
| `dsw-36`–`dsw-39` | DSW21 | Dortmund |
| `sto-15`, `btm-15`, `eva-15` | STOAG | Oberhausen |
| `wsw-16`, `wsw-66` | WSW mobil | Wuppertal |
| `hst-00`, `hst-50` | Hagener Straßenbahn | Hagen |
| `sws-18` | Stadtwerke Solingen | Solingen |
| `swr-16` | Stadtwerke Remscheid | Remscheid |
| `ver-45` | VER | Ennepe-Ruhr-Kreis |
| `ves-40` | Vestische | Recklinghausen/Marl |
| `bvr-88` | BVR Busverkehr Rheinland | Rheinland-Bus |
| `bsm-76` | Bahnen der Stadt Monheim | Monheim |
| `hcr-35` | Straßenbahn Herne-Castrop | Herne/Castrop |
| `swn-60` | Stadtwerke Neuss | Neuss |
| `ddb-8003` | DB Regio NRW | Regionalzug NRW |
| `ddb-NX` | National Express | Regionalzug Ruhrgebiet |
| `ddb-RR` | RheinRuhrBahn | Regionalzug |
| `ddb-R2` | eurobahn | Regionalzug |
| `ddb-W3` | WestfalenBahn | Regionalzug |
| `ddb-N2` | NordWestBahn | Regionalzug |
| `ddb-M2` | REGIOBAHN | Regionalzug |

Rail-Freight-/Charter-Einträge im Feed (Train Charter Services, Train Rental International, Ulmer Eisenbahnfreunde u. a.) sind für den Personenverkehr nicht relevant.

**Komplette Liste:** `agency.txt` aus dem aktuellen Feed (Download via CKAN-API, siehe unten) – alle 58 IDs mit Namen. Ein Blick per Python:

```bash
python3 -c "import csv; [print(r['agency_id'], '|', r['agency_name'])
             for r in csv.DictReader(open('agency.txt'))]"
```

## Architektur

```
┌─────────────────────── Server (stdio | http) ─────────────────────────┐
│                                                                        │
│  FastMCP                                                               │
│    ├── Live: EFA-Client (urllib, keyless)  ← immer aktiv                │
│    │     efa.vrr.de/standard/XML_DM_REQUEST (rapidJSON)               │
│    │     XML_TRIP_REQUEST2, XML_STOPFINDER_REQUEST                    │
│    └── Soll: GTFS-SQLite (nur mit FAHRPLAN_AGENCY_IDS) ← Auto-Update  │
│          CKAN-API (alle 6 h) → bei Neuheit: atomarer Import            │
└────────────────────────────────────────────────────────────────────────┘
```

### Eigenschaften

- **Live-Only-Default:** Ohne Konfiguration voll nutzbar (Live-Tools); GTFS-Teil schaltet sich mit `FAHRPLAN_AGENCY_IDS` dazu.
- **Selbst-aktualisierend** (mit Agencies): Daemon-Thread prüft alle `FAHRPLAN_FEED_CHECK_INTERVAL` Sekunden (Default 6 h) die CKAN-API; Import nur bei neuer Ressource – atomar per `.tmp` + `os.replace()`. Initial- und Auto-Import laufen über dasselbe Lock (kein Doppel-Import).
- **Operator-Filter für Live-Daten:** `FAHRPLAN_OPERATOR` (z. B. `NIAG`) filtert next_departures/next_arrivals; plan_connection sortiert passend bevorzugt. Leer = alle Unternehmen.
- **Keine API-Keys, keine Accounts.** VRR-Open-Data-Nutzungsbedingungen: Test-/Hobby-/Entwicklungsnutzung erlaubt; bei dauerhafter öffentlicher Nutzung Mail an openvrr@vrr.de.

## Deployment (Docker)

Der HTTP-Modus lauscht nur im Container-Netz auf `:8080/mcp` (kein Host-Port). Für die Anbindung an einen MCP-Hub in einem bestehenden Docker-Netz eine lokale Datei `compose.override.yaml` neben `compose.yaml` anlegen:

```yaml
services:
  fahrplan-mcp:
    networks:
      - mein-hub-netz   # Name des Netzes, in dem der Hub läuft

networks:
  mein-hub-netz:
    external: true
```

`docker compose up -d` merged das Override automatisch. Die Override-Datei ist umgebungsspezifisch und gehört nicht ins Repository (bereits in `.gitignore`).

### Konfiguration (Env)

| Variable | Default | Bedeutung |
|---|---|---|
| `FAHRPLAN_AGENCY_IDS` | *(leer)* | Kommaseparierte GTFS-Agency-IDs für den Sollplan-Import. **Leer = Live-Only**; GTFS-Tools antworten mit Konfigurationshinweis |
| `FAHRPLAN_DEFAULT_START` | *(leer)* | Standard-Start-Haltestelle (DHID, EFA-ID oder eindeutiger Name – passende IDs liefert `find_stop`); abrufbar via `get_default_start` |
| `FAHRPLAN_OPERATOR` | *(leer)* | Live-Operator-Filter (z. B. `NIAG`); leer = alle Unternehmen |
| `FAHRPLAN_DB_PATH` | `~/.local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db` | SQLite-DB-Pfad (Dockerfile: `/data/fahrplan.db`) |
| `FAHRPLAN_MCP_TRANSPORT` | `stdio` | `stdio` oder `http` (Dockerfile setzt `http`) |
| `FAHRPLAN_MCP_HOST` / `FAHRPLAN_MCP_PORT` | `0.0.0.0:8080` (Dockerfile) | nur http-Modus |
| `FAHRPLAN_FEED_CHECK_INTERVAL` | `21600` (6 h) | CKAN-Prüfung; `0` deaktiviert Auto-Update |

## Verifizierte EFA-Eigenheiten (Pitfalls)

- `outputFormat=rapidJSON`: Response-Keys heißen `stopEvents`/`journeys` (nicht `departureList`/`trips`).
- Freie Haltestellennamen können mehrdeutig sein → Antwort mit `locations[]`-Vorschlägen und leeren Events. Deshalb: IMMER per DHID/ID auflösen.
- `…_Parent`-Suffix aus GTFS `parent_station` wird von EFA nicht akzeptiert → DHID ohne Suffix.
- Bei Ankünften (`itdTripDateTimeDepArr=arr`) heißen die Zeitfelder trotzdem `departureTimePlanned/-Estimated`.
- Der EFA liefert alle Zeiten mit `Z`-Suffix (UTC) – der Client konvertiert sie nach Europe/Berlin, bevor er sie ausgibt (früher zeigten die Tools versehentlich UTC, was wie ein ignorierter `departure_time`-Parameter aussah; gefixt in v1.2.1).
- Verspätungen werden als UTC-Differenz berechnet – zeitzonenunabhängig korrekt.
- MS_REQUEST (Meldungs-Endpoint) liefert HTTP 400 → Störungen aus den eingebetteten `infos[]` der DM-Antworten extrahieren.
- `calcNumberRequests` greift in rapidJSON nicht zuverlässig → `plan_connection` kann mehr Verbindungen liefern als `max_results` (Ergebnisse korrekt, nur Begrenzung weich).

## GTFS-Feed-Details

- CKAN-Dataset: `496eea5d-d6ef-4dc2-aeb0-d15c4fbf3178` („Soll-Fahrplandaten VRR“), API: `https://opendata.ruhr/api/3/action/package_show?id=…`
- Ressourcen-UUID wechselt monatlich → Import ermittelt sie immer frisch per CKAN-API.
- Filter: `agency_id IN (FAHRPLAN_AGENCY_IDS)`; transitiv referenzierende Zeilen bleiben erhalten.
- `stop_times.txt` des Gesamtfeeds (~7 Mio. Zeilen) wird **gestreamt** (erste Listen-Version wurde OOM-killed).
- Ungefilterter Gesamtfeed wäre ~500 MB Datenbank – deshalb bewusst kein Default-Import.

## Entwicklung

```bash
git clone … && cd rhein-ruhr-fahrplan-mcp
uv venv && source .venv/bin/activate
uv pip install -e .
rhein-ruhr-fahrplan-mcp                   # stdio starten (Live-Only)
FAHRPLAN_AGENCY_IDS=nia-25 rhein-ruhr-fahrplan-mcp   # mit GTFS-Sollplan
python3 tests/stdio_e2e.py $(command -v rhein-ruhr-fahrplan-mcp)      # stdio-E2E
FAHRPLAN_AGENCY_IDS=nia-25 python3 tests/stdio_fullcycle.py $(command -v rhein-ruhr-fahrplan-mcp)  # inkl. Autoimport
```

Releases: Version in `pyproject.toml` hochziehen → Commit → `git tag vX.Y.Z && git push origin vX.Y.Z` – die GitHub-Action published automatisch nach PyPI (Trusted Publishing).
