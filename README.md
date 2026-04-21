# ✈️ KEF Gate D Flights

A live flight board showing arrivals and departures at **Gate D** of Keflavík International Airport (KEF), Iceland.

Auto-refreshes every 15 seconds with a clean, dark airport-style UI.

---

## 🚀 Quick Start (Local)

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Run the server
python server.py

# 3. Open in your browser
# http://localhost:5000
```

---

## 📁 Project Structure

```
kef-flights-web/
├── server.py          # Flask backend — scrapes kefairport.is, serves JSON API
├── static/
│   └── index.html     # Standalone frontend — dark theme flight board
├── requirements.txt   # Python dependencies
└── README.md
```

---

## 🌐 Deploy to the Internet

### Option A: Railway (easiest, free tier available)

1. Push this folder to a **GitHub repo**
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Railway auto-detects Python. Set the **start command** to:
   ```
   python server.py
   ```
4. Done! You'll get a public URL like `https://your-app.up.railway.app`

### Option B: Render (free tier available)

1. Push to GitHub
2. Go to [render.com](https://render.com) → New Web Service
3. Set:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python server.py`
4. Deploy — you'll get a public `.onrender.com` URL

### Option C: Any VPS / Server

```bash
# Install dependencies
pip install -r requirements.txt

# Run with gunicorn for production
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5000 server:app
```

---

## 🔧 Configuration

- **Refresh interval:** Change `CACHE_TTL` in `server.py` (default: 15 seconds)
- **Port:** Change the port in `server.py` or set the `PORT` environment variable
- **Frontend refresh:** Change the `setInterval` value in `static/index.html`

---

## 📱 Mobile Access

Once deployed, open the URL on your phone and **Add to Home Screen** for an app-like experience.
