"""
KEF Gate D Flights — Python Backend
Scrapes kefairport.is/fids and serves Gate D arrivals, departures & Heimavellir as JSON.
"""

import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

app = Flask(__name__, static_folder="static")
CORS(app)

# Cache to avoid hammering the airport site
cache = {"data": None, "timestamp": 0}
CACHE_TTL = 15  # seconds
lock = threading.Lock()

FIDS_URL = "https://www.kefairport.is/fids"
CARGO_DEP_URL = "https://www.kefairport.is/flug/fraktflug/brottfarir"
CARGO_ARR_URL = "https://www.kefairport.is/flug/fraktflug/komur"

# Schengen Area airports/countries — used to EXCLUDE these from cargo tab
SCHENGEN_COUNTRIES = {
    "austria", "belgium", "croatia", "czech republic", "czechia", "denmark",
    "estonia", "finland", "france", "germany", "greece", "hungary", "iceland",
    "italy", "latvia", "liechtenstein", "lithuania", "luxembourg", "malta",
    "netherlands", "norway", "poland", "portugal", "romania", "slovakia",
    "slovenia", "spain", "sweden", "switzerland",
}

# Known Schengen airport codes and city names for matching
SCHENGEN_AIRPORTS = {
    # Major hubs and cargo airports in Schengen
    "ams", "amsterdam", "schiphol",
    "fra", "frankfurt", "hahn",
    "cdg", "paris", "orly",
    "mad", "madrid", "barajas",
    "bcn", "barcelona",
    "muc", "munich", "münchen",
    "cph", "copenhagen", "copenhagen kastrup", "kastrup",
    "osl", "oslo", "gardermoen",
    "arn", "stockholm", "arlanda",
    "hel", "helsinki", "vantaa",
    "bru", "brussels", "bruxelles", "liège", "liege", "lgg",
    "vie", "vienna", "wien",
    "zrh", "zurich", "zürich",
    "lis", "lisbon", "lisboa",
    "ath", "athens",
    "waw", "warsaw", "warszawa",
    "prg", "prague", "praha",
    "bud", "budapest",
    "cgn", "cologne", "köln", "koln",
    "dus", "düsseldorf", "dusseldorf",
    "ham", "hamburg",
    "ber", "berlin",
    "lej", "leipzig",
    "str", "stuttgart",
    "nue", "nuremberg", "nürnberg",
    "mxp", "milan", "milano", "malpensa", "linate",
    "fco", "rome", "roma", "fiumicino",
    "tll", "tallinn",
    "rix", "riga",
    "vno", "vilnius",
    "lju", "ljubljana",
    "zag", "zagreb",
    "bts", "bratislava",
    "mla", "malta", "luqa",
    "lux", "luxembourg",
    "gva", "geneva", "genève",
    "bsl", "basel",
    "got", "gothenburg", "göteborg",
    "bgo", "bergen",
    "svg", "stavanger",
    "trd", "trondheim",
    "tku", "turku",
    "oul", "oulu",
    "rov", "rostock",
    "opo", "porto",
    "agp", "malaga", "málaga",
    "pmi", "palma",
    "ibz", "ibiza",
    "tfs", "tenerife",
    "lpa", "gran canaria", "las palmas",
    "fmm", "memmingen",
    "ein", "eindhoven",
    "bll", "billund",
    "aal", "aalborg",
    "rkv", "reykjavik", "reykjavík",
    "aey", "akureyri",
    "kef", "keflavik", "keflavík",
}


def is_schengen(location):
    """Check if a location string matches a Schengen area airport/city."""
    if not location:
        return False
    loc = location.strip().lower()
    # Direct match
    if loc in SCHENGEN_AIRPORTS:
        return True
    # Check if any known Schengen name is contained in the location
    for name in SCHENGEN_AIRPORTS:
        if len(name) > 2 and name in loc:
            return True
    return False


def extract_location_from_cell(cell):
    """Extract clean destination/origin from a cargo table cell.

    The cell contains nested spans. The destination text lives inside a span
    whose class contains 'destination'. We grab its direct text, ignoring
    nested mobile-only children (flight number, logo, status).
    """
    # Find the span with class containing 'destination' (but not 'destinationDetail')
    dest_spans = cell.find_all("span", class_=lambda c: c and any("destination__" in cls and "destinationDetail" not in cls for cls in (c if isinstance(c, list) else [c])))
    if dest_spans:
        # Get only direct text nodes of the first match
        span = dest_spans[0]
        text = "".join(child.strip() for child in span.children if isinstance(child, str))
        if text:
            return text.strip()

    # Fallback: try the NavLink text span
    text_span = cell.find("span", class_=lambda c: c and any("navLink__text" in cls for cls in (c if isinstance(c, list) else [c])))
    if text_span:
        for child in text_span.children:
            if isinstance(child, str):
                t = child.strip()
                if t:
                    return t

    # Last fallback: get full cell text and clean up
    raw = cell.get_text(strip=True)
    raw = raw.lstrip("→").strip()
    return raw


