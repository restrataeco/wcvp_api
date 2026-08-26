"""
WCVP / POWO CSV-backed API
--------------------------
Serves a large CSV (e.g. wcvp_names.csv) as a searchable, paginated REST API,
backed by a local SQLite file built from the CSV — not an in-memory
dataframe, and not a re-scan of the CSV on every request.

Startup flow:
  1. If CSV_SOURCE_URL is set and no local CSV exists yet (or FORCE_DOWNLOAD
     is true): download it. Otherwise use whatever CSV is already at CSV_PATH.
  2. If no SQLite DB exists yet at DB_PATH (or FORCE_REBUILD is true): build
     one from the CSV, with indexes on the configured columns. Otherwise skip
     straight to serving — a plain restart does NOT re-download or rebuild.
  3. POST /reload always does both steps, regardless of the flags above —
     that's the explicit "refresh now" trigger.

Once built, every request queries the SQLite file directly with indexed SQL —
fast, low memory, and gives back exact total counts (no more "total: null").
"""

import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware

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

TABLE = "records"
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")  # guards against SQL injection via column names


def _valid_identifier(name: str) -> bool:
    return bool(IDENTIFIER_RE.match(name))


def _escape_like(value: str) -> str:
    """Escape % _ and \\ so user input can't inject LIKE wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Download ---------------------------------------------------------------

def download_csv_from_url() -> None:
    """
    Download the CSV from settings.csv_source_url to settings.csv_path.
    Streams to a .tmp file first and renames into place, so a request that
    races with a slow download never reads a half-written file.

    Google Drive share links need special handling: Drive shows an
    HTML "can't scan for viruses" interstitial for large files instead of
    serving the raw file, so a plain HTTP GET would download that warning
    page instead of your CSV. gdown handles Drive's confirmation token
    automatically and accepts the normal share URL as-is (fuzzy=True).
    """
    os.makedirs(os.path.dirname(settings.csv_path) or ".", exist_ok=True)
    tmp_path = settings.csv_path + ".tmp"

    if "drive.google.com" in settings.csv_source_url:
        import gdown  # only needed for this path; keeps requests-only setups lighter
        logger.info(f"Downloading from Google Drive: {settings.csv_source_url}")
        gdown.download(url=settings.csv_source_url, output=tmp_path, quiet=False, fuzzy=True)
    else:
        logger.info(f"Downloading CSV from {settings.csv_source_url} ...")
        with requests.get(settings.csv_source_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):  # 8MB chunks
                    if chunk:
                        f.write(chunk)

    os.replace(tmp_path, settings.csv_path)  # atomic on same filesystem
    logger.info(f"Downloaded to {settings.csv_path}")


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

        cur = conn.cursor()
        cur.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS idx__row_id ON {TABLE}("_row_id")')

        index_cols = [c.strip() for c in settings.index_columns.split(",") if c.strip()] \
            or [c.strip() for c in settings.default_search_columns.split(",") if c.strip()]
        cur.execute(f"PRAGMA table_info({TABLE})")
        actual_cols = {row[1] for row in cur.fetchall()}
        for col in index_cols:
            if col not in actual_cols or not _valid_identifier(col):
                logger.warning(f"Skipping index on unknown/invalid column: {col}")
                continue
            logger.info(f"  ...building index on {col}")
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{col} ON {TABLE}("{col}")')

        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_db_path, settings.db_path)  # atomic swap
    logger.info(f"SQLite DB ready at {settings.db_path} ({offset:,} rows).")


def read_columns_from_db() -> list:
    conn = sqlite3.connect(settings.db_path)
    try:
        cur = conn.execute(f"PRAGMA table_info({TABLE})")
        return [row[1] for row in cur.fetchall() if row[1] != "_row_id"]
    finally:
        conn.close()


# --- Startup / refresh orchestration ----------------------------------------

def ensure_data_ready(force_download: bool = False, force_rebuild: bool = False) -> None:
    """
    Core readiness flow, shared by startup and /reload.
    - Downloads only if needed (missing file) or explicitly forced.
    - Rebuilds SQLite only if needed (missing DB) or explicitly forced.
    A plain process restart with an existing CSV + existing DB does neither —
    it just starts serving immediately.
    """
    global _last_ready_at, _columns

    with _build_lock:
        need_download = (
            settings.csv_source_url
            and (force_download or settings.force_download or not os.path.exists(settings.csv_path))
        )
        if need_download:
            download_csv_from_url()
        elif not os.path.exists(settings.csv_path):
            raise FileNotFoundError(
                f"No file at {settings.csv_path} and no CSV_SOURCE_URL configured to fetch one."
            )
        else:
            logger.info(f"Using existing CSV at {settings.csv_path} (no download needed).")

        need_rebuild = force_rebuild or settings.force_rebuild or not os.path.exists(settings.db_path)
        if need_rebuild:
            build_sqlite_from_csv()
        else:
            logger.info(f"Using existing SQLite DB at {settings.db_path} (no rebuild needed).")

        _columns = read_columns_from_db()
        _last_ready_at = datetime.now(timezone.utc)


@app.on_event("startup")
def startup_event():
    ensure_data_ready()


def get_connection() -> sqlite3.Connection:
    if not os.path.exists(settings.db_path):
        raise HTTPException(status_code=503, detail="Database not ready yet.")
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


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
        "ready_at": _last_ready_at.isoformat() if _last_ready_at else None,
        "source": "url" if settings.csv_source_url else "local",
    }


@app.get("/columns")
def columns():
    """Inspect actual column names from your CSV — use these in ?columns= and filters."""
    return {"columns": _columns}


@app.post("/reload")
def reload_data(x_reload_token: Optional[str] = Header(None)):
    """
    Explicit refresh: always re-downloads (if CSV_SOURCE_URL is set) and
    always rebuilds the SQLite database, regardless of FORCE_DOWNLOAD/
    FORCE_REBUILD settings — this endpoint IS the "force" trigger.
    """
    if settings.reload_token:
        if x_reload_token != settings.reload_token:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Reload-Token header.")

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


@app.get("/search")
def search(
    q: Optional[str] = Query(None, description="Keyword to search for, e.g. 'Lem'"),
    columns: Optional[str] = Query(
        None,
        description="Comma-separated column names to search in. Defaults to config, "
                    "or all columns if not configured. See /columns for options.",
    ),
    exact: bool = Query(False, description="If true, match the whole field exactly instead of substring."),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.default_page_size, ge=1),
):
    page_size = min(page_size, settings.max_page_size)

    search_cols = None
    if columns:
        search_cols = [c.strip() for c in columns.split(",") if c.strip()]
    elif settings.default_search_columns:
        search_cols = [c.strip() for c in settings.default_search_columns.split(",") if c.strip()]
    if search_cols:
        missing = [c for c in search_cols if c not in _columns or not _valid_identifier(c)]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown columns: {missing}. See /columns.")
    else:
        search_cols = _columns

    conn = get_connection()
    try:
        params = []
        where_sql = ""
        if q:
            if exact:
                clauses = [f'LOWER("{c}") = ?' for c in search_cols]
                params = [q.lower()] * len(search_cols)
            else:
                clauses = [f'"{c}" LIKE ? ESCAPE \'\\\'' for c in search_cols]
                params = [f"%{_escape_like(q)}%"] * len(search_cols)
            where_sql = "WHERE " + " OR ".join(clauses)

        total = conn.execute(f"SELECT COUNT(*) FROM {TABLE} {where_sql}", params).fetchone()[0]

        offset = (page - 1) * page_size
        rows = conn.execute(
            f'SELECT * FROM {TABLE} {where_sql} LIMIT ? OFFSET ?',
            params + [page_size, offset],
        ).fetchall()
    finally:
        conn.close()

    results = [dict(r) for r in rows]
    for r in results:
        r.pop("_row_id", None)

    return {
        "query": q,
        "searched_columns": search_cols,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size else 0,
        "results": results,
    }


@app.get("/filter")
def filter_exact(
    column: str = Query(..., description="Column name to filter on, e.g. 'family' or 'genus'"),
    value: str = Query(..., description="Exact value to match (case-insensitive)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.default_page_size, ge=1),
):
    """Exact-match filter on a single column, e.g. /filter?column=family&value=Orchidaceae"""
    if column not in _columns or not _valid_identifier(column):
        raise HTTPException(status_code=400, detail=f"Unknown column '{column}'. See /columns.")

    page_size = min(page_size, settings.max_page_size)
    offset = (page - 1) * page_size

    conn = get_connection()
    try:
        total = conn.execute(
            f'SELECT COUNT(*) FROM {TABLE} WHERE LOWER("{column}") = ?', (value.lower(),)
        ).fetchone()[0]
        rows = conn.execute(
            f'SELECT * FROM {TABLE} WHERE LOWER("{column}") = ? LIMIT ? OFFSET ?',
            (value.lower(), page_size, offset),
        ).fetchall()
    finally:
        conn.close()

    results = [dict(r) for r in rows]
    for r in results:
        r.pop("_row_id", None)

    return {
        "column": column,
        "value": value,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size else 0,
        "results": results,
    }