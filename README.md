# WCVP / POWO CSV API

Serves Kew's World Checklist of Vascular Plants (WCVP) as a searchable,
paginated REST API, with each plant's distribution available by `ipni_id`.

The data comes straight from Kew's
[wcvp.zip](https://sftp.kew.org/pub/data-repositories/WCVP/), which holds
`wcvp_names.csv` and `wcvp_distribution.csv`. The CSVs are loaded into a
local SQLite file (`data/wcvp.db`) and queried from there. The app checks
Kew once a day and updates itself when a new release is published.

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

# 4. Set up your config (set a real RELOAD_TOKEN; the defaults are fine otherwise)
copy .env.example .env

# 5. Run the dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

On the very first start there's no data yet, so the app downloads
`wcvp.zip` (~85MB) from Kew, extracts both CSVs and builds `data/wcvp.db`.
This takes a few minutes. Later starts reuse the existing DB and are instant.

Then open http://127.0.0.1:8000/docs — FastAPI auto-generates an interactive
Swagger UI where you can try every endpoint from the browser.

## 2. First things to check

- `GET /columns` — confirms the actual column names pandas picked up from
  your CSV, and which of them `/search` and `/filter` accept.
- `GET /health` — confirms row count loaded, whether distribution data is
  in, and which Kew release (`data_version`) is loaded.
- `GET /source` — the zip URL, the loaded release, and the result of the
  last update check.

## 3. Example calls

```
GET /search?q=Rosa
GET /search?q=Lantana camara&page=2
GET /search?q=rosa&exact=true
GET /search?q=Rosa&columns=genus,taxon_name
GET /filter?column=family&value=Orchidaceae&page=3
GET /distribution/325686-2          (an ipni_id taken from a search result)
GET /records/104
GET /source
POST /check-update   (header: X-Reload-Token: <your token from .env>)
POST /reload         (header: X-Reload-Token: <your token from .env>)
```

### Search and filter

Both only look at the name columns `genus`, `family`, `species`,
`infraspecies` and `taxon_name`, and always return **10 results per page**.
Use `page` to move through them, and `total` / `total_pages` to know how
many there are.

`/search` ranks the best matches first:
1. how well it matches: the whole field, then starts with, then contains
   (`exact=true` keeps only whole-field matches and is much faster);
2. which column matched: taxon_name, then genus, species, infraspecies, family;
3. accepted names before synonyms;
4. shorter names first, so `q=Rosa` puts the genus *Rosa* at the top.

`/filter` is an exact, case-insensitive match on one of those columns,
accepted names first.

### Distribution

Search and filter results don't include distribution. Take the `ipni_id`
from a result and call `GET /distribution/{ipni_id}` to get its `native`
and `introduced` areas (WGSRPD continent / region / area, plus `extinct`
and `location_doubtful` flags).

WCVP only records distribution against accepted names, so a synonym
returns the distribution of its accepted name. `accepted_plant_name_id`
and `accepted_taxon_name` in the response say which one. For example,
*Lantana camara f. alba* returns the distribution of
*Lantana nivea subsp. nivea*.

## 4. Updating the data (automatic)

Kew republishes `wcvp.zip` every 4–6 months. The app handles it:

1. Right after startup, and then every `UPDATE_CHECK_HOURS` (default 24),
   it sends one HEAD request for the zip and compares Kew's `ETag` /
   `Last-Modified` with the release it last downloaded. That release is
   saved in `data/wcvp_source.json`.
2. If they're the same, nothing happens. A check takes about a second.
3. If Kew has a new release, it downloads the zip, extracts both CSVs,
   builds a new database with all its indexes in a temp file, then swaps
   it in. The old database keeps answering requests the whole time
   (about 2–3 minutes), so there's no downtime and no restart needed.

A failed or corrupt download leaves the current CSVs and DB untouched, and
the next check tries again.

To run things by hand (both need the `X-Reload-Token` header):

```powershell
# check now, and update only if Kew has something newer
curl -X POST http://127.0.0.1:8000/check-update -H "X-Reload-Token: <your RELOAD_TOKEN>"

# re-download and rebuild even if nothing changed
curl -X POST http://127.0.0.1:8000/reload -H "X-Reload-Token: <your RELOAD_TOKEN>"
```

Set `UPDATE_CHECK_ENABLED=false` in `.env` to turn the daily check off.

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

The `data/` folder is mounted as a volume, so the downloaded CSVs and the
database survive container restarts and image rebuilds. The container
needs outbound HTTPS access to `sftp.kew.org` for the daily check.

**Option B — bare server (Linux VPS), no Docker:**

```bash
pip install -r requirements.txt
gunicorn app.main:app -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120
```

Run this under a process manager (systemd, or `pm2`/`supervisor`) so it
restarts automatically on crash or server reboot. Put nginx or Caddy in
front for HTTPS and to avoid exposing port 8000 directly.

## 6. Notes on scale

- Data lives on disk in SQLite, not in memory, so RAM use is low. The CSVs
  are read in chunks during a build. Plan for about 2GB of disk: the
  database (~670MB), the two CSVs (~430MB), and room for a second database
  while an update builds.
- All columns are stored as text, to avoid pandas guessing types
  inconsistently across a large file.
- `-w 2` in gunicorn runs 2 worker processes sharing the same database
  file. A file lock (`data/wcvp.db.lock`) makes sure only one of them
  downloads or builds at a time; the other picks up the result.

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