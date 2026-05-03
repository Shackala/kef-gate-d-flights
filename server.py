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

    return {
        "arrivals": arrivals,
        "departures": departures,
        "heimavellir": {
            "morning": fi_morning,
            "afternoon": fi_afternoon,
        },
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
