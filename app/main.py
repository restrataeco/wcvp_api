"""
WCVP / POWO CSV-backed API
--------------------------
Serves a large CSV (e.g. wcvp_names.csv) as a searchable, paginated REST API
with no database.

Two ways to supply the data:
  1. Local file only: leave CSV_SOURCE_URL blank in .env, put the CSV at
     CSV_PATH on disk yourself (e.g. via scp on Hetzner).
  2. Public URL: set CSV_SOURCE_URL to a direct-download HTTPS link. On every
     startup and every POST /reload, the app downloads that URL to CSV_PATH
     and loads it from there. This is what makes it work on platforms with
     ephemeral/reset filesystems (e.g. Render without a persistent disk).
"""

import logging
import math
import os
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
    description="Read-only API over a POWO/WCVP CSV export.",
    version="1.0.0",
)

# Adjust origins for production if this is called from a browser frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- In-memory data store -----------------------------------------------
# Guarded by a lock so a /reload can't race with an in-flight request reading
# a half-swapped dataframe reference. Loading is fast (seconds) even at ~300MB.
_data_lock = threading.RLock()
_state = {"df": None, "columns": [], "row_count": 0}
_last_loaded_at: Optional[datetime] = None


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
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)

    os.replace(tmp_path, settings.csv_path)  # atomic on same filesystem
    logger.info(f"Downloaded to {settings.csv_path}")

def load_dataframe() -> pd.DataFrame:
    logger.info(f"Loading CSV from {settings.csv_path} ...")
    df = pd.read_csv(
        settings.csv_path,
        delimiter=settings.csv_delimiter,
        dtype=str,          # keep everything as text; avoids pandas guessing types
                             # inconsistently across a huge file. Cast per-field later if needed.
        keep_default_na=False,
        low_memory=False,
    )
    df.reset_index(drop=True, inplace=True)
    df["_row_id"] = df.index  # stable id used for GET /records/{id}
    logger.info(f"Loaded {len(df):,} rows, {len(df.columns)} columns.")
    logger.info(f"Columns: {list(df.columns)}")
    return df


def refresh_data():
    """Full refresh cycle: download from URL if configured, then load into memory."""
    global _last_loaded_at
    if settings.csv_source_url:
        download_csv_from_url()
    df = load_dataframe()
    _state["df"] = df
    _state["columns"] = [c for c in df.columns if c != "_row_id"]
    _state["row_count"] = len(df)
    _last_loaded_at = datetime.now(timezone.utc)


@app.on_event("startup")
def startup_event():
    with _data_lock:
        refresh_data()


def get_df() -> pd.DataFrame:
    if _state["df"] is None:
        raise HTTPException(status_code=503, detail="Data not loaded yet.")
    return _state["df"]


# --- Endpoints ------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "rows_loaded": _state["row_count"],
        "loaded_at": _last_loaded_at.isoformat() if _last_loaded_at else None,
        "source": "url" if settings.csv_source_url else "local",
    }


@app.get("/columns")
def columns():
    """Inspect actual column names from your CSV — use these in ?columns= and filters."""
    return {"columns": _state["columns"]}


@app.post("/reload")
def reload_data(x_reload_token: Optional[str] = Header(None)):
    """
    Reload the data without restarting the process.
    - If CSV_SOURCE_URL is set: re-downloads from that URL, then reloads.
    - Otherwise: re-reads csv_path directly off local disk (replace the file
      at CSV_PATH yourself first, e.g. via scp, then call this).
    """
    if settings.reload_token:
        if x_reload_token != settings.reload_token:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Reload-Token header.")

    with _data_lock:
        try:
            refresh_data()
        except Exception as e:
            logger.exception("Reload failed")
            raise HTTPException(status_code=500, detail=f"Reload failed: {e}")

    return {
        "status": "reloaded",
        "rows_loaded": _state["row_count"],
        "loaded_at": _last_loaded_at.isoformat(),
    }


@app.get("/records/{row_id}")
def get_record(row_id: int):
    df = get_df()
    match = df[df["_row_id"] == row_id]
    if match.empty:
        raise HTTPException(status_code=404, detail="Record not found.")
    return match.drop(columns=["_row_id"]).iloc[0].to_dict()


@app.get("/search")
def search(
    q: Optional[str] = Query(None, description="Keyword to search for, e.g. 'Lem'"),
    columns: Optional[str] = Query(
        None,
        description="Comma-separated column names to search in. Defaults to config, "
                    "or all text columns if not configured. See /columns for options.",
    ),
    exact: bool = Query(False, description="If true, match the whole field exactly instead of substring."),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.default_page_size, ge=1),
):
    df = get_df()
    page_size = min(page_size, settings.max_page_size)

    search_cols = None
    if columns:
        search_cols = [c.strip() for c in columns.split(",") if c.strip()]
    elif settings.default_search_columns:
        search_cols = [c.strip() for c in settings.default_search_columns.split(",") if c.strip()]

    if search_cols:
        missing = [c for c in search_cols if c not in df.columns]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown columns: {missing}. See /columns.")
    else:
        search_cols = [c for c in df.columns if c != "_row_id"]

    result = df
    if q:
        mask = pd.Series(False, index=df.index)
        for col in search_cols:
            if exact:
                mask |= df[col].str.lower() == q.lower()
            else:
                mask |= df[col].str.contains(q, case=False, na=False, regex=False)
        result = df[mask]

    total = len(result)
    start = (page - 1) * page_size
    end = start + page_size
    page_df = result.iloc[start:end].drop(columns=["_row_id"], errors="ignore")

    return {
        "query": q,
        "searched_columns": search_cols,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if page_size else 0,
        "results": page_df.to_dict(orient="records"),
    }


@app.get("/filter")
def filter_exact(
    column: str = Query(..., description="Column name to filter on, e.g. 'family' or 'genus'"),
    value: str = Query(..., description="Exact value to match (case-insensitive)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(settings.default_page_size, ge=1),
):
    """Exact-match filter on a single column, e.g. /filter?column=family&value=Orchidaceae"""
    df = get_df()
    if column not in df.columns:
        raise HTTPException(status_code=400, detail=f"Unknown column '{column}'. See /columns.")

    page_size = min(page_size, settings.max_page_size)
    result = df[df[column].str.lower() == value.lower()]

    total = len(result)
    start = (page - 1) * page_size
    end = start + page_size
    page_df = result.iloc[start:end].drop(columns=["_row_id"], errors="ignore")

    return {
        "column": column,
        "value": value,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if page_size else 0,
        "results": page_df.to_dict(orient="records"),
    }