STATUS_MAP = {
    "ON": "Lent",
    "ATD": "Farin",
    "NoStatus": "Á áætlun",
    "DEP": "Farin",
    "ARR": "Lent",
    "CNL": "Aflýst",
}


def scrape_cargo():
    """Scrape cargo flights from kefairport.is using embedded JSON data.

    The cargo pages embed a __NEXT_DATA__ JSON blob with an 'arrival' boolean
    on each flight, which is the authoritative way to split departures/arrivals.
    Both brottfarir and komur pages return the same data, so we only need one.
    """
    cargo_departures = []
    cargo_arrivals = []

    try:
        resp = requests.get(CARGO_DEP_URL, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; KEFGateDBoard/1.0)"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[WARN] Failed to fetch cargo page: {e}")
        return {"departures": [], "arrivals": []}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract JSON from __NEXT_DATA__ script tag
    flights_json = []
    for script in soup.find_all("script"):
        txt = script.string or ""
        if "flightArrayData" in txt:
            try:
                data = json.loads(txt)
                flights_str = data["props"]["pageProps"]["flightArrayData"]
                flights_json = json.loads(flights_str)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"[WARN] Failed to parse cargo JSON: {e}")
            break

    for f in flights_json:
        destination = f.get("destination", "")

        # Filter Schengen
        if is_schengen(destination):
            continue

        status_code = f.get("status", "")
        status_text = STATUS_MAP.get(status_code, status_code)

        scheduled = f.get("time", "")
        updated = f.get("updatedTime", "")

        flight_data = {
            "flight": f.get("flightNumber", ""),
            "location": destination,
            "scheduled": scheduled,
            "estimated": updated if updated else "",
            "status": status_text,
            "airline": f.get("airline", ""),
            "acType": "",
            "acReg": "",
        }

        if f.get("arrival", False):
            cargo_arrivals.append(flight_data)
        else:
            cargo_departures.append(flight_data)

    return {
        "departures": cargo_departures,
        "arrivals": cargo_arrivals,
    }


def scrape_flights():
    """Scrape KEF airport FIDS page and return Gate D flights."""
    try:
        resp = requests.get(FIDS_URL, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; KEFGateDBoard/1.0)"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch FIDS: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    arrivals = []
    departures = []
    all_departures_raw = []  # All departures before gate filter (for FI filtering)

    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

        is_arrival = "origin" in headers or "sta" in headers or "belt" in headers
        is_departure = "destination" in headers or "std" in headers

        if not is_arrival and not is_departure:
            prev_text = ""
            prev = table.find_previous(["h1", "h2", "h3", "h4", "p", "div", "span"])
            if prev:
                prev_text = prev.get_text(strip=True).lower()
            if "arrival" in prev_text:
                is_arrival = True
            elif "departure" in prev_text:
                is_departure = True
            else:
                continue

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["th", "td"])]
            if len(cells) < len(headers):
                continue

            cell_map = dict(zip(headers, cells))
            gate = cell_map.get("gate", "")
            is_gate_d = bool(re.match(r"^D\d*", gate, re.IGNORECASE))

            if is_arrival and is_gate_d:
                arrivals.append({
                    "flight": cell_map.get("flight", ""),
                    "origin": cell_map.get("origin", ""),
                    "sta": cell_map.get("sta", ""),
                    "eta": cell_map.get("eta", ""),
                    "status": cell_map.get("status", ""),
                    "stand": cell_map.get("stand", ""),
                    "gate": gate,
                    "belt": cell_map.get("belt", ""),
                })
            elif is_departure:
                dep_entry = {
                    "flight": cell_map.get("flight", ""),
                    "destination": cell_map.get("destination", ""),
                    "std": cell_map.get("std", ""),
                    "etd": cell_map.get("etd", ""),
                    "status": cell_map.get("status", ""),
                    "stand": cell_map.get("stand", ""),
                    "gate": gate,
                }
                if is_gate_d:
                    departures.append(dep_entry)
                # Collect all departures for FI filtering
                all_departures_raw.append({**dep_entry, "is_gate_d": is_gate_d})

    # Heimavellir: FI flights at Gate D, split by time
    fi_morning = []
    fi_afternoon = []
    for d in all_departures_raw:
        flight = d.get("flight", "").upper()
        if not flight.startswith("FI"):
            continue
        if not d.get("is_gate_d"):
            continue
        # Parse STD time for filtering
        std = d.get("std", "")
        try:
            hour = int(std.split(":")[0])
        except (ValueError, IndexError):
            continue
        entry = {
            "flight": d["flight"],
            "destination": d["destination"],
            "std": d["std"],
            "etd": d["etd"],
            "status": d["status"],
            "stand": d.get("stand", ""),
            "gate": d["gate"],
        }
        if 6 <= hour <= 12:
            fi_morning.append(entry)
        elif 13 <= hour <= 22:
            fi_afternoon.append(entry)

    # Deduplicate morning flights by flight number, remove departed/airborne
    seen = set()
    deduped_morning = []
    for f in fi_morning:
        num = f["flight"].strip().upper()
        status = f.get("status", "").lower()
        if num in seen:
            continue
        if "departed" in status or "airborne" in status:
            continue
        seen.add(num)
        deduped_morning.append(f)
    fi_morning = deduped_morning

    # Cargo flights (non-Schengen)
    cargo = scrape_cargo()

    return {
        "arrivals": arrivals,
        "departures": departures,
        "heimavellir": {
            "morning": fi_morning,
            "afternoon": fi_afternoon,
        },
        "cargo": cargo,
        "updated": time.strftime("%H:%M:%S %Z"),
    }


