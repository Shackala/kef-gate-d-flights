# Keflavíkurflugvöllur — Hlið D Flugupplýsingar

Live flight board showing Gate D arrivals, departures, and Icelandair (FI) home-bound flights from Keflavík International Airport.

## Features

- **Komur** — Gate D arrivals
- **Brottfarir** — Gate D departures
- **Heimavellir** — Icelandair (FI) Gate D departures split into:
  - 🌅 Morgunflug (06:00 – 12:00)
  - 🌇 Síðdegisflug (13:00 – 22:00)
- Auto-refreshes every 15 seconds (seamless, no flicker)
- Dark theme with Icelandic UI

## Run Locally

```bash
pip install -r requirements.txt
python server.py
```

Open http://localhost:5000

## Deploy to Railway

1. Push this folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub Repo**
3. Select your repo — Railway auto-detects the Procfile
4. Click **Generate Domain** in Settings to get your public URL

## Tech Stack

- **Backend:** Python / Flask / BeautifulSoup
- **Frontend:** HTML / Tailwind CSS / DaisyUI
- **Data:** Scraped from kefairport.is/fids, cached 15s
