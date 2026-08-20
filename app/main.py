"""
WCVP / POWO CSV-backed API
--------------------------
Serves a large CSV (e.g. wcvp_names.csv) as a searchable, paginated REST API
with no database. To update the data: replace the CSV file and call POST /reload
(or restart the process) — no re-import step required.
"""

import logging
import math
import threading
from typing import Optional

import pandas as pd
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


@app.on_event("startup")
def startup_event():
    with _data_lock:
        df = load_dataframe()
        _state["df"] = df
        _state["columns"] = [c for c in df.columns if c != "_row_id"]
        _state["row_count"] = len(df)


def get_df() -> pd.DataFrame:
    if _state["df"] is None:
        raise HTTPException(status_code=503, detail="Data not loaded yet.")
    return _state["df"]


# --- Endpoints ------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "rows_loaded": _state["row_count"]}


@app.get("/columns")
def columns():
    """Inspect actual column names from your CSV — use these in ?columns= and filters."""
    return {"columns": _state["columns"]}


@app.post("/reload")
def reload_data(x_reload_token: Optional[str] = Header(None)):
    """
    Reload the CSV from disk without restarting the process.
    Swap in the new wcvp_names.csv at settings.csv_path, then call this.
    """
    if settings.reload_token:
        if x_reload_token != settings.reload_token:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Reload-Token header.")

    with _data_lock:
        df = load_dataframe()
        _state["df"] = df
        _state["columns"] = [c for c in df.columns if c != "_row_id"]
        _state["row_count"] = len(df)

    return {"status": "reloaded", "rows_loaded": _state["row_count"]}


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