def get_cached_flights():
    """Skilar síðustu vistuðu gögnum — sækir ALDREI af neti inni í fyrirspurn.

    Bakgrunnsþráður heldur skyndiminninu fersku, svo vefurinn svarar samstundis
    í stað þess að bíða eftir kefairport.is.
    """
    with lock:
        if cache["data"] is not None:
            return cache["data"]

    # Kaldræsing: fyrsta fyrirspurn bíður eftir fyrstu sókn (mest 20 sek).
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.25)
        with lock:
            if cache["data"] is not None:
                return cache["data"]
    return None


def _flights_worker():
    """Uppfærir flugtöfluna í bakgrunni svo enginn notandi bíði eftir vefsókn."""
    while True:
        try:
            data = scrape_flights()
            if data:
                with lock:
                    cache["data"] = data
                    cache["timestamp"] = time.time()
        except Exception as e:
            print(f"[WARN] flights worker: {e}")
        time.sleep(CACHE_TTL)


threading.Thread(target=_flights_worker, daemon=True).start()


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/flights")
def flights_api():
    data = get_cached_flights()
    if data is None:
        return jsonify({"error": "Failed to fetch flight data"}), 503
    return jsonify(data)


# ---------------------------------------------------------------------------
# Flugsjá — live radar feed relayed from flugumferd.is
# ---------------------------------------------------------------------------

RADAR_WS_URL = "wss://flugumferd.is/api/live"
radar_state = {"aircraft": [], "now": 0, "updated": 0, "status": "starting"}
radar_lock = threading.Lock()


def _radar_worker():
    """Maintain a persistent websocket to flugumferd.is and cache snapshots."""
    import websocket  # websocket-client

    backoff = 2
    while True:
        ws = None
        try:
            with radar_lock:
                radar_state["status"] = "connecting"
            ws = websocket.create_connection(
                RADAR_WS_URL,
                origin="https://flugumferd.is",
                header={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                },
                timeout=30,
            )
            backoff = 2
            with radar_lock:
                radar_state["status"] = "connected"
            while True:
                msg = ws.recv()
                if not msg:
                    break
                try:
                    payload = json.loads(msg)
                except Exception:
                    continue
                if payload.get("type") == "full" and isinstance(
                    payload.get("aircraft"), list
                ):
                    with radar_lock:
                        radar_state["aircraft"] = payload["aircraft"]
                        radar_state["now"] = payload.get("now", time.time())
                        radar_state["updated"] = time.time()
                        radar_state["status"] = "connected"
        except Exception:
            with radar_lock:
                radar_state["status"] = "offline"
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


_radar_thread = threading.Thread(target=_radar_worker, daemon=True)
_radar_thread.start()


