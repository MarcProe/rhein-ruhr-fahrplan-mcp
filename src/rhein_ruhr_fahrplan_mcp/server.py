#!/usr/bin/env python3
"""Model-Context-Protocol-Server für Fahrplandaten im Rhein-Ruhr-Gebiet.

Zwei Datenpfeiler (beide live verifiziert am 06.10.2026):
1. GTFS-Sollfahrplan (VRR-OpenData-Feed, gefiltert auf FAHRPLAN_AGENCY_IDS) → SQLite
2. VRR-EFA-Echtzeit (efa.vrr.de, keyless) → Live-Abfahrten mit Prognosen

Konfiguration (Env):
- FAHRPLAN_AGENCY_IDS  kommaseparierte GTFS-Agency-IDs (PFLICHT für GTFS-Import;
                       leerer Default → GTFS-Tools melden klaren Fehler mit Anleitung;
                       vollständige Liste im README unter „Verkehrsunternehmen“)
- FAHRPLAN_DEFAULT_START  optionale Default-Start-Haltestelle (DHID, EFA-ID oder
                       Name); abrufbar über das Tool get_default_start
- FAHRPLAN_OPERATOR   optionaler Live-Operator-Filter (z.B. NIAG); leer = alle
- FAHRPLAN_DB_PATH    Default ~/.local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db
- FAHRPLAN_MCP_TRANSPORT  stdio (Default) | http
- FAHRPLAN_MCP_HOST/PORT  nur http-Modus (Dockerfile: 0.0.0.0:8080)
- FAHRPLAN_FEED_CHECK_INTERVAL  CKAN-Prüfung in s (Default 21600 = 6 h; 0 = aus)

Tools:
- find_stop / next_departures / next_arrivals / stop_schedule
- plan_connection / line_disruptions / sync_status / update_feed
- get_default_start
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .efa_client import EfaError, OPERATOR_FILTER
from . import efa_client, gtfs_import
from fastmcp import FastMCP

# stdio-Modus: Loggen NUR nach stderr, nie stdout (MCP-Protokoll nutzt stdout)
if os.environ.get("FAHRPLAN_MCP_TRANSPORT", "stdio") == "stdio":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
else:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

log = logging.getLogger("fahrplan-mcp")

DB_PATH = Path(os.environ.get("FAHRPLAN_DB_PATH",
                              Path.home() / ".local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db"))


def _env(name: str) -> str:
    """Env-Wert lesen; unaufgelöste Platzhalter (${...}) gelten als nicht gesetzt.

    Installer wie mcpm hinterlassen beim Auslassen optionaler Argumente den
    Literal-Platzhalter (z.B. '${FAHRPLAN_AGENCY_IDS}') in der Konfiguration.
    Der Server behandelt solche Werte wie 'nicht gesetzt', damit der
    Live-Only-Default erhalten bleibt (kein Import-Versuch mit Müll-IDs,
    kein toter Operator-Filter, kein Platzhalter als Default-Start).
    """
    raw = os.environ.get(name, "")
    if "${" in raw and "}" in raw:
        return ""
    return raw.strip()


AGENCY_IDS_RAW = _env("FAHRPLAN_AGENCY_IDS")
AGENCY_IDS = {a.strip() for a in AGENCY_IDS_RAW.split(",") if a.strip()}

DEFAULT_START_RAW = _env("FAHRPLAN_DEFAULT_START")
# Lazy-Cache: erfolgreiche Auflösung von FAHRPLAN_DEFAULT_START (dict oder None)
_DEFAULT_START_CACHE: Optional[dict] = None

AGENCY_HINT = (
    "Keine Verkehrsunternehmen konfiguriert: Die Umgebungsvariable "
    "FAHRPLAN_AGENCY_IDS ist nicht gesetzt. Ohne sie würde der Import den "
    "kompletten Rhein-Ruhr-Feed laden (~500 MB Datenbank), was bewusst nicht "
    "der Default ist. Beispiel: FAHRPLAN_AGENCY_IDS=nia-25. Die vollständige "
    "Liste aller Agency-IDs steht im README (Abschnitt Verkehrsunternehmen). "
    "Mehrere IDs kommasepariert (z.B. nia-25,swk-18,dvg-20)."
)

mcp = FastMCP(
    "rhein-ruhr-fahrplan-mcp",
    instructions=(
        "Fahrplan- und Echtzeitdaten für den ÖPNV im Rhein-Ruhr-Gebiet "
        "(Datenbasis: VRR-OpenData). "
        "Haltestellen immer zuerst mit find_stop auflösen, dann die zurückgegebene "
        "dhid für Live-Abfahrten verwenden. "
        "Soll-Abfahrten: stop_schedule. Live-Abfahrten: next_departures. "
        "Konfigurierter Standard-Start: get_default_start."
    ),
)

# ----------------------------------------------------------------- DB-Helfer

def _db_error(e: Exception) -> dict:
    """Einheitliche Fehlerantwort für DB-abhängige Tools."""
    if not AGENCY_IDS:
        return {"ok": False, "error": AGENCY_HINT}
    if isinstance(e, RuntimeError) and "nicht gefunden" in str(e):
        return {"ok": False,
                "error": f"GTFS-Datenbank nicht gefunden ({DB_PATH}). "
                         "Der initiale Import läuft ggf. noch (Hintergrund) – "
                         "update_feed ausführen oder kurz warten."}
    return {"ok": False, "error": str(e)}


def _conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        if not AGENCY_IDS:
            raise RuntimeError(AGENCY_HINT)
        raise RuntimeError(
            f"GTFS-Datenbank nicht gefunden ({DB_PATH}). "
            "Der initiale Import läuft ggf. noch (Hintergrund) – "
            "update_feed ausführen oder kurz warten."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _service_active_on(d: date) -> tuple[str, list]:
    """WHERE-Klausel für aktive Service-IDs an Datum d + Parameter."""
    ymd = d.strftime("%Y%m%d")
    cols = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    wd_col = cols[d.weekday()]
    sql_where = (
        f"(t.service_id IN (SELECT service_id FROM calendar "
        f"WHERE {wd_col}=1 AND start_date<=? AND end_date>=?) "
        f"OR t.service_id IN (SELECT service_id FROM calendar_dates "
        f"WHERE date=? AND exception_type=1)) "
        f"AND t.service_id NOT IN (SELECT service_id FROM calendar_dates "
        f"WHERE date=? AND exception_type=2)"
    )
    params = [ymd, ymd, ymd, ymd]
    return sql_where, params


def _normalize_time(hhmmss: Optional[str]) -> Optional[str]:
    if not hhmmss:
        return None
    try:
        h, m, s = hhmmss.split(":")
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        return hhmmss


def _local_now() -> datetime:
    return datetime.now().astimezone()


# ------------------------------------------------------------------ MCP-Tools

@mcp.tool
def find_stop(query: str, limit: int = 10) -> dict:
    """Haltestelle im Rhein-Ruhr-Gebiet finden (GTFS-Index; Live-Fallback EFA).

    Liefert stop_id, Name, DHID (globale ID), Koordinaten
    und ggf. Steig-Details. Der GTFS-Index enthält nur Haltestellen der
    konfigurierten Verkehrsunternehmen (FAHRPLAN_AGENCY_IDS); unbekannte
    Haltestellen werden live über die EFA gesucht.
    """
    like = f"%{query.replace('*', '%').strip()}%"
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT s.stop_id, s.stop_name, s.dhid, s.stop_lat, s.stop_lon, "
                "       s.parent_station, s.platform_code, s.location_type "
                "FROM stops s "
                "WHERE s.stop_name LIKE ? COLLATE NOCASE "
                "ORDER BY s.stop_name LIMIT ?",
                (like, max(1, min(limit, 30)))).fetchall()
            if not rows:
                # Fallback: EFA-STOPFINDER (falls GTFS-Index sie nicht kennt)
                try:
                    efa_stops = efa_client.find_stops(query, limit=limit)
                    return {"source": "efa", "stops": efa_stops}
                except EfaError as e:
                    return {"source": "gtfs", "stops": [], "error": str(e)}
            return {"source": "gtfs", "stops": _rows_to_dicts(rows)}
    except (RuntimeError, sqlite3.Error) as e:
        return _db_error(e)


@mcp.tool
def get_default_start() -> dict:
    """Konfigurierte Standard-Start-Haltestelle abrufen (FAHRPLAN_DEFAULT_START).

    Löst die konfigurierte Angabe (DHID, EFA-ID oder Name) auf und liefert
    die kanonische DHID plus Name.
    Die aufgelöste Haltestelle kann direkt für next_departures, stop_schedule
    oder als origin in plan_connection verwendet werden.
    Ohne gesetzte FAHRPLAN_DEFAULT_START: Hinweis mit Anleitung.
    """
    global _DEFAULT_START_CACHE
    if not DEFAULT_START_RAW:
        return {"ok": False, "set": False,
                "error": "FAHRPLAN_DEFAULT_START ist nicht gesetzt. Die "
                         "Umgebungsvariable erwartet eine Haltestellen-ID "
                         "(DHID oder EFA-ID; alternativ ein eindeutiger "
                         "Name). Passende IDs liefert find_stop."}
    if _DEFAULT_START_CACHE:
        return {"ok": True, "set": True, **_DEFAULT_START_CACHE}

    # 1) GTFS-Index (falls DB verfügbar)
    ref = None
    try:
        ref = _resolve_stop(DEFAULT_START_RAW)
    except (RuntimeError, sqlite3.Error):
        ref = None  # DB fehlt/Agency ungefiltert → EFA-Fallback
    if ref:
        resolved = {
            "ref": DEFAULT_START_RAW,
            "stop_id": ref["stop_id"],
            "dhid": ref.get("dhid") or ref["stop_id"],
            "name": ref["stop_name"],
            "source": "gtfs",
        }
    else:
        # 2) EFA-STOPFINDER (löst DHID, EFA-ID und Namen auf)
        try:
            stops = efa_client.find_stops(DEFAULT_START_RAW, limit=1)
        except EfaError as e:
            return {"ok": False, "set": True, "ref": DEFAULT_START_RAW,
                    "error": f"Auflösung fehlgeschlagen: {e}"}
        if not stops:
            return {"ok": False, "set": True, "ref": DEFAULT_START_RAW,
                    "error": f"Haltestelle nicht gefunden: {DEFAULT_START_RAW!r}. "
                             "Mit find_stop nach der korrekten ID suchen und "
                             "FAHRPLAN_DEFAULT_START entsprechend setzen."}
        s = stops[0]
        resolved = {
            "ref": DEFAULT_START_RAW,
            "stop_id": s.get("id"),
            "dhid": s.get("dhid") or s.get("id"),
            "name": s.get("name"),
            "source": "efa",
        }
    _DEFAULT_START_CACHE = resolved
    return {"ok": True, "set": True, **resolved}


@mcp.tool
def next_departures(stop_ref: str, limit: int = 10) -> dict:
    """Live-Abfahrten einer Haltestelle mit Echtzeitprognose.

    stop_ref: DHID (empfohlen), stop_id oder exakter Name.
    Liefert Linie, Richtung, Soll- und Prognosezeit, Verspätung in Minuten,
    Steig und aktuelle Störungshinweise. Mit gesetztem FAHRPLAN_OPERATOR
    werden nur Fahrten dieses Unternehmens geliefert, sonst alle.
    """
    try:
        events = efa_client.stop_events(stop_ref, direction="dep", limit=limit,
                                        operator_filter=OPERATOR_FILTER)
        return {"ok": True, "stop_ref": stop_ref, "departures": events}
    except EfaError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def next_arrivals(stop_ref: str, limit: int = 10) -> dict:
    """Live-Ankünfte einer Haltestelle mit Echtzeitprognose.

    stop_ref: DHID (empfohlen), stop_id oder exakter Name.
    """
    try:
        events = efa_client.stop_events(stop_ref, direction="arr", limit=limit,
                                        operator_filter=OPERATOR_FILTER)
        return {"ok": True, "stop_ref": stop_ref, "arrivals": events}
    except EfaError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def stop_schedule(stop_ref: str, limit: int = 10,
                  day_offset: int = 0) -> dict:
    """Sollfahrplan aus dem GTFS-Feed: nächste geplante Abfahrten.

    stop_ref: DHID oder stop_id aus der Datenbank. day_offset: 0=heute,
    1=morgen, -1=gestern. Nutzt den lokalen Feed (monatlich aktualisiert),
    keine Echtzeit – für Prognosen next_departures verwenden.
    """
    try:
        ref = _resolve_stop(stop_ref)
        if not ref:
            return {"ok": False, "error": f"Haltestelle nicht gefunden: {stop_ref}"}
        stop_id = ref["stop_id"]
        d = _local_now().date() + timedelta(days=day_offset)
        where, params = _service_active_on(d)
        now_hm = _local_now().strftime("%H:%M")
        with _conn() as conn:
            rows = conn.execute(
                f"SELECT st.departure_time, r.route_short_name AS line, "
                f"       (SELECT s2.stop_name FROM stop_times st2 "
                f"        JOIN stops s2 ON s2.stop_id = st2.stop_id "
                f"        WHERE st2.trip_id = t.trip_id "
                f"        ORDER BY st2.stop_sequence DESC LIMIT 1) AS direction, "
                f"       s.stop_name AS platform, t.trip_id, r.route_id "
                f"FROM stop_times st "
                f"JOIN trips t ON t.trip_id = st.trip_id "
                f"JOIN routes r ON r.route_id = t.route_id "
                f"JOIN stops s ON s.stop_id = st.stop_id "
                f"WHERE (st.stop_id = ? OR s.parent_station = ?) AND {where} "
                f"  AND substr(st.departure_time, 1, 5) >= ? "
                f"ORDER BY st.departure_time LIMIT ?",
                (stop_id, ref.get("parent_station") or stop_id, *params,
                 now_hm if day_offset == 0 else "00:00",
                 max(1, min(limit, 50)))).fetchall()
        # Anzeigename: bei DHID-Level „Bstg X“-Suffix entfernen
        display_name = ref["stop_name"]
        if stop_ref.startswith("de:") and " Bstg " in display_name:
            display_name = display_name.rsplit(" Bstg ", 1)[0]
        departures = [{
            "time": _normalize_time(r["departure_time"]),
            "line": r["line"],
            "direction": r["direction"],
            "platform": r["platform"],
        } for r in rows]
        return {"ok": True, "stop": display_name, "date": d.isoformat(),
                "departures": departures}
    except (RuntimeError, sqlite3.Error) as e:
        return _db_error(e)


@mcp.tool
def plan_connection(origin_ref: str, destination_ref: str,
                    departure_time: Optional[str] = None,
                    max_results: int = 5) -> dict:
    """Verbindung von A nach B planen (EFA TripRequest, ganzes Rhein-Ruhr-Gebiet).

    origin/destination: DHID oder exakter Haltestellenname.
    departure_time optional 'YYYY-MM-DD HH:MM'. Mit gesetztem
    FAHRPLAN_OPERATOR werden Verbindungen mit Legs dieses Unternehmens
    bevorzugt (hart filtern kann EFA nicht).
    """
    t = None
    if departure_time:
        try:
            t = datetime.strptime(departure_time, "%Y-%m-%d %H:%M")
        except ValueError:
            return {"ok": False,
                    "error": "departure_time muss 'YYYY-MM-DD HH:MM' sein"}
    try:
        conns = efa_client.connections(origin_ref, destination_ref,
                                        time_from=t, max_results=max_results,
                                        operator_filter=OPERATOR_FILTER)
        return {"ok": True, "connections": conns}
    except EfaError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool
def line_disruptions(stop_ref: str, line: Optional[str] = None) -> dict:
    """Aktuelle Störungen/Hinweise an einer Haltestelle (und optional Linie).

    Nutzt die in den Live-Abfahrten eingebetteten EFA-infos (Bauarbeiten,
    Umleitungen, Haltestellenverlegungen). line schränkt optional ein.
    """
    try:
        events = efa_client.stop_events(stop_ref, direction="dep", limit=30,
                                        operator_filter=None)
    except EfaError as e:
        return {"ok": False, "error": str(e)}
    found: dict[tuple, dict] = {}
    for ev in events:
        if line and ev.get("line") != line:
            continue
        for info in ev.get("infos") or []:
            key = (ev.get("line"), info.get("title"))
            if key not in found:
                found[key] = {"line": ev.get("line"), **info}
    return {"ok": True, "disruptions": list(found.values())}


@mcp.tool
def sync_status() -> dict:
    """Status des GTFS-Imports: Feed-Version, Gültigkeit, Zeilenzahlen."""
    try:
        with _conn() as conn:
            meta = dict(conn.execute("SELECT key, value FROM feed_meta").fetchall())
            counts = dict(conn.execute(
                "SELECT 'routes', COUNT(*) FROM routes "
                "UNION ALL SELECT 'trips', COUNT(*) FROM trips "
                "UNION ALL SELECT 'stops', COUNT(*) FROM stops "
                "UNION ALL SELECT 'stop_times', COUNT(*) FROM stop_times").fetchall())
            validity = conn.execute(
                "SELECT MIN(start_date), MAX(end_date) FROM calendar").fetchone()
            cd_range = conn.execute(
                "SELECT MIN(date), MAX(date) FROM calendar_dates").fetchone()
        all_dates = [x for x in ([validity[0], validity[1], cd_range[0], cd_range[1]]) if x]
        return {"meta": meta, "counts": counts,
                "service_range": [min(all_dates), max(all_dates)] if all_dates else None}
    except (RuntimeError, sqlite3.Error) as e:
        return _db_error(e)


@mcp.tool
def update_feed() -> dict:
    """GTFS-Feed aktualisieren: CKAN prüfen, nur bei neuer Ressource importieren.

    Manueller Trigger des Automatismus (Der Server prüft auch selbst alle
    FAHRPLAN_FEED_CHECK_INTERVAL Sekunden). Idempotent: Bei aktuellem Feed
    passiert nichts. Bei neuer Ressource: Download + Import, ~1–2 Minuten
    (je nach Anzahl konfigurierter Unternehmen).
    """
    try:
        return _check_and_update_feed("update_feed-Tool")
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _resolve_stop(stop_ref: str) -> Optional[dict]:
    """DHID/stop_id/exakter Name → {stop_id, stop_name, dhid, parent_station}.

    Bei DHID (de:xxxxx:yyyyy ohne Steig-Suffix) bevorzugt die zugehörige
    Parent-Station liefern, damit stop_schedule alle Steige abdeckt."""
    with _conn() as conn:
        # 1) Exakter Treffer auf stop_id oder dhid
        row = conn.execute(
            "SELECT stop_id, stop_name, dhid, parent_station, location_type "
            "FROM stops WHERE stop_id = ? OR dhid = ? COLLATE NOCASE LIMIT 1",
            (stop_ref, stop_ref)).fetchone()
        # 2) Bei DHID ohne Steig-Suffix: Parent-Station der zugehörigen Steige
        if row is None and stop_ref.startswith("de:") and stop_ref.count(":") == 2:
            row = conn.execute(
                "SELECT s.stop_id, s.stop_name, s.dhid, s.parent_station, s.location_type "
                "FROM stops s WHERE s.stop_id LIKE ? AND s.location_type = '1' "
                "LIMIT 1", (stop_ref.replace(":", "_") + "_Parent",)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT stop_id, stop_name, dhid, parent_station, location_type "
                    "FROM stops WHERE stop_id LIKE ? LIMIT 1",
                    (stop_ref + ":%",)).fetchone()
        # 3) Exakter Name
        if row is None:
            row = conn.execute(
                "SELECT stop_id, stop_name, dhid, parent_station, location_type "
                "FROM stops WHERE stop_name = ? COLLATE NOCASE LIMIT 1",
                (stop_ref,)).fetchone()
        # 4) Fuzzy
        if row is None:
            row = conn.execute(
                "SELECT stop_id, stop_name, dhid, parent_station, location_type "
                "FROM stops WHERE stop_name LIKE ? COLLATE NOCASE LIMIT 1",
                (f"%{stop_ref}%",)).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------------ Auto-Update

_FEED_LOCK = threading.Lock()


def _current_feed_created() -> Optional[str]:
    """feed_created der aktuell importierten DB (None, wenn keine DB)."""
    if not DB_PATH.exists():
        return None
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT value FROM feed_meta WHERE key='feed_created'").fetchone()
            return row["value"] if row else None
    except sqlite3.Error:
        return None


def _check_and_update_feed(reason: str = "periodisch") -> dict:
    """CKAN prüfen; bei neuer Ressource Import ausführen.

    Thread-sicher (Lock, kein Doppel-Import). Ohne konfigurierte
    Agency-IDs wird NICHT importiert, sondern ein klaren Hinweis geliefert.
    """
    if not AGENCY_IDS:
        log.warning("[%s] Kein GTFS-Import: %s", reason, AGENCY_HINT)
        return {"ok": False, "error": AGENCY_HINT}
    with _FEED_LOCK:
        try:
            latest_url, latest_created = gtfs_import.latest_feed_url()
        except Exception as e:
            log.warning("[%s] CKAN-API nicht erreichbar: %s", reason, e)
            return {"ok": False, "updated": False,
                    "error": f"CKAN-API: {e}"}
        current = _current_feed_created()
        if current is not None and current == latest_created:
            log.info("[%s] Feed aktuell (created %s) – kein Import nötig",
                     reason, current)
            return {"ok": True, "updated": False, "feed_created": current}
        log.info("[%s] Neue GTFS-Ressource: %s (aktiv: %s) – Import läuft",
                 reason, latest_created, current)
        try:
            rc = _run_import()
            return {"ok": rc == 0, "updated": True, "feed_created": latest_created}
        except Exception as e:
            log.warning("[%s] Feed-Import fehlgeschlagen: %s", reason, e)
            return {"ok": False, "updated": False, "error": str(e)}


def _run_import() -> int:
    """Import mit den bereits validierten AGENCY_IDS ausführen."""
    return gtfs_import.main_with_agencies(AGENCY_IDS)


def _feed_checker_loop(interval_s: int) -> None:
    """Daemon-Thread: periodisch CKAN auf neue Ressource prüfen."""
    log.info("Feed-Checker gestartet (Interval %d s)", interval_s)
    while True:
        try:
            _check_and_update_feed("feed-checker")
        except Exception as e:  # nie sterben lassen
            log.warning("Feed-Checker unerwarteter Fehler: %s", e)
        time.sleep(interval_s)


# ------------------------------------------------------------------ Startup

def _autoimport_async_if_empty() -> None:
    """DB fehlt → Import im Hintergrund-Thread (stdio-Modus: nicht blockieren).

    Nur wenn FAHRPLAN_AGENCY_IDS gesetzt ist – sonst würde der Server ohne
    Konfiguration 500 MB importieren. Ohne Konfiguration wird nur geloggt.
    """
    if not DB_PATH.exists():
        if not AGENCY_IDS:
            log.info("Keine GTFS-Datenbank und keine FAHRPLAN_AGENCY_IDS gesetzt – "
                     "GTFS-Tools werden einen Konfigurationshinweis liefern. "
                     "Live-Tools (next_departures etc.) funktionieren ohne DB.")
            return
        log.info("Keine GTFS-Datenbank gefunden – initialer Import (Hintergrund) …")
        threading.Thread(
            target=lambda: _check_and_update_feed("initial-import"),
            daemon=True, name="initial-import").start()


def main() -> None:
    """Entry-Point: 'rhein-ruhr-fahrplan-mcp' bzw. 'python -m rhein_ruhr_fahrplan_mcp.server'."""
    transport = os.environ.get("FAHRPLAN_MCP_TRANSPORT", "stdio")
    host = os.environ.get("FAHRPLAN_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("FAHRPLAN_MCP_PORT", "8080"))
    interval_s = int(os.environ.get("FAHRPLAN_FEED_CHECK_INTERVAL", "21600"))

    if transport == "stdio":
        _autoimport_async_if_empty()
        if interval_s > 0:
            threading.Thread(target=_feed_checker_loop, args=(interval_s,),
                             daemon=True, name="feed-checker").start()
        mcp.run()  # stdio-Default
    else:
        # HTTP-Modus (Container): synchroner First-Run-Import, dann Serve
        if not DB_PATH.exists() and AGENCY_IDS:
            log.info("Keine GTFS-Datenbank gefunden – initialer Import …")
            try:
                _run_import()
            except Exception as e:
                log.warning("Auto-Import fehlgeschlagen: %s – Tools melden das selbst", e)
        if interval_s > 0:
            threading.Thread(target=_feed_checker_loop, args=(interval_s,),
                             daemon=True, name="feed-checker").start()
        mcp.run(transport="http", host=host, port=port, path="/mcp")


if __name__ == "__main__":
    main()
