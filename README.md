# AlonTrip

> A public-transit-optimized travel planner for budget backpackers in East Asia.
>
> Live: https://alonuniverse.com/trip/

Plan multi-day trips by public transit only — subway, bus, and walking; no
taxis, no car rentals — with AI-optimized daily routes and map visualization
of real transit itineraries: transfers, times, and fares.

## Tech Stack

| Layer | Choice |
|-------|--------|
| Frontend | Vue 3 + Vite |
| Map | Leaflet + OpenStreetMap |
| Backend | FastAPI (Python) |
| Transit & POI data | SerpApi (`google_maps_directions` and `google_maps` engines) |
| AI route grouping | DeepSeek API (Anthropic-compatible endpoint) |
| Database | MySQL |
| Deployment | Nginx reverse proxy |

## Local Setup

### Backend

```bash
cp .env.example .env        # from 04app/ — optional, only needed for live SerpApi/DeepSeek/MySQL
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main          # serves on APP_PORT (default 5003)
```

`.env` lives at the repo root (`04app/.env`), not inside `backend/`. `Settings` resolves it by
an absolute path, so it's found regardless of which directory you launch uvicorn from.

> **Zero-key mode:** without `SERPAPI_KEY` / `DEEPSEEK_API_KEY` the backend
> automatically falls back to local seed data (POIs), distance-based route
> estimates, and rule-based grouping — the whole flow runs with no
> configuration at all.

Verify the API is up:

```bash
curl http://localhost:5003/api/trip/health
# → {"status":"ok","poi_provider":"local-json","transit_provider":"local-json","grouper":"rule-based"}
```

For development with auto-reload:

```bash
uvicorn app.main:app --reload --port 5003
```

### Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173/trip/
```

Production build must set `VITE_API_BASE_URL=''` (same-origin `/api/trip`). Forgetting this points the UI at localhost:5003.

## Environment Variables

Copy `.env.example` to `.env` and fill in the values. `.env` is gitignored —
never commit it.

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `SERPAPI_KEY` | ✅ | — | SerpApi API key (serpapi.com) |
| `DEEPSEEK_API_KEY` | ✅ | — | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | — | `https://api.deepseek.com/anthropic` | Anthropic-compatible API base URL |
| `APP_PORT` | — | `5003` | Port the backend listens on |
| `CORS_ORIGINS` | — | `http://localhost:5173,http://localhost:4173` | Comma-separated browser origins allowed by CORS |

## API

Current routes, and which ones call SerpApi / DeepSeek: **[docs/API.md](docs/API.md)**. Interactive: `http://127.0.0.1:5003/docs`.

## Project Status

- Live demo: https://alonuniverse.com/trip/ (kept current with the repo via `scripts/deploy.sh`)
- Local: frontend `http://localhost:5173/trip/`, backend `:5003`
- This folder is its own git repo, kept out of the private `personal` remote (D-005).

## Acknowledgments

- **SerpApi** — powers every transit leg and the POI catalog. Extra thanks to
  **Alaa, Roi and Jordanne** from the SerpApi team, who manually onboarded this
  project when signup was blocked and granted hackathon credits mid-competition.
- **DeepSeek** (`deepseek-v4-flash`) — fast, affordable day-by-day plan drafting.
