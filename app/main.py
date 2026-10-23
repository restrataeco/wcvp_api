"""
WCVP / POWO CSV-backed API
--------------------------
Serves a large CSV (e.g. wcvp_names.csv) as a searchable, paginated REST API,
backed by a local SQLite file built from the CSV — not an in-memory
dataframe, and not a re-scan of the CSV on every request.

Data source: Kew's wcvp.zip (WCVP_ZIP_URL), which holds wcvp_names.csv and
wcvp_distribution.csv and is republished every 4–6 months.

Startup flow:
  1. If either CSV is missing (or FORCE_DOWNLOAD is true): download the zip
     and extract both. Otherwise use the CSVs already on disk.
  2. If no SQLite DB exists yet at DB_PATH (or FORCE_REBUILD is true, or the
     CSVs were just downloaded): build one from the CSVs. Otherwise skip
     straight to serving — a plain restart does NOT re-download or rebuild.
  3. A background thread checks the zip's Last-Modified/ETag every
     UPDATE_CHECK_HOURS and repeats steps 1–2 only when Kew has a new release.
  4. POST /reload always does both steps — the explicit "refresh now" trigger.

Once built, every request queries the SQLite file directly with indexed SQL —
fast, low memory, and gives back exact total counts (no more "total: null").
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from filelock import FileLock

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wcvp_api")

app = FastAPI(
    title="WCVP Data API",
    description="Read-only API over a POWO/WCVP CSV export, backed by SQLite.",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_build_lock = threading.Lock()
_last_ready_at: Optional[datetime] = None
_columns: list = []
_has_distribution: bool = False
_last_update_check: Optional[dict] = None
_stop_updates = threading.Event()

# gunicorn runs several worker processes; this OS-level lock keeps them from
# downloading or building at the same time. The OS releases it if a worker dies.
_process_lock = FileLock(settings.db_path + ".lock")

# Members of Kew's wcvp.zip -> the setting that says where each one goes.
ZIP_MEMBERS = {"wcvp_names.csv": "csv_path", "wcvp_distribution.csv": "distribution_csv_path"}

TABLE = "records"
DIST_TABLE = "distribution"

# /search and /filter only look at these name columns, and return a fixed
# page of results.
NAME_COLUMNS = ["genus", "family", "species", "infraspecies", "taxon_name"]
PAGE_SIZE = 10
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")  # guards against SQL injection via column names


def _valid_identifier(name: str) -> bool:
    return bool(IDENTIFIER_RE.match(name))


def _escape_like(value: str) -> str:
    """Escape % _ and \\ so user input can't inject LIKE wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Source: Kew's wcvp.zip ---------------------------------------------------

def _version_from_headers(headers) -> dict:
    return {
        "last_modified": headers.get("Last-Modified"),
        "etag": headers.get("ETag"),
        "content_length": headers.get("Content-Length"),
    }


