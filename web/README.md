# Zaliver web UI (React + Vite)

## One server (recommended)

Build static files, then run FastAPI — оно отдаёт и API, и UI:

```bash
cd web
npm install
npm run build

cd ..
set ZALIVER_API_TOKEN=your-secret
set PYTHONPATH=src
python -m zaliver.api
```

Открой http://127.0.0.1:8080 — UI на `/`, API на `/v1` и `/health` (по умолчанию `127.0.0.1:8080`).

Опционально: `ZALIVER_WEB_DIST=C:\path\to\dist` если сборка лежит не в `web/dist`.

Чтобы упаковать UI рядом с пакетом:

```bash
# после npm run build
# Windows PowerShell:
Remove-Item -Recurse -Force src\zaliver\api\web_dist -ErrorAction SilentlyContinue
Copy-Item -Recurse web\dist src\zaliver\api\web_dist
```

## Dev (hot reload)

```bash
# терминал 1 — API
set PYTHONPATH=src
python -m zaliver.api

# терминал 2 — Vite
cd web
npm run dev
```

http://127.0.0.1:5173 — Vite проксирует `/v1` и `/health` на `:8080`.