@app.route("/api/radar")
def radar_api():
    """Flugsjá: flugumferd.is (Ísland/N-Atlantshaf) + víðari þekja fyrir sýnilega svæðið."""
    with radar_lock:
        age = time.time() - radar_state["updated"] if radar_state["updated"] else None
        local = list(radar_state["aircraft"])
        status = radar_state["status"]

    bbox = _parse_bbox(request.args.get("bbox"))
    wide, wide_info = ([], None)
    if bbox:
        wide, wide_info = _wide_area(bbox)

    seen = set()
    merged = []
    for ac in local:
        k = (ac.get("hex") or ac.get("r") or ac.get("flight") or "").upper()
        if k:
            seen.add(k)
        merged.append(ac)
    added = 0
    for ac in wide:
        k = (ac.get("hex") or ac.get("r") or ac.get("flight") or "").upper()
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(ac)
        added += 1

    return jsonify(
        {
            "aircraft": merged,
            "now": time.time(),
            "age": age,
            "status": status,
            "source": "flugumferd.is",
            "local": len(local),
            "wide": {**(wide_info or {}), "added": added} if wide_info else None,
        }
    )


# ---------------------------------------------------------------------------
# Víð þekja fyrir Flugsjá — adsb.lol í 6°x6° reitum, OpenSky þegar sýn er mjög víð
# ---------------------------------------------------------------------------

CELL_DEG = 6
CELL_RADIUS_NM = 250
MAX_CELLS = 20            # fleiri reitir en þetta -> OpenSky yfirlitsmynd
CELL_FRESH = 12           # sek — reitur telst ferskur
CELL_HOT_TTL = 40         # sek — reitur uppfærður í bakgrunni eftir síðustu beiðni
CELL_WORKER_PERIOD = 2
CELL_MIN_GAP = 1.0        # sek milli fyrirspurna (adsb.fi: 1 req/s)
_cells = {}               # (i, j) -> {"ac": [...], "ts": t, "wanted": t}
_cells_lock = threading.Lock()
_cell_pool = ThreadPoolExecutor(max_workers=2)
_rate_lock = threading.Lock()
_rate_last = [0.0]


def _rate_wait():
    with _rate_lock:
        gap = CELL_MIN_GAP - (time.time() - _rate_last[0])
        if gap > 0:
            time.sleep(gap)
        _rate_last[0] = time.time()

# adsb.fi leyfir 1 fyrirspurn/sek stöðugt; adsb.lol takmarkar mun harðar (420/429) og er varaleið.
_AREA_FEEDS = (
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{r}"),
    ("adsb.lol", "https://api.adsb.lol/v2/point/{lat}/{lon}/{r}"),
)


def _ac_list(payload):
    """adsb.lol notar lykilinn "ac", adsb.fi notar "aircraft"."""
    return (payload.get("ac") or payload.get("aircraft") or []) if isinstance(payload, dict) else []


def _parse_bbox(s):
    try:
        lamin, lomin, lamax, lomax = [float(x) for x in s.split(",")]
    except Exception:
        return None
    lamin, lamax = max(-85.0, min(lamin, lamax)), min(85.0, max(lamin, lamax))
    lomin, lomax = max(-180.0, min(lomin, lomax)), min(180.0, max(lomin, lomax))
    if lamax - lamin <= 0 or lomax - lomin <= 0:
        return None
    return (lamin, lomin, lamax, lomax)


def _cells_for(bbox):
    lamin, lomin, lamax, lomax = bbox
    import math
    i0, i1 = math.floor(lamin / CELL_DEG), math.floor((lamax - 1e-9) / CELL_DEG)
    j0, j1 = math.floor(lomin / CELL_DEG), math.floor((lomax - 1e-9) / CELL_DEG)
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def _fetch_cell(cell):
    i, j = cell
    lat = i * CELL_DEG + CELL_DEG / 2
    lon = j * CELL_DEG + CELL_DEG / 2
    for name, tmpl in _AREA_FEEDS:
        if time.time() < _feed_penalty.get(name, 0):
            continue
        _rate_wait()
        try:
            r = requests.get(
                tmpl.format(lat=f"{lat:.2f}", lon=f"{lon:.2f}", r=CELL_RADIUS_NM),
                timeout=(2, 5),
                headers={"User-Agent": "kef-fids/1.0"},
            )
            if r.status_code != 200:
                print(f"cell {cell} {name}: http {r.status_code}")
                if r.status_code in (420, 429):
                    _feed_penalty[name] = time.time() + 3   # örstutt hvíld, síðan aftur
                elif r.status_code == 403:
                    _feed_penalty[name] = time.time() + 120
                continue
            ac = [a for a in _ac_list(r.json()) if a.get("lat") is not None and a.get("lon") is not None]
            for a in ac:
                a["src"] = name
            with _cells_lock:
                ent = _cells.setdefault(cell, {"ac": [], "ts": 0, "wanted": 0})
                ent["ac"], ent["ts"] = ac, time.time()
            return True
        except requests.exceptions.RequestException as e:
            print(f"cell {cell} {name}: net {e!r}"[:160])
            _feed_penalty[name] = time.time() + 30
        except Exception as e:
            print(f"cell {cell} {name}: {e!r}"[:160])
    return False


