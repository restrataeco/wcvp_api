# WCVP / POWO CSV API

Serves your `wcvp_names.csv` (or any similar POWO export) as a searchable,
paginated REST API. No database — the CSV is loaded into memory at startup.
To update data later: replace the CSV file and call `POST /reload`.

## 1. Run locally on Windows

**Requirements:** Python 3.11+ installed (check with `python --version` in
Command Prompt / PowerShell).

```powershell
# 1. Unzip this project, then open a terminal in the wcvp_api folder

# 2. Create a virtual environment
python -m venv venv
venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up your config
copy .env.example .env
# then edit .env: set CSV_PATH to the location of your wcvp_names.csv,
# e.g. CSV_PATH=C:/Users/yourname/Downloads/wcvp_names.csv

# 5. Run the dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open http://127.0.0.1:8000/docs — FastAPI auto-generates an interactive
Swagger UI where you can try every endpoint from the browser.

## 2. First things to check

- `GET /columns` — confirms the actual column names pandas picked up from
  your CSV (WCVP exports vary slightly by version — use these exact names
  in `/filter` and `/search?columns=`).
- `GET /health` — confirms row count loaded.

## 3. Example calls

```
GET /search?q=Lem&page=1&page_size=20
GET /search?q=Lem&columns=taxon_name&page=2
GET /filter?column=family&value=Orchidaceae
GET /records/104
POST /reload   (header: X-Reload-Token: <your token from .env>)
```

## 4. Updating the data (every few months)

1. Download the new CSV from POWO.
2. Replace the file at the path set in `CSV_PATH`.
3. Call `POST /reload` with header `X-Reload-Token: <your RELOAD_TOKEN>`.
   No restart needed, no re-import step, no schema mapping.

```powershell
curl -X POST http://127.0.0.1:8000/reload -H "X-Reload-Token: change-me-to-a-real-secret"
```

## 5. Moving to production

The dev command (`uvicorn --reload`) is for local testing only — single
process, auto-restarts on code changes, not resilient to crashes.

**Option A — Docker (recommended, works the same on any host):**

```bash
docker build -t wcvp-api .
docker run -d \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/.env:/app/.env \
  -p 8000:8000 \
  --restart unless-stopped \
  wcvp-api
```

Put your CSV in a `data/` folder next to the Dockerfile before running —
it's mounted as a volume, so replacing the file on the host and calling
`/reload` works without rebuilding or restarting the container.

**Option B — bare server (Linux VPS), no Docker:**

```bash
pip install -r requirements.txt
gunicorn app.main:app -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120
```

Run this under a process manager (systemd, or `pm2`/`supervisor`) so it
restarts automatically on crash or server reboot. Put nginx or Caddy in
front for HTTPS and to avoid exposing port 8000 directly.

## 6. Notes on scale

- A ~300MB CSV loads in a few seconds and uses roughly 1–2GB RAM once in
  memory (pandas overhead on string data) — fine on any machine with 4GB+
  free, but avoid the smallest 512MB cloud instances.
- All columns load as text (`dtype=str`) to avoid pandas guessing types
  inconsistently across a large file. If you need numeric filtering/sorting
  later (e.g. by year), cast specific columns after load — ask and I'll
  add that.
- `-w 2` in gunicorn runs 2 worker processes, each holding its own copy of
  the data in memory — so double the RAM estimate above per worker. Adjust
  worker count to your server's RAM, not just CPU count.

Here, -m venv tells Python to run its built-in venv module, and the second venv is just the folder name it will create (you could name it anything, e.g. env).

python -m venv venv

Then activate it:
venv\Scripts\activate

If PowerShell blocks the activation with an execution-policy error, run this once first:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

Install dependencies
pip install -r requirements.txt

To run the server
uvicorn app.main:app --reload