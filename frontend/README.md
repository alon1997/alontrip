# Frontend

Vue 3 + Vite single-page app, dark code-editor aesthetic. Served under the
`/trip/` path prefix (`base: '/trip/'` in `vite.config.js`).

## Development

```bash
npm install
npm run dev            # http://localhost:5173/trip/
```

The backend must be running (see `../README.md` — `python -m app.main` in
`../backend/`). The API base URL comes from `VITE_API_BASE_URL`
(default `http://localhost:5003`, see `.env.example`).

## Production build

```bash
npm run build          # emits to dist/
npm run preview        # serve the build locally
```

Set `VITE_API_BASE_URL=''` (same origin) or an absolute URL before building
when the API lives on a different origin.