def _cell_worker():
    """Heldur reitum sem einhver horfir á ferskum — beiðnir bíða aldrei eftir neti."""
    while True:
        now = time.time()
        with _cells_lock:
            stale = [c for c, e in _cells.items() if now - e["wanted"] < CELL_HOT_TTL and now - e["ts"] > CELL_FRESH - 1]
            for c in [c for c, e in _cells.items() if now - e["wanted"] > 600]:
                _cells.pop(c, None)
        if stale:
            stale.sort(key=lambda c: _cells[c]["ts"])
            list(_cell_pool.map(_fetch_cell, stale))
        time.sleep(CELL_WORKER_PERIOD)


threading.Thread(target=_cell_worker, daemon=True).start()


def _in_bbox(lat, lon, bbox):
    lamin, lomin, lamax, lomax = bbox
    return lamin <= lat <= lamax and lomin <= lon <= lomax


def _wide_area(bbox):
    cells = _cells_for(bbox)
    if len(cells) > MAX_CELLS:
        return _wide_opensky(bbox)

    now = time.time()
    with _cells_lock:
        for c in cells:
            _cells.setdefault(c, {"ac": [], "ts": 0, "wanted": 0})["wanted"] = now
        cold = [c for c in cells if _cells[c]["ts"] == 0]
    if cold:
        # Fyrsta skipti sem horft er á þennan reit: stutt, afmörkuð bið, miðjan fyrst
        ci, cj = (bbox[0] + bbox[2]) / 2 / CELL_DEG, (bbox[1] + bbox[3]) / 2 / CELL_DEG
        cold.sort(key=lambda c: (c[0] + .5 - ci) ** 2 + (c[1] + .5 - cj) ** 2)
        futs = [_cell_pool.submit(_fetch_cell, c) for c in cold]
        deadline = now + 2.5
        for f in futs:
            try:
                f.result(timeout=max(0.05, deadline - time.time()))
            except Exception:
                pass

    out, oldest = [], 0
    with _cells_lock:
        for c in cells:
            e = _cells.get(c)
            if not e or not e["ts"]:
                continue
            oldest = max(oldest, now - e["ts"])
            out.extend(a for a in e["ac"] if _in_bbox(a["lat"], a["lon"], bbox))
    return out, {"mode": "cells", "cells": len(cells), "age": round(oldest)}


def _wide_opensky(bbox):
    """Mjög víð sýn: OpenSky yfirlitsmynd, færð áfram eftir hraða og stefnu (dead reckoning)."""
    import math
    with _opensky_lock:
        snap, ts = _opensky_by_cs, _opensky_ts
    age = time.time() - ts if ts else None
    out = []
    for ac in snap.values():
        lat, lon = ac["lat"], ac["lon"]
        if age and ac.get("gs") and ac.get("track") is not None and ac.get("alt_baro"):
            d_nm = ac["gs"] * min(age, 1200) / 3600.0
            brg = math.radians(ac["track"])
            lat = lat + (d_nm / 60.0) * math.cos(brg)
            lon = lon + (d_nm / 60.0) * math.sin(brg) / max(0.2, math.cos(math.radians(lat)))
        if _in_bbox(lat, lon, bbox):
            out.append({**ac, "lat": lat, "lon": lon, "src": "opensky"})
    total = len(out)
    if total > 1500:  # of stórt svar fyrir vafrann — þær næstu miðju sýnar
        cla, clo = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        k = math.cos(math.radians(cla))
        out.sort(key=lambda a: (a["lat"] - cla) ** 2 + ((a["lon"] - clo) * k) ** 2)
        out = out[:1500]
    return out, {"mode": "opensky", "age": round(age) if age else None, "total": total}


