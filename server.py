"""
KEF Gate D Flights — Python Backend
Scrapes kefairport.is/fids and serves Gate D arrivals & departures as JSON.
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

    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

        # Determine if arrivals or departures by column names
        is_arrival = "origin" in headers or "sta" in headers or "belt" in headers
        is_departure = "destination" in headers or "std" in headers

        if not is_arrival and not is_departure:
            # Try to determine from surrounding text
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
            if not re.match(r"^D\d*", gate, re.IGNORECASE):
                continue

            if is_arrival:
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
                departures.append({
                    "flight": cell_map.get("flight", ""),
                    "destination": cell_map.get("destination", ""),
                    "std": cell_map.get("std", ""),
                    "etd": cell_map.get("etd", ""),
                    "status": cell_map.get("status", ""),
                    "gate": gate,
                })

    return {
        "arrivals": arrivals,
        "departures": departures,
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
