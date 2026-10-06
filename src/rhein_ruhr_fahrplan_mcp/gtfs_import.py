#!/usr/bin/env python3
"""GTFS-Import (VRR-Feed) → gefilterte SQLite-Datenbank.

Quelle: Open-Data-ÖPNV-CKAN (Dataset 496eea5d-…, „Soll-Fahrplandaten VRR“).

Design (Speicher): Der VRR-Gesamtfeed hat ~7 Mio. stop_times-Zeilen.
Deshalb wird stop_times GESTREAMT (Zeile für Zeile, Batch-Insert),
nie vollständig materialisiert. Alles andere ist klein genug für RAM.

Verhalten:
- CKAN-API package_show → neueste ZIP-Ressource (sortiert nach `created`).
- Feed-ZIP laden, auf die konfigurierten Agencies reduzieren.
- SQLite-Schema mit Indizes; Import idempotent (DB wird neu aufgebaut).
- FAHRPLAN_AGENCY_IDS (kommasepariert) MUSS gesetzt sein – ein Import
  ohne Filter würde ~500 MB DB erzeugen und ist bewusst nicht der Default.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

log = logging.getLogger("gtfs-import")

CKAN_PACKAGE_SHOW = (
    "https://opendata.ruhr/api/3/action/package_show"
    "?id=496eea5d-d6ef-4dc2-aeb0-d15c4fbf3178"
)
UA = "rhein-ruhr-fahrplan-mcp/1.0 (+https://github.com/MarcProe/rhein-ruhr-fahrplan-mcp)"

SCHEMA = """
DROP TABLE IF EXISTS feed_meta;
CREATE TABLE feed_meta (key TEXT PRIMARY KEY, value TEXT);
DROP TABLE IF EXISTS agency;
CREATE TABLE agency (
  agency_id TEXT PRIMARY KEY,
  agency_name TEXT, agency_url TEXT, agency_timezone TEXT,
  agency_lang TEXT, agency_phone TEXT, agency_fare_url TEXT, agency_email TEXT
);
DROP TABLE IF EXISTS stops;
CREATE TABLE stops (
  stop_id TEXT PRIMARY KEY,
  stop_name TEXT NOT NULL,
  stop_code TEXT,
  stop_lat REAL, stop_lon REAL,
  location_type TEXT, parent_station TEXT, platform_code TEXT,
  dhid TEXT
);
CREATE INDEX idx_stops_name ON stops (stop_name);
CREATE INDEX idx_stops_dhid ON stops (dhid);
CREATE INDEX idx_stops_parent ON stops (parent_station);
DROP TABLE IF EXISTS routes;
CREATE TABLE routes (
  route_id TEXT PRIMARY KEY,
  agency_id TEXT NOT NULL,
  route_short_name TEXT, route_long_name TEXT,
  route_type INTEGER, route_color TEXT, route_text_color TEXT
);
CREATE INDEX idx_routes_agency ON routes (agency_id);
DROP TABLE IF EXISTS trips;
CREATE TABLE trips (
  trip_id TEXT PRIMARY KEY,
  route_id TEXT NOT NULL,
  service_id TEXT NOT NULL,
  trip_headsign TEXT,
  direction_id INTEGER
);
CREATE INDEX idx_trips_route ON trips (route_id);
CREATE INDEX idx_trips_service ON trips (service_id);
DROP TABLE IF EXISTS stop_times;
CREATE TABLE stop_times (
  trip_id TEXT NOT NULL,
  arrival_time TEXT, departure_time TEXT,
  stop_id TEXT NOT NULL,
  stop_sequence INTEGER,
  pickup_type INTEGER, drop_off_type INTEGER,
  PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX idx_st_times_stop ON stop_times (stop_id);
CREATE INDEX idx_st_times_trip ON stop_times (trip_id);
DROP TABLE IF EXISTS calendar;
CREATE TABLE calendar (
  service_id TEXT PRIMARY KEY,
  monday INTEGER, tuesday INTEGER, wednesday INTEGER,
  thursday INTEGER, friday INTEGER, saturday INTEGER, sunday INTEGER,
  start_date TEXT, end_date TEXT
);
DROP TABLE IF EXISTS calendar_dates;
CREATE TABLE calendar_dates (
  service_id TEXT NOT NULL,
  date TEXT NOT NULL,
  exception_type INTEGER,
  PRIMARY KEY (service_id, date)
);
DROP TABLE IF EXISTS transfers;
CREATE TABLE transfers (
  from_stop_id TEXT, to_stop_id TEXT,
  transfer_type TEXT, min_transfer_time TEXT,
  from_trip_id TEXT, to_trip_id TEXT, from_route_id TEXT, to_route_id TEXT
);
"""


# ---------------------------------------------------------------- CKAN + Download

def latest_feed_url() -> tuple[str, str]:
    """Neueste VRR-GTFS-ZIP-Ressource via CKAN-API. Returns (url, created)."""
    req = Request(CKAN_PACKAGE_SHOW, headers={"User-Agent": UA})
    with urlopen(req, timeout=60) as r:
        data = json.load(r)
    if not data.get("success"):
        raise RuntimeError(f"CKAN-API nicht erfolgreich: {data.get('error')}")
    resources = data["result"]["resources"]
    zips = [r for r in resources if str(r.get("url", "")).endswith(".zip")]
    if not zips:
        raise RuntimeError("Keine ZIP-Ressourcen im CKAN-Paket gefunden")
    latest = max(zips, key=lambda r: r.get("created") or "")
    return latest["url"], latest.get("created", "")


def download(url: str, dest: Path, timeout: int = 600) -> int:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return dest.stat().st_size


# ---------------------------------------------------------------- Filter-Konfig

def agency_ids_from_env() -> set[str]:
    """FAHRPLAN_AGENCY_IDS parsen (kommasepariert). Leer = Fehler.

    Ein Import des ungefilterten Gesamtfeeds würde ~500 MB erzeugen und
    ist bewusst nicht als Default vorgesehen. Die Fehlermeldung erklärt
    die Konfiguration.
    """
    raw = os.environ.get("FAHRPLAN_AGENCY_IDS", "").strip()
    ids = {a.strip() for a in raw.split(",") if a.strip()}
    if not ids:
        raise RuntimeError(
            "FAHRPLAN_AGENCY_IDS ist nicht gesetzt – der GTFS-Import braucht "
            "mindestens eine Verkehrsunternehmen-ID (agency_id aus dem VRR-Feed). "
            "Ohne Filter würde der komplette Rhein-Ruhr-Feed (~500 MB Datenbank, "
            "~7 Mio. Stop-Times) importiert, was als Default nicht sinnvoll ist. "
            "Beispiel: FAHRPLAN_AGENCY_IDS=nia-25 (Niederrheinische Verkehrsbetriebe). "
            "Die Liste aller verfügbaren IDs steht im README (Abschnitt "
            "„Verkehrsunternehmen“). Mehrere IDs kommasepariert."
        )
    return ids


# ---------------------------------------------------------------- GTFS-Zugriff

def _open_csv(zf: zipfile.ZipFile, name: str):
    """GTFS-CSV als (fieldnames, iterator) öffnen; fehlt → (None, leer)."""
    try:
        raw = zf.read(f"{name}.txt")
    except KeyError:
        return None, iter(())
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return (reader.fieldnames or []), reader


def read_table(zf: zipfile.ZipFile, name: str) -> list[dict]:
    """Kleine Tabelle vollständig lesen (agency, routes, trips, stops, …)."""
    _, it = _open_csv(zf, name)
    return list(it)


def iter_table(zf: zipfile.ZipFile, name: str):
    """Große Tabelle als Iterator (stop_times) – ohne Materialisierung."""
    _, it = _open_csv(zf, name)
    return it


# ---------------------------------------------------------------- SQLite-Import

def build_db(zf: zipfile.ZipFile, db_path: Path,
             feed_url: str, feed_created: str,
             agency_ids: set[str]) -> dict:
    """Feed aus Zip lesen, Agency-Filter, Streaming-Import. Gibt Statistiken.

    ATOMAR: Es wird in db_path + '.tmp' gebaut und erst nach vollständigem
    Erfolg per os.replace() an die Zielposition verschoben. Ein laufender
    Server sieht dadurch nie eine halbfertige Datenbank."""
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    conn = sqlite3.connect(tmp_path)
    stats: dict[str, int] = {}
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO feed_meta (key, value) VALUES (?, ?)",
            [("feed_url", feed_url), ("feed_created", feed_created),
             ("imported_at", datetime.now().astimezone().isoformat(timespec="seconds")),
             ("agency_ids", ",".join(sorted(agency_ids)))])

        # -- Agency (nur konfigurierte)
        agency = [a for a in read_table(zf, "agency")
                  if a.get("agency_id") in agency_ids]
        if not agency:
            avail = sorted({a.get("agency_id") for a in read_table(zf, "agency")})
            raise RuntimeError(
                f"Keine der konfigurierten Agency-IDs im Feed gefunden "
                f"(gesetzt: {sorted(agency_ids)}). Verfügbar sind z.B.: "
                f"{avail[:15]} … – vollständige Liste im README."
            )
        conn.executemany(
            "INSERT INTO agency VALUES (?,?,?,?,?,?,?,?)",
            [(a.get("agency_id"), a.get("agency_name"), a.get("agency_url"),
              a.get("agency_timezone"), a.get("agency_lang"),
              a.get("agency_phone"), a.get("agency_fare_url"),
              a.get("agency_email")) for a in agency])
        stats["agency"] = len(agency)

        # -- Routes (gefiltert)
        route_ids: set[str] = set()
        kept_routes: list[tuple] = []
        for r in read_table(zf, "routes"):
            if r.get("agency_id") in agency_ids:
                route_ids.add(r["route_id"])
                kept_routes.append((r["route_id"], r.get("agency_id"),
                                    r.get("route_short_name"),
                                    r.get("route_long_name"), r.get("route_type"),
                                    r.get("route_color"), r.get("route_text_color")))
        conn.executemany("INSERT INTO routes VALUES (?,?,?,?,?,?,?)", kept_routes)
        stats["routes"] = len(kept_routes)
        log.info("Routes: %d", len(kept_routes))

        # -- Trips (gefiltert)
        trip_ids: set[str] = set()
        service_ids: set[str] = set()
        kept_trips: list[tuple] = []
        for t in read_table(zf, "trips"):
            if t.get("route_id") in route_ids:
                trip_ids.add(t["trip_id"])
                service_ids.add(t["service_id"])
                kept_trips.append((t["trip_id"], t["route_id"], t["service_id"],
                                   t.get("trip_headsign"), t.get("direction_id")))
        conn.executemany("INSERT INTO trips VALUES (?,?,?,?,?)", kept_trips)
        stats["trips"] = len(kept_trips)
        log.info("Trips: %d", len(kept_trips))

        # -- Stop times (STREAMING, nur gefilterte Trips)
        used_stops: set[str] = set()
        batch: list[tuple] = []
        n_st = 0
        BATCH = 20_000
        insert_st = ("INSERT OR REPLACE INTO stop_times VALUES (?,?,?,?,?,?,?)")
        for st in iter_table(zf, "stop_times"):
            if st.get("trip_id") not in trip_ids:
                continue
            batch.append((st["trip_id"], st.get("arrival_time"),
                          st.get("departure_time"), st["stop_id"],
                          int(st["stop_sequence"]), st.get("pickup_type"),
                          st.get("drop_off_type")))
            used_stops.add(st["stop_id"])
            n_st += 1
            if len(batch) >= BATCH:
                conn.executemany(insert_st, batch)
                batch.clear()
        if batch:
            conn.executemany(insert_st, batch)
        stats["stop_times"] = n_st
        stats["used_stops"] = len(used_stops)
        log.info("Stop times: %d | benutzte Haltestellen: %d", n_st, len(used_stops))

        # -- Stops: benutzte + deren Parent (für Suchindex/Anzeigen)
        stops_keep = set(used_stops)
        kept_stops: list[tuple] = []
        for s in read_table(zf, "stops"):
            sid = s["stop_id"]
            parent = s.get("parent_station") or ""
            if sid in used_stops or (parent and parent in used_stops):
                if sid not in stops_keep:
                    stops_keep.add(sid)
                kept_stops.append((sid, s.get("stop_name"), s.get("stop_code"),
                                   float(s["stop_lat"]), float(s["stop_lon"]),
                                   s.get("location_type"), parent or None,
                                   s.get("platform_code"), s.get("NVBW_HST_DHID")))
        conn.executemany(
            "INSERT OR IGNORE INTO stops VALUES (?,?,?,?,?,?,?,?,?)",
            kept_stops)
        stats["stops"] = len(kept_stops)
        log.info("Stops importiert: %d", len(kept_stops))

        # -- Calendar (nur benutzte Services)
        cal = [c for c in read_table(zf, "calendar")
               if c["service_id"] in service_ids]
        conn.executemany(
            "INSERT OR REPLACE INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(c["service_id"], c.get("monday"), c.get("tuesday"),
              c.get("wednesday"), c.get("thursday"), c.get("friday"),
              c.get("saturday"), c.get("sunday"),
              c.get("start_date"), c.get("end_date")) for c in cal])
        stats["calendar"] = len(cal)

        # -- Calendar dates (Ausnahmen für benutzte Services)
        cd = [c for c in read_table(zf, "calendar_dates")
              if c.get("service_id") in service_ids]
        conn.executemany(
            "INSERT OR REPLACE INTO calendar_dates VALUES (?,?,?)",
            [(c["service_id"], c["date"], c.get("exception_type")) for c in cd])
        stats["calendar_dates"] = len(cd)

        # -- Transfers (nur zwischen benutzten Stops)
        tr = [t for t in read_table(zf, "transfers")
              if t.get("from_stop_id") in used_stops
              and t.get("to_stop_id") in used_stops]
        conn.executemany(
            "INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?)",
            [(t.get("from_stop_id"), t.get("to_stop_id"), t.get("transfer_type"),
              t.get("min_transfer_time"), t.get("from_trip_id"),
              t.get("to_trip_id"), t.get("from_route_id"),
              t.get("to_route_id")) for t in tr])
        stats["transfers"] = len(tr)

        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()
    # Atomarer Tausch gegen die Live-DB
    os.replace(tmp_path, db_path)
    return stats


def main(agency_ids: set[str] | None = None) -> int:
    """Import ausführen. agency_ids: vorvalidierte IDs (z.B. vom Server-Lock-Pfad);
    None → aus FAHRPLAN_AGENCY_IDS lesen (RuntimeError bei leerer Konfig)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if agency_ids is None:
        agency_ids = agency_ids_from_env()  # RuntimeError bei leerer Konfig
    db_path = Path(os.environ.get("FAHRPLAN_DB_PATH",
                                  Path.home() / ".local/share/rhein-ruhr-fahrplan-mcp/fahrplan.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("Ermittle neueste VRR-GTFS-Ressource via CKAN-API …")
    feed_url, feed_created = latest_feed_url()
    log.info("Ressource: %s (erstellt %s)", feed_url, feed_created)

    with tempfile.TemporaryDirectory(prefix="fahrplan-gtfs-") as td:
        zpath = Path(td) / "gtfs.zip"
        size = download(feed_url, zpath)
        log.info("Feed geladen: %.1f MB", size / 1e6)
        with zipfile.ZipFile(zpath) as zf:
            stats = build_db(zf, db_path, feed_url, feed_created, agency_ids)

    print(json.dumps({
        "ok": True,
        "db": str(db_path),
        "feed_created": feed_created,
        "agency_ids": sorted(agency_ids),
        **{f"n_{k}": v for k, v in stats.items()},
    }, ensure_ascii=False))
    return 0


# Kompatibilität mit server.py (vorvalidierte IDs aus dem Lock-Pfad)
def main_with_agencies(agency_ids: set[str]) -> int:
    return main(agency_ids=agency_ids)


if __name__ == "__main__":
    sys.exit(main())