def fetch_remote_version() -> dict:
    """One HEAD request: what version of the zip Kew is serving right now."""
    r = requests.head(settings.wcvp_zip_url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return _version_from_headers(r.headers)


def read_local_version() -> Optional[dict]:
    """Which version of the zip the CSVs on disk came from (None if unknown)."""
    try:
        with open(settings.source_state_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _write_local_version(version: dict) -> None:
    os.makedirs(os.path.dirname(settings.source_state_path) or ".", exist_ok=True)
    tmp = settings.source_state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({**version, "downloaded_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    os.replace(tmp, settings.source_state_path)


def _same_version(remote: dict, local: Optional[dict]) -> bool:
    """ETag is the most precise signal, then Last-Modified; unknown local = changed."""
    if not local:
        return False
    for key in ("etag", "last_modified"):
        if remote.get(key) and local.get(key):
            return remote[key] == local[key]
    return False


def download_and_extract_zip() -> dict:
    """
    Downloads wcvp.zip and extracts both CSVs. Everything goes to temp files
    first and is renamed into place only once both CSVs extracted cleanly, so
    a failed or corrupt download leaves the current CSVs untouched.
    """
    zip_tmp = settings.db_path + ".zip.tmp"
    os.makedirs(os.path.dirname(zip_tmp) or ".", exist_ok=True)

    logger.info(f"Downloading {settings.wcvp_zip_url} ...")
    with requests.get(settings.wcvp_zip_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        version = _version_from_headers(r.headers)
        with open(zip_tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):  # 8MB chunks
                if chunk:
                    f.write(chunk)

    extracted = []  # (tmp_path, final_path)
    try:
        with zipfile.ZipFile(zip_tmp) as zf:
            by_name = {os.path.basename(n): n for n in zf.namelist()}
            missing = [m for m in ZIP_MEMBERS if m not in by_name]
            if missing:
                raise ValueError(f"{settings.wcvp_zip_url} does not contain {missing}")
            for member, setting_name in ZIP_MEMBERS.items():
                dest = getattr(settings, setting_name)
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                tmp = dest + ".tmp"
                logger.info(f"  ...extracting {member}")
                with zf.open(by_name[member]) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 8 * 1024 * 1024)
                extracted.append((tmp, dest))
        for tmp, dest in extracted:
            os.replace(tmp, dest)
            logger.info(f"  ...{dest} updated")
    finally:
        for tmp, _ in extracted:
            if os.path.exists(tmp):
                os.remove(tmp)
        if os.path.exists(zip_tmp):
            os.remove(zip_tmp)

    _write_local_version(version)
    logger.info(f"WCVP data downloaded (Last-Modified: {version['last_modified']}).")
    return version


# --- SQLite build -------------------------------------------------------

def build_sqlite_from_csv() -> None:
    """
    Reads the CSV in chunks (bounded memory) and writes it into a fresh
    SQLite file, then builds indexes. Builds into a .building temp file and
    atomically renames into place at the end, so any request hitting the
    real DB_PATH mid-build still sees the last good, fully-built database.
    """
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    tmp_db_path = settings.db_path + ".building"
    if os.path.exists(tmp_db_path):
        os.remove(tmp_db_path)

    logger.info(f"Building SQLite DB from {settings.csv_path} ...")
    conn = sqlite3.connect(tmp_db_path)
    try:
        offset = 0
        first_chunk = True
        reader = pd.read_csv(
            settings.csv_path,
            delimiter=settings.csv_delimiter,
            dtype=str,
            keep_default_na=False,
            low_memory=False,
            chunksize=settings.build_chunk_size,
        )
        for chunk in reader:
            chunk = chunk.reset_index(drop=True)
            chunk.insert(0, "_row_id", range(offset, offset + len(chunk)))
            chunk.to_sql(
                TABLE, conn,
                if_exists="replace" if first_chunk else "append",
                index=False,
            )
            offset += len(chunk)
            first_chunk = False
            logger.info(f"  ...inserted {offset:,} rows so far")

        if first_chunk:
            raise ValueError("CSV appears to be empty — no rows were read.")

        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS idx__row_id ON {TABLE}("_row_id")')
        conn.commit()
        _build_distribution_table(conn)
    finally:
        conn.close()
    ensure_indexes(tmp_db_path)

    # Atomic swap. On Windows it fails while a request has the old DB open,
    # so retry briefly — requests only hold it for milliseconds.
    for attempt in range(30):
        try:
            os.replace(tmp_db_path, settings.db_path)
            break
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(1)
    logger.info(f"SQLite DB ready at {settings.db_path} ({offset:,} rows).")


def _build_distribution_table(conn: sqlite3.Connection) -> None:
    """
    Loads wcvp_distribution.csv into its own table in the same DB file, indexed
    on plant_name_id so each page of results needs only one extra lookup.
    Optional: if the file isn't there, the API runs without distribution data.
    """
    if not os.path.exists(settings.distribution_csv_path):
        logger.warning(
            f"No distribution file at {settings.distribution_csv_path} — "
            "results will be served without distribution data."
        )
        return

    logger.info(f"Loading distribution data from {settings.distribution_csv_path} ...")
    rows = 0
    reader = pd.read_csv(
        settings.distribution_csv_path,
        delimiter=settings.csv_delimiter,
        dtype=str,
        keep_default_na=False,
        low_memory=False,
        chunksize=settings.build_chunk_size,
    )
    for i, chunk in enumerate(reader):
        chunk.to_sql(DIST_TABLE, conn, if_exists="replace" if i == 0 else "append", index=False)
        rows += len(chunk)
        logger.info(f"  ...inserted {rows:,} distribution rows so far")

    conn.execute(f'CREATE INDEX IF NOT EXISTS idx_dist_plant_name_id ON {DIST_TABLE}("plant_name_id")')
    conn.commit()


def ensure_indexes(db_path: Optional[str] = None) -> None:
    """
    Creates any missing indexes on the live DB. Cheap when they already exist,
    so it runs on every startup — an older DB picks up new indexes without
    a full rebuild.
    - Name columns get NOCASE indexes, matching the case-insensitive
      `= ? COLLATE NOCASE` lookups in /search and /filter.
    - ipni_id finds the plant for /distribution/{ipni_id}.
    - INDEX_COLUMNS (optional) adds plain indexes on anything else.
    The build also calls this on its temp file, so a fresh DB is swapped in
    with every index already in place.
    """
    conn = sqlite3.connect(db_path or settings.db_path)
    try:
        actual_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}
        extra_cols = [c.strip() for c in settings.index_columns.split(",") if c.strip()]
        wanted = [(c, "NOCASE") for c in NAME_COLUMNS] + [("ipni_id", None)] \
            + [(c, None) for c in extra_cols if c not in NAME_COLUMNS]
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )}
        for col, collation in wanted:
            if col not in actual_cols or not _valid_identifier(col):
                logger.warning(f"Skipping index on unknown/invalid column: {col}")
                continue
            name = f"idx_{col}_nocase" if collation else f"idx_{col}"
            if name in existing:
                continue
            logger.info(f"  ...building index {name}")
            collate_sql = f" COLLATE {collation}" if collation else ""
            conn.execute(f'CREATE INDEX {name} ON {TABLE}("{col}"{collate_sql})')
        conn.commit()
    finally:
        conn.close()


def db_has_distribution() -> bool:
    if not os.path.exists(settings.db_path):
        return False
    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (DIST_TABLE,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def read_columns_from_db() -> list:
    conn = sqlite3.connect(settings.db_path)
    try:
        cur = conn.execute(f"PRAGMA table_info({TABLE})")
        return [row[1] for row in cur.fetchall() if row[1] != "_row_id"]
    finally:
        conn.close()


# --- Startup / refresh orchestration ----------------------------------------

def _refresh_locked(force_download: bool = False, force_rebuild: bool = False) -> None:
    """The readiness flow itself; callers must hold both locks."""
    global _last_ready_at, _columns, _has_distribution

    missing_csvs = [
        p for p in (settings.csv_path, settings.distribution_csv_path) if not os.path.exists(p)
    ]
    downloaded = False
    if force_download or settings.force_download or missing_csvs:
        download_and_extract_zip()
        downloaded = True
    else:
        logger.info(f"Using existing CSVs at {settings.csv_path} (no download needed).")

    need_rebuild = (
        downloaded or force_rebuild or settings.force_rebuild
        or not os.path.exists(settings.db_path) or not db_has_distribution()
    )
    if need_rebuild:
        build_sqlite_from_csv()
    else:
        logger.info(f"Using existing SQLite DB at {settings.db_path} (no rebuild needed).")

    ensure_indexes()
    _columns = read_columns_from_db()
    _has_distribution = db_has_distribution()
    _last_ready_at = datetime.now(timezone.utc)


def ensure_data_ready(force_download: bool = False, force_rebuild: bool = False) -> None:
    """
    Core readiness flow, shared by startup and /reload.
    - Downloads only if a CSV is missing or explicitly forced.
    - Rebuilds SQLite only if needed (missing DB, new download) or explicitly forced.
    A plain process restart with existing CSVs + existing DB does neither —
    it just starts serving immediately.
    """
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    with _build_lock, _process_lock:
        _refresh_locked(force_download, force_rebuild)


def check_for_update() -> dict:
    """
    Compares Kew's current zip with the version we last downloaded and, only
    if it changed, downloads, extracts and rebuilds. The comparison happens
    inside the locks, so when several workers check at once only the first
    one downloads — the others then see the version it saved.
    """
    global _last_update_check
    remote = fetch_remote_version()
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    with _build_lock, _process_lock:
        local = read_local_version()
        updated = not _same_version(remote, local)
        if updated:
            logger.info(
                f"New WCVP release (Last-Modified {remote['last_modified']}, "
                f"had {local.get('last_modified') if local else 'unknown'}) - updating."
            )
            _refresh_locked(force_download=True, force_rebuild=True)
        else:
            logger.info(f"WCVP zip unchanged (Last-Modified {remote['last_modified']}).")
    _last_update_check = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "remote": remote,
        "updated": updated,
    }
    return _last_update_check


def _update_loop() -> None:
    """Checks once right after startup, then every UPDATE_CHECK_HOURS."""
    while True:
        try:
            check_for_update()
        except Exception:
            logger.exception("WCVP update check failed; will retry at the next interval.")
        if _stop_updates.wait(settings.update_check_hours * 3600):
            return


@app.on_event("startup")
def startup_event():
    ensure_data_ready()
    if settings.update_check_enabled:
        threading.Thread(target=_update_loop, name="wcvp-update-check", daemon=True).start()


@app.on_event("shutdown")
def shutdown_event():
    _stop_updates.set()


def get_connection() -> sqlite3.Connection:
    if not os.path.exists(settings.db_path):
        raise HTTPException(status_code=503, detail="Database not ready yet.")
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# --- Distribution -----------------------------------------------------------

def _format_distribution(rows: list) -> dict:
    native, introduced = [], []
    for row in rows:
        area = {
            "continent_code_l1": row["continent_code_l1"],
            "continent": row["continent"],
            "region_code_l2": row["region_code_l2"],
            "region": row["region"],
            "area_code_l3": row["area_code_l3"],
            "area": row["area"],
            "extinct": row["extinct"] == "1",
            "location_doubtful": row["location_doubtful"] == "1",
        }
        (introduced if row["introduced"] == "1" else native).append(area)
    return {"native": native, "introduced": introduced}


# --- Name search helpers ------------------------------------------------------

# Within the same match level, a hit on the full name beats one on the family.
COLUMN_PRIORITY = {"taxon_name": 0, "genus": 1, "species": 2, "infraspecies": 3, "family": 4}


def _match_rank_sql(col: str) -> str:
    """
    Lower is better. Tens = how well it matches (0 whole field, 1 starts with,
    2 contains); units = which column matched (COLUMN_PRIORITY). 99 = no match.
    """
    p = COLUMN_PRIORITY[col]
    return (
        f'CASE WHEN "{col}" = ? COLLATE NOCASE THEN {p} '
        f'WHEN "{col}" LIKE ? ESCAPE \'\\\' THEN {10 + p} '
        f'WHEN "{col}" LIKE ? ESCAPE \'\\\' THEN {20 + p} ELSE 99 END'
    )


# Best match first; then accepted names before synonyms; then the shortest
# name, so searching "Rosa" puts the genus itself ahead of "Rosa canina".
ORDER_SQL = (
    "ORDER BY _rank, "
    "CASE WHEN \"taxon_status\" = 'Accepted' THEN 0 ELSE 1 END, "
    "LENGTH(\"taxon_name\"), \"taxon_name\""
)


def _page_response(rows: list, total: int, page: int, **extra) -> dict:
    results = []
    for row in rows:
        r = dict(row)
        for key in ("_row_id", "_rank", "_total"):
            r.pop(key, None)
        results.append(r)
    return {
        **extra,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
        "results": results,
    }


def _parse_name_columns(columns: Optional[str]) -> list:
    if not columns:
        return [c for c in NAME_COLUMNS if c in _columns]
    cols = [c.strip() for c in columns.split(",") if c.strip()]
    invalid = [c for c in cols if c not in NAME_COLUMNS or c not in _columns]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported columns: {invalid}. Choose from {NAME_COLUMNS}.",
        )
    return cols