# ---------------------------------------------------------------------------
# Eftirfylgni með stakri flugvél  (IATA flugnúmer -> ADS-B kallmerki)
# ---------------------------------------------------------------------------

# IATA flugfélagskóði -> ICAO kallmerkjaforskeyti (flugfélög sem fljúga um KEF)
AIRLINE_ICAO = {
    "FI": "ICE", "OG": "FPY", "DL": "DAL", "UA": "UAL", "AA": "AAL",
    "BA": "BAW", "LH": "DLH", "SK": "SAS", "DY": "NOZ", "D8": "NSZ",
    "W6": "WZZ", "EW": "EWG", "LX": "SWR", "KL": "KLM", "AF": "AFR",
    "IB": "IBE", "TP": "TAP", "AY": "FIN", "OS": "AUA", "SN": "BEL",
    "EI": "EIN", "TK": "THY", "VY": "VLG", "PC": "PGT", "WK": "EDW",
    "LS": "EXS", "U2": "EZY", "FR": "RYR", "AC": "ACA", "TS": "TSC",
    "WS": "WJA", "JL": "JAL", "NH": "ANA", "LO": "LOT", "AZ": "ITY",
    "A3": "AEE", "SU": "AFL", "TU": "TAR", "MT": "TCX", "BY": "TOM",
    "EJU": "EJU", "N0": "NOZ", "RC": "FLI", "NO": "NOS", "HV": "TRA",
    "6B": "BLX", "QS": "TVS", "X3": "TUI", "DE": "CFG", "ET": "ETH",
    "QR": "QTR", "EK": "UAE", "CX": "CPA", "5X": "UPS", "FX": "FDX",
    "3S": "BOX", "QY": "BCS", "M6": "AJT", "GG": "CVA", "K4": "CKS",
}

_track_cache = {}
_track_lock = threading.Lock()
TRACK_TTL = 8  # sekúndur


def _callsign_candidates(flight):
    """Býr til líkleg ADS-B kallmerki út frá IATA flugnúmeri, t.d. FI672 -> ICE672."""
    m = re.match(r"^\s*([A-Z0-9]{2,3}?)\s*0*(\d{1,4})\s*$", (flight or "").upper())
    if not m:
        return []
    code, num = m.group(1), m.group(2)
    icao = AIRLINE_ICAO.get(code, code)
    out = []
    for pfx in (icao, code):
        for n in (num, num.zfill(3), num.zfill(4)):
            cs = f"{pfx}{n}"
            if cs not in out:
                out.append(cs)
    return out


def _shape(ac, source, callsign):
    """Sameiginlegt svarform fyrir báðar gagnaveitur."""
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None
    alt = ac.get("alt_baro")
    if isinstance(alt, str):  # "ground"
        alt = 0
    return {
        "found": True,
        "callsign": (ac.get("flight") or callsign or "").strip(),
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "reg": ac.get("r"),
        "type": ac.get("t"),
        "vert": ac.get("baro_rate"),
        "hex": (ac.get("hex") or "").upper() or None,
        "source": source,
    }


def _find_local(cands):
    """Leitar fyrst í beinu straumnum frá flugumferd.is (besta þekjan yfir N-Atlantshafi)."""
    with radar_lock:
        fleet = list(radar_state["aircraft"])
    wanted = {c.upper() for c in cands}
    for ac in fleet:
        cs = (ac.get("flight") or "").strip().upper()
        if cs and cs in wanted:
            hit = _shape(ac, "flugumferd.is", cs)
            if hit:
                return hit
    return None


_GLOBAL_FEEDS = (
    ("adsb.lol", "https://api.adsb.lol/v2/callsign/{cs}"),
    ("adsb.fi", "https://opendata.adsb.fi/api/v2/callsign/{cs}"),
)

# Veitur sem svara hægt (t.d. vegna fyrirspurnamarka) eru hvíldar um stund.
_feed_penalty = {}
FEED_CONNECT_TIMEOUT = 2.0
FEED_READ_TIMEOUT = 3.0


