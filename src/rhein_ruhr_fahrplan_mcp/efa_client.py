#!/usr/bin/env python3
"""EFA-Client für die VRR-Echtzeitschnittstelle (efa.vrr.de).

Nur Python-stdlib (urllib) – bewusst kein httpx/aiohttp, damit das
Docker-Image schlank bleibt und ohne Build-Dependencies auskommt.

Operator-Filter: FAHRPLAN_OPERATOR (Default: alle Unternehmen, also
kein Filter). Beispiel: FAHRPLAN_OPERATOR=NIAG.

Verifizierte EFA-Eigenheiten (06.10.2026, Live-Tests):
- outputFormat=rapidJSON → Keys: stopEvents / journeys (nicht departureList/trips)
- Bei mehrdeutigen/freien Namen antwortet der Server mit locations[] und
  leeren stopEvents[] → Client löst erst per DHID/ID auf, nie per freiem Text
- parent_station-Suffix „…_Parent“ wird nicht akzeptiert → DHID nutzen
- itdTripDateTimeDepArr=arr liefert Ankünfte; die JSON-Keys heißen trotzdem
  departureTimePlanned/-Estimated (Semantik über Parameter, nicht Feldname)
- Störungen/Meldungen kommen eingebettet als infos[] in jeder Antwort
- MS_REQUEST (Meldungs-Endpoint) antwortet mit HTTP 400 → nicht nutzen;
  Meldungen stattdessen aus DM-Antworten extrahieren
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

EFA_BASE = "https://efa.vrr.de/standard"  # ohne Trailing-Slash; Endpunkt wird angehängt
UA = "rhein-ruhr-fahrplan-mcp/1.0 (+https://github.com/MarcProe/rhein-ruhr-fahrplan-mcp)"
TIMEOUT = 30

def _operator_from_env() -> Optional[str]:
    """Operator-Filter aus FAHRPLAN_OPERATOR; unaufgelöste Installer-
    Platzhalter (${...}) gelten als nicht gesetzt (vgl. server._env)."""
    raw = os.environ.get("FAHRPLAN_OPERATOR", "")
    if "${" in raw and "}" in raw:
        return None
    raw = raw.strip()
    return raw or None


OPERATOR_FILTER = _operator_from_env()


class EfaError(RuntimeError):
    pass


def _get(endpoint: str, params: dict[str, Any]) -> dict:
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    url = f"{EFA_BASE}/{endpoint}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise EfaError(f"EFA-HTTP-Fehler {e.code} bei {url}") from e
    except urllib.error.URLError as e:
        raise EfaError(f"EFA nicht erreichbar: {e.reason}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise EfaError(f"EFA-Antwort kein JSON (evtl. HTML-Fehlerseite): "
                       f"{body[:200]!r}") from e


def _iso_to_hhmm(iso: Optional[str]) -> Optional[str]:
    """EFA-Zeitstempel (UTC, Z-Suffix) → lokale Uhrzeit HH:MM.

    Der VRR-EFA liefert alle timePlanned/timeEstimated in UTC; angezeigt
    werden sollen lokale Zeiten (Europe/Berlin). Ohne Konvertierung würden
    Nutzer Mehlzeiten/Abfahrten 2h zu früh angezeigt bekommen (Bug-Feedback
    06.10.2026: departure_time schien ignoriert, tatsächlich waren die
    Anzeigen UTC und damit irreführend).
    """
    if not iso or len(iso) < 16:
        return None
    if iso.endswith("Z"):
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            local = dt.astimezone(timezone(timedelta(hours=2)))  # Europe/Berlin CEST
            return local.strftime("%H:%M")
        except ValueError:
            pass
    return f"{iso[11:13]}:{iso[14:16]}"


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", html)
    text = re.sub(r"<[^>]+>", "", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&szlig;", "ß").replace("&uuml;", "ü")
                .replace("&auml;", "ä").replace("&ouml;", "ö")
                .replace("&Uuml;", "Ü").replace("&Auml;", "Ä")
                .replace("&Ouml;", "Ö").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def _delay_minutes(planned: Optional[str], estimated: Optional[str]) -> Optional[int]:
    """Verspätung in Minuten aus zwei ISO-Zeitstempeln (beide UTC, Z-Suffix)."""
    if not planned or not estimated:
        return None
    try:
        p = datetime.fromisoformat(planned.replace("Z", "+00:00"))
        e = datetime.fromisoformat(estimated.replace("Z", "+00:00"))
        return int(round((e - p).total_seconds() / 60))
    except ValueError:
        return None


# ----------------------------------------------------------------- Tools-Daten

def stop_events(stop_ref: str, direction: str = "dep",
                limit: int = 10, operator_filter: Optional[str] = None,
                time_from: Optional[datetime] = None
                ) -> list[dict[str, Any]]:
    """Abfahrten/Ankünfte an einer Haltestelle.

    stop_ref: DHID (de:xxxxx:yyyyy), interne Stop-ID oder exakter Name.
    direction: 'dep' (Abfahrten) oder 'arr' (Ankünfte).
    operator_filter: Operator-Code (z.B. 'NIAG') oder None = alle.
    Rückgabe: Liste normalisierter Events.
    """
    params = {
        "outputFormat": "rapidJSON",
        "type_dm": "stop",
        "name_dm": stop_ref,
        "mode": "direct",
        "useRealtime": "1",
        "limit": str(max(1, min(limit, 50))),
    }
    if direction == "arr":
        params["itdTripDateTimeDepArr"] = "arr"
    if time_from is not None:
        params["itdDateDay"] = f"{time_from.day:02d}"
        params["itdDateMonth"] = f"{time_from.month:02d}"
        params["itdDateYear"] = str(time_from.year)
        params["itdTimeHour"] = f"{time_from.hour:02d}"
        params["itdTimeMinute"] = f"{time_from.minute:02d}"

    data = _get("XML_DM_REQUEST", params)
    events = data.get("stopEvents") or []
    # Mehrdeutigkeit? Dann locations[] mit Vorschlägen, stopEvents leer.
    if not events:
        locs = data.get("locations") or []
        if locs:
            names = [l.get("name") for l in locs[:5]]
            raise EfaError(f"Haltestelle mehrdeutig/nicht gefunden. "
                           f"Vorschläge: {', '.join(filter(None, names))}. "
                           f"Besser DHID oder ID verwenden.")
        return []

    out: list[dict[str, Any]] = []
    for ev in events:
        t = ev.get("transportation") or {}
        operator = (t.get("operator") or {}).get("code")
        if operator_filter and operator != operator_filter:
            continue
        # Semantik: bei Ankünften meinen die departureTime*-Felder die Ankunft
        planned = ev.get("departureTimePlanned")
        estimated = ev.get("departureTimeEstimated")
        is_arr = direction == "arr"
        out.append({
            "line": t.get("number"),
            "direction": (t.get("destination") or {}).get("name"),
            "origin": (t.get("origin") or {}).get("name"),
            "operator": operator,
            "operator_name": (t.get("operator") or {}).get("name"),
            "product": (t.get("product") or {}).get("name"),
            "kind": "arrival" if is_arr else "departure",
            "time_planned": _iso_to_hhmm(planned),
            "time_estimated": _iso_to_hhmm(estimated),
            "is_realtime": bool(ev.get("isRealtimeControlled")),
            "delay_minutes": _delay_minutes(planned, estimated),
            "platform": (ev.get("location") or {}).get("name"),
            "stop_id": ((ev.get("location") or {}).get("parent") or {}).get("id"),
            "trip_id": ((ev.get("transportation") or {}).get("id")),
            "infos": _extract_infos(ev.get("infos")),
        })
    return out


def _journey_duration_min(legs: list[dict]) -> Optional[int]:
    """Gesamtdauer in Minuten: letzte Ankunft minus erste Abfahrt.
    Times sind 'HH:MM' Strings aus _iso_to_hhmm; Tageswechsel möglich."""
    if not legs:
        return None
    dep = legs[0].get("dep_planned")
    arr = legs[-1].get("arr_planned")
    if not dep or not arr:
        return None
    try:
        dh, dm = map(int, dep.split(":"))
        ah, am = map(int, arr.split(":"))
        mins = (ah * 60 + am) - (dh * 60 + dm)
        if mins < 0:  # Mitternachtswechsel
            mins += 24 * 60
        return mins
    except ValueError:
        return None


def _extract_infos(infos: Optional[list[dict]]) -> list[dict]:
    if not infos:
        return []
    out = []
    for i in infos:
        link = (i.get("infoLinks") or [{}])[0]
        out.append({
            "title": link.get("title") or i.get("title"),
            "text": _strip_html(link.get("content") or link.get("additionalText") or ""),
            "url": link.get("url"),
            "valid": i.get("incidentDateTime"),
        })
    return out


# ------------------------------------------------------------------ Verbindung

def connections(origin_ref: str, destination_ref: str,
                time_from: Optional[datetime] = None,
                max_results: int = 5,
                operator_filter: Optional[str] = None) -> list[dict]:
    """Verbindungen zwischen zwei Haltestellen (XML_TRIP_REQUEST2, rapidJSON).

    origin/destination: DHID/ID/exakter Name. Ein Operator-Filter ist hier
    nicht hart durchsetzbar (EFA routet über das Gesamtnetz) → nur weich:
    Verbindungen mit Legs des konfigurierten Operators werden nach vorn sortiert.
    """
    params = {
        "outputFormat": "rapidJSON",
        "language": "de",
        "type_origin": "stop",
        "name_origin": origin_ref,
        "type_destination": "stop",
        "name_destination": destination_ref,
        "calcNumberRequests": str(max(1, min(max_results, 10))),
    }
    if time_from is not None:
        params.update({
            "itdDateDay": f"{time_from.day:02d}",
            "itdDateMonth": f"{time_from.month:02d}",
            "itdDateYear": str(time_from.year),
            "itdTimeHour": f"{time_from.hour:02d}",
            "itdTimeMinute": f"{time_from.minute:02d}",
        })
    data = _get("XML_TRIP_REQUEST2", params)
    journeys = data.get("journeys") or []
    out = []
    for j in journeys:
        legs = j.get("legs") or []
        leg_out = []
        has_operator_leg = False
        for leg in legs:
            t = leg.get("transportation") or {}
            operator = (t.get("operator") or {}).get("code")
            if operator_filter and operator == operator_filter:
                has_operator_leg = True
            origin = leg.get("origin") or {}
            dest = leg.get("destination") or {}
            leg_out.append({
                "mode": (t.get("product") or {}).get("name"),
                "line": t.get("number"),
                "operator": operator,
                "from": origin.get("name"),
                "from_id": origin.get("id"),
                "dep_planned": _iso_to_hhmm(origin.get("departureTimePlanned")),
                "dep_estimated": _iso_to_hhmm(origin.get("departureTimeEstimated")),
                "to": dest.get("name"),
                "to_id": dest.get("id"),
                "arr_planned": _iso_to_hhmm(dest.get("arrivalTimePlanned")),
                "arr_estimated": _iso_to_hhmm(dest.get("arrivalTimeEstimated")),
                "interchanges": leg.get("interchanges"),
                "infos": _extract_infos(leg.get("infos")),
            })
        out.append({
            "interchanges": j.get("interchanges"),
            "legs": leg_out,
            "has_operator_leg": has_operator_leg,
            "duration_min": _journey_duration_min(leg_out),
        })
    # Operator-Verbindungen nach vorn (falls Filter gesetzt)
    out.sort(key=lambda c: (not c["has_operator_leg"],))
    return out


# ------------------------------------------------------------- Haltestellensuche

def find_stops(query: str, limit: int = 10) -> list[dict]:
    """Haltestellensuche per EFA STOPFINDER (für Orte ohne DHID-Index)."""
    params = {
        "outputFormat": "rapidJSON",
        "locationServerActive": "1",
        "type_sf": "stop",
        "name_sf": query,
    }
    data = _get("XML_STOPFINDER_REQUEST", params)
    locs = data.get("locations") or []
    out = []
    for l in locs:
        if l.get("type") != "stop":
            continue
        props = l.get("properties") or {}
        out.append({
            "id": l.get("id"),
            "name": l.get("name"),
            "dhid": l.get("id") if str(l.get("id", "")).startswith("de:") else props.get("stopId"),
            "locality": ((l.get("parent") or {}).get("name")),
            "coord": l.get("coord"),
        })
        if len(out) >= max(1, min(limit, 20)):
            break
    return out