# --- Endpoints ------------------------------------------------------------

@app.get("/health")
def health():
    db_size_mb = round(os.path.getsize(settings.db_path) / (1024 * 1024), 1) if os.path.exists(settings.db_path) else None
    row_count = None
    if os.path.exists(settings.db_path):
        conn = get_connection()
        try:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        finally:
            conn.close()
    return {
        "status": "ok",
        "db_size_mb": db_size_mb,
        "row_count": row_count,
        "distribution_loaded": _has_distribution,
        "ready_at": _last_ready_at.isoformat() if _last_ready_at else None,
        "data_version": (read_local_version() or {}).get("last_modified"),
    }


@app.get("/columns")
def columns():
    """Inspect actual column names from your CSV."""
    return {"columns": _columns, "searchable_columns": NAME_COLUMNS}


@app.get("/source")
def source():
    """Where the data comes from, which release is loaded, and the last update check."""
    return {
        "zip_url": settings.wcvp_zip_url,
        "loaded_version": read_local_version(),
        "update_check_enabled": settings.update_check_enabled,
        "update_check_hours": settings.update_check_hours,
        "last_check": _last_update_check,
    }


def _require_reload_token(x_reload_token: Optional[str]) -> None:
    if settings.reload_token and x_reload_token != settings.reload_token:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Reload-Token header.")