def _feed_lookup(name, tmpl, cs):
    if time.time() < _feed_penalty.get(name, 0):
        return None
    try:
        r = requests.get(
            tmpl.format(cs=cs),
            timeout=(FEED_CONNECT_TIMEOUT, FEED_READ_TIMEOUT),
            headers={"User-Agent": "kef-fids/1.0"},
        )
        if r.status_code != 200:
            if r.status_code in (429, 403):
                _feed_penalty[name] = time.time() + 300
            return None
        for ac in _ac_list(r.json()):
            hit = _shape(ac, name, cs)
            if hit:
                return hit
    except requests.exceptions.RequestException:
        _feed_penalty[name] = time.time() + 60
    except Exception as e:
        print(f"{name} lookup error for {cs}: {e}")
    return None


_feed_pool = ThreadPoolExecutor(max_workers=16)


def _find_global(cands, deadline):
    """Hnattrænar ADS-B veitur — samhliða fyrirspurnir með hörðum tímafresti."""
    jobs = [(n, t, cs) for cs in cands[:2] for n, t in _GLOBAL_FEEDS]
    if not jobs:
        return None
    futures = [_feed_pool.submit(_feed_lookup, *j) for j in jobs]
    for f in futures:
        try:
            hit = f.result(timeout=max(0.05, deadline - time.time()))
        except Exception:
            continue
        if hit:
            return hit
    return None


# --- OpenSky: breið þekja yfir Atlantshafi/N-Ameríku, sótt í bakgrunni ---
_OPENSKY_BBOX = "lamin=30&lomin=-145&lamax=80&lomax=45"
_opensky_by_cs = {}
_opensky_ts = 0
_opensky_lock = threading.Lock()
OPENSKY_PERIOD = 900  # sek — 96 köll x 4 einingar = innan 400 eininga dagskvóta án innskráningar


def _opensky_worker():
    """Sækir heildarmynd frá OpenSky reglulega. Ekkert kall gerist inni í fyrirspurn."""
    global _opensky_by_cs, _opensky_ts
    while True:
        try:
            r = requests.get(
                f"https://opensky-network.org/api/states/all?{_OPENSKY_BBOX}",
                timeout=(5, 20),
                headers={"User-Agent": "kef-fids/1.0"},
            )
            if r.status_code == 200:
                by_cs = {}
                for s in r.json().get("states") or []:
                    cs = (s[1] or "").strip().upper()
                    if not cs or s[5] is None or s[6] is None:
                        continue
                    alt_m = s[13] if s[13] is not None else s[7]
                    by_cs[cs] = {
                        "lat": s[6],
                        "lon": s[5],
                        "alt_baro": round(alt_m * 3.28084) if alt_m is not None else None,
                        "gs": round(s[9] * 1.94384, 1) if s[9] is not None else None,
                        "track": s[10],
                        "baro_rate": round(s[11] * 196.85) if s[11] is not None else None,
                        "flight": cs,
                        "hex": (s[0] or "").upper(),
                        "r": None,
                        "t": None,
                    }
                with _opensky_lock:
                    _opensky_by_cs = by_cs
                    _opensky_ts = time.time()
        except Exception as e:
            print(f"opensky error: {e}")
        time.sleep(OPENSKY_PERIOD)


threading.Thread(target=_opensky_worker, daemon=True).start()


def _find_opensky(cands):
    """Uppfletting í minni — tekur örskotsstund."""
    with _opensky_lock:
        snap = _opensky_by_cs
    for cs in cands:
        ac = snap.get(cs.upper())
        if ac:
            hit = _shape(ac, "opensky", cs)
            if hit:
                return hit
    return None


# Síðasta þekkta staðsetning — brúar ADS-B eyðuna yfir miðju Atlantshafi.
_last_seen = {}
LAST_SEEN_MAX_AGE = 3 * 3600

# Vaktlisti: flug sem notandinn hefur opnað nýlega eru uppfærð í bakgrunni,
# svo svörin liggja tilbúin í minni þegar smellt er.
_watch = {}
WATCH_TTL = 300          # hættum að fylgjast með 5 mín eftir síðasta smell
TRACK_FRESH = 20         # sek — hversu gamalt svar má vera og teljast ferskt
TRACK_WORKER_PERIOD = 6  # sek milli bakgrunnsuppfærslna
FIRST_HIT_DEADLINE = 3.0  # sek — hámarksbið við allra fyrsta smell


# Skrásetningarnúmer/vélargerð fyrir vélar sem OpenSky þekkir ekki (sótt í bakgrunni).
_hex_cache = {}


def _hex_fetch(hx):
    try:
        r = requests.get(
            f"https://hexdb.io/api/v1/aircraft/{hx}",
            timeout=(2, 4),
            headers={"User-Agent": "kef-fids/1.0"},
        )
        d = r.json() if r.status_code == 200 else {}
        _hex_cache[hx] = (d.get("Registration"), d.get("ICAOTypeCode"))
    except Exception:
        _hex_cache[hx] = (None, None)


