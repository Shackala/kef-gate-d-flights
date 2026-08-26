"""
KEF Gate D Flights — Python Backend
Scrapes kefairport.is/fids and serves Gate D arrivals, departures & Heimavellir as JSON.
"""

import json
import re
import time
import threading
from flask import Flask, jsonify, send_from_directory
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
    """Return cached data or scrape fresh if stale."""
    with lock:
        now = time.time()
        if cache["data"] is None or (now - cache["timestamp"]) > CACHE_TTL:
            data = scrape_flights()
            if data:
                cache["data"] = data
                cache["timestamp"] = now
        return cache["data"]


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
    with radar_lock:
        age = time.time() - radar_state["updated"] if radar_state["updated"] else None
        return jsonify(
            {
                "aircraft": radar_state["aircraft"],
                "now": radar_state["now"],
                "age": age,
                "status": radar_state["status"],
                "source": "flugumferd.is",
            }
        )


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


def _find_global(cands):
    """Varaleið: adsb.lol — hnattræn þekja fyrir vélar utan íslenska straumsins."""
    for cs in cands[:3]:
        try:
            r = requests.get(
                f"https://api.adsb.lol/v2/callsign/{cs}",
                timeout=8,
                headers={"User-Agent": "kef-fids/1.0"},
            )
            if r.status_code != 200:
                continue
            for ac in r.json().get("ac", []):
                hit = _shape(ac, "adsb.lol", cs)
                if hit:
                    return hit
        except Exception as e:
            print(f"adsb.lol lookup error for {cs}: {e}")
    return None


@app.route("/api/track/<flight>")
def track_api(flight):
    """Staðsetning einnar flugvélar eftir flugnúmeri."""
    key = (flight or "").upper().strip()
    now = time.time()
    with _track_lock:
        hit = _track_cache.get(key)
        if hit and now - hit[0] < TRACK_TTL:
            return jsonify(hit[1])

    cands = _callsign_candidates(key)
    if not cands:
        return jsonify({"found": False, "reason": "bad_flight"})

    result = _find_local(cands) or _find_global(cands)
    if not result:
        result = {"found": False, "reason": "not_airborne", "tried": cands[:3]}

    with _track_lock:
        if len(_track_cache) > 500:
            _track_cache.clear()
        _track_cache[key] = (now, result)
    return jsonify(result)


_TILE_STYLES = {"dark_all", "light_all", "dark_nolabels", "light_nolabels"}
_tile_cache = {}


@app.route("/api/tile/<style>/<int:z>/<int:x>/<int:y>.png")
def tile_proxy(style, z, x, y):
    """Varaleið fyrir kortaflísar ef vafrinn nær ekki beint í CARTO."""
    if style not in _TILE_STYLES or not (0 <= z <= 18):
        return ("bad tile request", 400)
    key = (style, z, x, y)
    if key not in _tile_cache:
        if len(_tile_cache) > 4000:
            _tile_cache.clear()
        url = f"https://a.basemaps.cartocdn.com/{style}/{z}/{x}/{y}.png"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "kef-fids/1.0"})
            if r.status_code != 200:
                return ("tile unavailable", 502)
            _tile_cache[key] = r.content
        except Exception as e:
            print(f"tile proxy error: {e}")
            return ("tile error", 502)
    return app.response_class(
        _tile_cache[key],
        mimetype="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


if __name__ == "__main__":
    print("🛫 KEF Gate D Flights server starting on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