@app.post("/check-update")
def check_update(x_reload_token: Optional[str] = Header(None)):
    """
    Runs the daily update check now: downloads and rebuilds only if Kew has
    published a newer wcvp.zip than the one loaded.
    """
    _require_reload_token(x_reload_token)
    try:
        return check_for_update()
    except Exception as e:
        logger.exception("Update check failed")
        raise HTTPException(status_code=502, detail=f"Update check failed: {e}")


@app.post("/reload")
def reload_data(x_reload_token: Optional[str] = Header(None)):
    """
    Explicit refresh: always re-downloads wcvp.zip and always rebuilds the
    SQLite database, even if Kew's file hasn't changed — this endpoint IS
    the "force" trigger. Use /check-update to download only when newer.
    """
    _require_reload_token(x_reload_token)

    try:
        ensure_data_ready(force_download=True, force_rebuild=True)
    except Exception as e:
        logger.exception("Reload failed")
        raise HTTPException(status_code=500, detail=f"Reload failed: {e}")

    return {"status": "reloaded", "ready_at": _last_ready_at.isoformat()}


@app.get("/records/{row_id}")
def get_record(row_id: int):
    conn = get_connection()
    try:
        row = conn.execute(f'SELECT * FROM {TABLE} WHERE "_row_id" = ?', (row_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Record not found.")
    result = dict(row)
    result.pop("_row_id", None)
    return result


@app.get("/distribution/{ipni_id}")
def get_distribution(ipni_id: str):
    """
    Native and introduced distribution for a plant, by the ipni_id returned
    from /search or /filter. WCVP only records distribution against accepted
    names, so a synonym returns the distribution of its accepted name
    (see accepted_plant_name_id).
    """
    if not _has_distribution:
        raise HTTPException(status_code=503, detail="Distribution data is not loaded.")
    conn = get_connection()
    try:
        # An ipni_id is normally unique; if not, prefer the accepted name.
        record = conn.execute(
            f'SELECT * FROM {TABLE} WHERE "ipni_id" = ? '
            "ORDER BY CASE WHEN \"taxon_status\" = 'Accepted' THEN 0 ELSE 1 END LIMIT 1",
            (ipni_id,),
        ).fetchone()
        if record is None:
            raise HTTPException(status_code=404, detail="No plant with that ipni_id.")
        lookup_id = record["accepted_plant_name_id"] or record["plant_name_id"]
        accepted = conn.execute(
            f'SELECT "taxon_name" FROM {TABLE} WHERE "plant_name_id" = ?', (lookup_id,)
        ).fetchone()
        rows = conn.execute(
            f'SELECT * FROM {DIST_TABLE} WHERE "plant_name_id" = ? ORDER BY rowid', (lookup_id,)
        ).fetchall()
    finally:
        conn.close()
    return {
        "ipni_id": ipni_id,
        "taxon_name": record["taxon_name"],
        "taxon_authors": record["taxon_authors"],
        "taxon_status": record["taxon_status"],
        "plant_name_id": record["plant_name_id"],
        "accepted_plant_name_id": lookup_id,
        "accepted_taxon_name": accepted["taxon_name"] if accepted else None,
        "distribution": _format_distribution(rows),
    }


@app.get("/search")
def search(
    q: str = Query(..., min_length=1, description="Name to search for, e.g. 'Rosa' or 'Lantana camara'"),
    columns: Optional[str] = Query(
        None,
        description="Comma-separated subset of genus, family, species, infraspecies, "
                    "taxon_name. Defaults to all five.",
    ),
    exact: bool = Query(False, description="If true, only whole-field matches (fast, uses indexes)."),
    page: int = Query(1, ge=1),
):
    """
    Searches plant names, best matches first: a whole-field match beats a
    starts-with match, which beats a contains match. 10 results per page.
    """
    search_cols = _parse_name_columns(columns)
    q = q.strip()
    starts, contains = f"{_escape_like(q)}%", f"%{_escape_like(q)}%"

    if exact:
        where_sql = " OR ".join(f'"{c}" = ? COLLATE NOCASE' for c in search_cols)
        where_params = [q] * len(search_cols)
    else:
        where_sql = " OR ".join(f'"{c}" LIKE ? ESCAPE \'\\\'' for c in search_cols)
        where_params = [contains] * len(search_cols)
    ranks = [_match_rank_sql(c) for c in search_cols]
    # SQLite's multi-argument MIN is scalar; with one argument it's the aggregate.
    rank_sql = ranks[0] if len(ranks) == 1 else f"MIN({', '.join(ranks)})"
    rank_params = [q, starts, contains] * len(search_cols)

    conn = get_connection()
    try:
        # COUNT(*) OVER () gives the total in the same pass as the page.
        rows = conn.execute(
            f"SELECT *, {rank_sql} AS _rank, COUNT(*) OVER () AS _total "
            f"FROM {TABLE} WHERE {where_sql} {ORDER_SQL} LIMIT ? OFFSET ?",
            rank_params + where_params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        if rows:
            total = rows[0]["_total"]
        else:  # past the last page: still report the real total
            total = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE {where_sql}", where_params
            ).fetchone()[0]
    finally:
        conn.close()

    return _page_response(rows, total, page, query=q, searched_columns=search_cols)


@app.get("/filter")
def filter_exact(
    column: str = Query(..., description="One of genus, family, species, infraspecies, taxon_name"),
    value: str = Query(..., min_length=1, description="Exact value to match (case-insensitive)"),
    page: int = Query(1, ge=1),
):
    """
    Exact-match filter on one name column, e.g. /filter?column=family&value=Orchidaceae.
    Accepted names first. 10 results per page.
    """
    col = _parse_name_columns(column)
    if len(col) != 1:
        raise HTTPException(status_code=400, detail="Pass exactly one column.")
    col = col[0]

    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT *, 0 AS _rank, COUNT(*) OVER () AS _total FROM {TABLE} "
            f'WHERE "{col}" = ? COLLATE NOCASE {ORDER_SQL} LIMIT ? OFFSET ?',
            (value, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
        if rows:
            total = rows[0]["_total"]
        else:
            total = conn.execute(
                f'SELECT COUNT(*) FROM {TABLE} WHERE "{col}" = ? COLLATE NOCASE', (value,)
            ).fetchone()[0]
    finally:
        conn.close()

    return _page_response(rows, total, page, column=col, value=value)
