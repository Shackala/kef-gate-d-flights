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


def scrape_cargo_page(url):
    """Scrape a single cargo page and return list of parsed flight dicts."""
    flights = []
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; KEFGateDBoard/1.0)"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[WARN] Failed to fetch cargo page {url}: {e}")
        return flights

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        if not headers:
            continue

        header_idx = {h: i for i, h in enumerate(headers)}

        # Location column — departures page uses "áfangastaður", arrivals uses "kemur frá"
        loc_col = header_idx.get("áfangastaður",
                  header_idx.get("kemur frá",
                  header_idx.get("destination",
                  header_idx.get("origin", -1))))

        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < len(headers):
                continue

            flight_col = header_idx.get("flug", header_idx.get("flight", -1))
            flight = cells[flight_col].get_text(strip=True) if flight_col >= 0 else ""

            location = extract_location_from_cell(cells[loc_col]) if loc_col >= 0 else ""

            time_col = header_idx.get("tími", header_idx.get("std", header_idx.get("sta", -1)))
            scheduled = cells[time_col].get_text(strip=True) if time_col >= 0 else ""
            if scheduled:
                times = re.findall(r'\d{1,2}:\d{2}', scheduled)
                scheduled = times[-1] if times else scheduled

            est_col = header_idx.get("áætlað", header_idx.get("etd", header_idx.get("eta", -1)))
            estimated = cells[est_col].get_text(strip=True) if est_col >= 0 else ""
            if estimated:
                times = re.findall(r'\d{1,2}:\d{2}', estimated)
                estimated = times[-1] if times else estimated

            status_col = header_idx.get("staða", header_idx.get("status", -1))
            status = cells[status_col].get_text(strip=True) if status_col >= 0 else ""

            airline_col = header_idx.get("flugfélag", header_idx.get("airline", -1))
            airline = cells[airline_col].get_text(strip=True) if airline_col >= 0 else ""

            if is_schengen(location):
                continue

            flights.append({
                "flight": flight,
                "location": location,
                "scheduled": scheduled,
                "estimated": estimated,
                "status": status,
                "airline": airline,
                "acType": "",
                "acReg": "",
            })

    return flights


# Arrival-related statuses (Icelandic)
ARRIVAL_STATUSES = {"lent", "lentur", "lent á áætlun"}
# Departure-related statuses (Icelandic)
DEPARTURE_STATUSES = {"farin", "farinn", "farin á áætlun"}


def scrape_cargo():
    """Scrape cargo flights from kefairport.is and split into departures/arrivals.

    The kefairport.is brottfarir and komur pages return identical data,
    so we scrape both and deduplicate.  We split by status:
      - Farin / Farinn  → departure
      - Lent / Lentur   → arrival
      - Á áætlun etc.   → use which page it was scraped from as tiebreaker
    """
    dep_page = scrape_cargo_page(CARGO_DEP_URL)
    arr_page = scrape_cargo_page(CARGO_ARR_URL)

    # Build a set of (flight, scheduled) keys seen on each page for Á áætlun tiebreaker
    dep_keys = {(f["flight"], f["scheduled"]) for f in dep_page}
    arr_keys = {(f["flight"], f["scheduled"]) for f in arr_page}

    # Merge all flights, dedup by (flight, scheduled)
    seen = set()
    all_flights = []
    for f in dep_page + arr_page:
        key = (f["flight"], f["scheduled"])
        if key not in seen:
            seen.add(key)
            all_flights.append(f)

    cargo_departures = []
    cargo_arrivals = []

    for f in all_flights:
        status_lower = f["status"].lower().strip()
        key = (f["flight"], f["scheduled"])

        if status_lower in DEPARTURE_STATUSES:
            cargo_departures.append(f)
        elif status_lower in ARRIVAL_STATUSES:
            cargo_arrivals.append(f)
        else:
            # Scheduled / unknown — assign based on page presence
            # If only on dep page → departure; only on arr page → arrival;
            # on both → show as departure (default for cargo scheduled flights)
            in_dep = key in dep_keys
            in_arr = key in arr_keys
            if in_dep and not in_arr:
                cargo_departures.append(f)
            elif in_arr and not in_dep:
                cargo_arrivals.append(f)
            else:
                # On both pages — default to departure for scheduled flights
                cargo_departures.append(f)

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
