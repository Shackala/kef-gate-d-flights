"""
KEF Gate D Flights — Python Backend
Scrapes kefairport.is/fids and serves Gate D arrivals, departures & Heimavellir as JSON.
"""

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


def scrape_cargo():
    """Scrape cargo flights from kefairport.is and filter non-Schengen only."""
    cargo_departures = []
    cargo_arrivals = []

    for url, direction in [(CARGO_DEP_URL, "departures"), (CARGO_ARR_URL, "arrivals")]:
        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; KEFGateDBoard/1.0)"
            })
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[WARN] Failed to fetch cargo {direction}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        tables = soup.find_all("table")

        for table in tables:
            rows = table.find_all("tr")
            if not rows:
                continue
            headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
            if not headers:
                continue

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["th", "td"])]
                if len(cells) < len(headers):
                    continue
                cell_map = dict(zip(headers, cells))

                # Determine location field
                location = ""
                if direction == "departures":
                    location = cell_map.get("destination", cell_map.get("áfangastaður", ""))
                else:
                    location = cell_map.get("origin", cell_map.get("uppruni", ""))

                # Skip Schengen destinations/origins
                if is_schengen(location):
                    continue

                entry = {
                    "flight": cell_map.get("flight", cell_map.get("flug", "")),
                    "location": location,
                    "scheduled": cell_map.get("std", cell_map.get("sta", cell_map.get("áætlað", ""))),
                    "estimated": cell_map.get("etd", cell_map.get("eta", cell_map.get("áætlun", ""))),
                    "status": cell_map.get("status", cell_map.get("staða", "")),
                    "acType": cell_map.get("a/c type", cell_map.get("flugvélategund", "")),
                    "acReg": cell_map.get("a/c reg", cell_map.get("skráningarnúmer", "")),
                }

                if direction == "departures":
                    cargo_departures.append(entry)
                else:
                    cargo_arrivals.append(entry)

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


if __name__ == "__main__":
    print("🛫 KEF Gate D Flights server starting on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