def _enrich(result):
    """Bætir við skrásetningu/gerð úr minni; sækir í bakgrunni ef vantar."""
    hx = result.get("hex")
    if not hx or (result.get("reg") and result.get("type")):
        return result
    got = _hex_cache.get(hx)
    if got is None:
        _feed_pool.submit(_hex_fetch, hx)  # bíðum ekki — kemur í næstu uppfærslu
        return result
    reg, typ = got
    result["reg"] = result.get("reg") or reg
    result["type"] = result.get("type") or typ
    return result


def _resolve(key, cands, deadline):
    """Finnur staðsetningu: fyrst í minni, svo hnattrænar veitur ef tími leyfir."""
    result = _find_local(cands) or _find_opensky(cands)
    if not result and time.time() < deadline:
        result = _find_global(cands, deadline)

    now = time.time()
    if result:
        _enrich(result)
        with _track_lock:
            if len(_last_seen) > 500:
                _last_seen.clear()
            _last_seen[key] = (now, dict(result))
    else:
        with _track_lock:
            prev = _last_seen.get(key)
        if prev and now - prev[0] < LAST_SEEN_MAX_AGE:
            result = dict(prev[1])
            result["stale"] = True
            result["age_min"] = int((now - prev[0]) / 60)
        else:
            result = {"found": False, "reason": "not_airborne", "tried": cands[:3]}
    result["anr"] = f"https://www.airnavradar.com/flight/{key}"

    with _track_lock:
        if len(_track_cache) > 500:
            _track_cache.clear()
        _track_cache[key] = (now, result)
    return result


def _track_worker():
    """Heldur vöktuðum flugum ferskum í bakgrunni."""
    while True:
        now = time.time()
        with _track_lock:
            for k, (ts, _) in list(_watch.items()):
                if now - ts > WATCH_TTL:
                    _watch.pop(k, None)
            jobs = [(k, c) for k, (ts, c) in _watch.items()]
        for key, cands in jobs:
            try:
                _resolve(key, cands, time.time() + 5)
            except Exception as e:
                print(f"track worker {key}: {e}")
        time.sleep(TRACK_WORKER_PERIOD)


threading.Thread(target=_track_worker, daemon=True).start()


@app.route("/api/track/<flight>")
def track_api(flight):
    """Staðsetning einnar flugvélar eftir flugnúmeri — svarar úr minni."""
    key = (flight or "").upper().strip()
    cands = _callsign_candidates(key)
    if not cands:
        return jsonify({"found": False, "reason": "bad_flight"})

    now = time.time()
    with _track_lock:
        _watch[key] = (now, cands)
        hit = _track_cache.get(key)
        if hit and now - hit[0] < TRACK_FRESH:
            return jsonify(hit[1])

    # Fyrsta uppfletting: stutt, afmörkuð leit. Bakgrunnsþráðurinn sér um framhaldið.
    return jsonify(_resolve(key, cands, now + FIRST_HIT_DEADLINE))


_TILE_SOURCES = {
    "sat": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "image/jpeg"),
    "labels": ("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", "image/png"),
}
_tile_cache = {}


@app.route("/api/tile/<kind>/<int:z>/<int:x>/<int:y>")
def tile_proxy(kind, z, x, y):
    """Varaleið fyrir kortaflísar ef vafrinn nær ekki beint í Esri."""
    if kind not in _TILE_SOURCES or not (0 <= z <= 18):
        return ("bad tile request", 400)
    key = (kind, z, x, y)
    if key not in _tile_cache:
        if len(_tile_cache) > 4000:
            _tile_cache.clear()
        url = _TILE_SOURCES[kind][0].format(z=z, x=x, y=y)
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "kef-fids/1.0"})
            if r.status_code != 200:
                return ("tile unavailable", 502)
            _tile_cache[key] = (r.content, r.headers.get("Content-Type", _TILE_SOURCES[kind][1]))
        except Exception as e:
            print(f"tile proxy error: {e}")
            return ("tile error", 502)
    body, ctype = _tile_cache[key]
    return app.response_class(
        body,
        mimetype=ctype,
        headers={"Cache-Control": "public, max-age=604800"},
    )


if __name__ == "__main__":
    print("🛫 KEF Gate D Flights server starting on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
