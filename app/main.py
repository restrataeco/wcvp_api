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

Anything that goes wrong along the way (Kew unreachable or moved, zip/CSV
layout changed, missing library, permissions, disk space) is logged and shown
on /health and the Swagger page — see app/monitoring.py. The app keeps serving
the last good data whenever it has some.
"""

from __future__ import annotations  # annotations mention sqlite3 even if it's missing

import errno
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app import monitoring
from app.config import settings
from app.monitoring import ERROR, INFO, WARNING, optional_import

# Libraries the server might lack. The app still starts without them and
# /health explains what's missing, instead of failing with an ImportError.
sqlite3 = optional_import("sqlite3")
pd = optional_import("pandas")
requests = optional_import("requests")
filelock = optional_import("filelock")
optional_import("zlib")  # zipfile needs it to extract wcvp.zip

monitoring.setup_logging(settings.log_path, settings.log_max_mb, settings.log_backup_count)
logger = logging.getLogger("wcvp_api")

BASE_DESCRIPTION = "Read-only API over Kew's World Checklist of Vascular Plants, backed by SQLite."

app = FastAPI(title="WCVP Data API", description=BASE_DESCRIPTION, version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _openapi_with_warnings() -> dict:
    """Rebuilt on every /openapi.json request, so the Swagger page always shows
    the current warnings at the top."""
    return get_openapi(
        title=app.title,
        version=app.version,
        description=monitoring.description_with_warnings(BASE_DESCRIPTION),
        routes=app.routes,
    )


app.openapi = _openapi_with_warnings

_build_lock = threading.Lock()
_last_ready_at: Optional[datetime] = None
_columns: list = []
_has_distribution: bool = False
_last_update_check: Optional[dict] = None
_stop_updates = threading.Event()

# gunicorn runs several worker processes; this OS-level lock keeps them from
# downloading or building at the same time. The OS releases it if a worker dies.
# Without filelock, fall back to an in-process lock (fine for a single worker).
# The timeout is a safety net: another worker's build takes minutes, not hours.
_process_lock = (
    filelock.FileLock(settings.db_path + ".lock", timeout=3 * 3600) if filelock else threading.Lock()
)

# Setting holding each expected zip member's name -> setting saying where it goes.
ZIP_MEMBERS = {"zip_names_member": "csv_path", "zip_distribution_member": "distribution_csv_path"}

# Columns the API relies on; a new release missing any of them is not loaded.
REQUIRED_COLUMNS = {
    "zip_names_member": [
        "plant_name_id", "ipni_id", "taxon_rank", "taxon_status", "family", "genus",
        "species", "infraspecies", "taxon_name", "taxon_authors", "accepted_plant_name_id",
    ],
    "zip_distribution_member": [
        "plant_name_id", "continent_code_l1", "continent", "region_code_l2", "region",
        "area_code_l3", "area", "introduced", "extinct", "location_doubtful",
    ],
}

# Warnings about one download/extract/build attempt; cleared when one succeeds.
REFRESH_CODES = (
    "zip_corrupt", "zip_member_missing", "csv_format_changed", "permission_denied",
    "disk_full", "sqlite_error", "update_failed", "data_unavailable",
)
SOURCE_CODES = ("source_unreachable", "source_not_found", "source_not_zip")

TABLE = "records"
DIST_TABLE = "distribution"

# /search and /filter only look at these name columns, and return a fixed
# page of results.
NAME_COLUMNS = ["genus", "family", "species", "infraspecies", "taxon_name"]
PAGE_SIZE = 10
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")  # guards against SQL injection via column names


class SourceError(Exception):
    """A problem with Kew's zip or its contents, with a warning code for /health."""

    def __init__(self, code: str, message: str, level: str = ERROR, **detail):
        super().__init__(message)
        self.code, self.message, self.level, self.detail = code, message, level, detail


def _valid_identifier(name: str) -> bool:
    return bool(IDENTIFIER_RE.match(name))


def _escape_like(value: str) -> str:
    """Escape % _ and \\ so user input can't inject LIKE wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require(lib, name: str) -> None:
    if lib is None:
        # Same code as the startup environment check, so it updates that warning.
        raise SourceError(
            f"library_missing_{name}",
            f"Python library '{name}' is not available; it's needed for "
            f"{monitoring.LIBRARY_PURPOSE[name]}. Install it (pip install -r requirements.txt) "
            "and restart.",
        )


def _serving_note() -> str:
    """Appended to warnings, so readers know what users are getting meanwhile."""
    if os.path.exists(settings.db_path):
        version = (read_local_version() or {}).get("last_modified")
        return f" Still serving the previous data ({version or 'release date unknown'})."
    return " No data is loaded yet, so the API can't answer queries."


def _check_data_folder_writable() -> None:
    """
    Fails fast with a clear warning if the data folder is read-only. Without
    this, filelock can't create its lock file there and would retry until
    its timeout, hanging startup instead of reporting the problem.
    """
    data_dir = os.path.dirname(settings.db_path) or "."
    problem = monitoring.writable_dir_problem(data_dir)
    if problem:
        raise SourceError(
            f"folder_not_writable:{data_dir}",
            f"Can't write to {os.path.abspath(data_dir)} ({problem}). Downloading, extracting "
            "and rebuilding the data will fail. Give the app's user write access.",
        )


def _kew_folder() -> str:
    return settings.wcvp_zip_url.rsplit("/", 1)[0] + "/"


# --- Source: Kew's wcvp.zip ---------------------------------------------------

def _version_from_headers(headers) -> dict:
    return {
        "last_modified": headers.get("Last-Modified"),
        "etag": headers.get("ETag"),
        "content_length": headers.get("Content-Length"),
    }


def _check_response(r) -> None:
    """Turns a bad response from Kew into a SourceError with a clear reason."""
    url = settings.wcvp_zip_url
    if r.status_code in (404, 410):
        raise SourceError(
            "source_not_found",
            f"{url} returned HTTP {r.status_code}: Kew may have moved or renamed wcvp.zip. "
            f"Check {_kew_folder()} and set WCVP_ZIP_URL to the new link.",
            status=r.status_code,
        )
    if r.status_code >= 400:
        raise SourceError(
            "source_unreachable", f"{url} returned HTTP {r.status_code}.", status=r.status_code
        )
    if r.history and r.url != url:
        monitoring.warn(
            "source_redirected",
            f"{url} now redirects to {r.url}. It still works, but Kew may have moved the "
            "file — consider setting WCVP_ZIP_URL to the new address.",
            level=INFO,
            new_url=r.url,
        )
    else:
        monitoring.clear("source_redirected")
    content_type = r.headers.get("Content-Type", "")
    if not any(t in content_type for t in ("zip", "octet-stream")):
        raise SourceError(
            "source_not_zip",
            f"{url} returned '{content_type or 'no content type'}' instead of a zip file: the "
            f"link may now point to a web page. Check {_kew_folder()} and update WCVP_ZIP_URL.",
            content_type=content_type,
        )


def fetch_remote_version() -> dict:
    """One HEAD request: what version of the zip Kew is serving right now."""
    _require(requests, "requests")
    try:
        r = requests.head(settings.wcvp_zip_url, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        raise SourceError(
            "source_unreachable",
            f"Can't reach {settings.wcvp_zip_url} ({type(e).__name__}: {e}). Check the server's "
            "internet access, DNS and firewall, and whether sftp.kew.org is up.",
        )
    _check_response(r)
    return _version_from_headers(r.headers)


def read_local_version() -> Optional[dict]:
    """Which version of the zip the CSVs on disk came from (None if unknown)."""
    try:
        with open(settings.source_state_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
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


def _validate_csv(path: str, member_setting: str) -> None:
    """Checks a freshly extracted CSV has the delimiter and columns we rely on."""
    member = getattr(settings, member_setting)
    header = list(pd.read_csv(path, delimiter=settings.csv_delimiter, nrows=0, dtype=str).columns)
    if len(header) <= 1:
        raise SourceError(
            "csv_format_changed",
            f"{member} in the new release has {len(header)} column(s) when split on "
            f"'{settings.csv_delimiter}': Kew may have changed the delimiter. Set CSV_DELIMITER "
            "to match the file.",
            header=header[:1],
        )
    missing = [c for c in REQUIRED_COLUMNS[member_setting] if c not in header]
    if missing:
        raise SourceError(
            "csv_format_changed",
            f"{member} in the new release is missing columns the API needs: {missing}. "
            "Kew may have renamed them; the new release was not loaded.",
            missing_columns=missing,
            found_columns=header,
        )


def download_and_extract_zip() -> dict:
    """
    Downloads wcvp.zip, extracts both CSVs and checks their columns. Everything
    goes to temp files first and is renamed into place only once both CSVs are
    extracted and valid, so a failed download, a corrupt zip or a changed
    layout leaves the current CSVs untouched.
    """
    _require(requests, "requests")
    _require(pd, "pandas")
    if "zlib" in monitoring.missing_libraries:
        _require(None, "zlib")

    zip_tmp = settings.db_path + ".zip.tmp"
    os.makedirs(os.path.dirname(zip_tmp) or ".", exist_ok=True)

    logger.info(f"Downloading {settings.wcvp_zip_url} ...")
    try:
        with requests.get(settings.wcvp_zip_url, stream=True, timeout=120) as r:
            _check_response(r)
            version = _version_from_headers(r.headers)
            with open(zip_tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):  # 8MB chunks
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise SourceError(
            "source_unreachable",
            f"Download of {settings.wcvp_zip_url} failed ({type(e).__name__}: {e}).",
        )

    extracted = []  # (tmp_path, final_path)
    try:
        try:
            zf = zipfile.ZipFile(zip_tmp)
        except zipfile.BadZipFile as e:
            raise SourceError(
                "zip_corrupt",
                f"The downloaded wcvp.zip is not a valid zip ({e}). The download may have been "
                "cut off, or the link now serves something else; will retry next check.",
            )
        with zf:
            by_name = {os.path.basename(n): n for n in zf.namelist()}
            csvs_in_zip = sorted(n for n in by_name if n.lower().endswith(".csv"))
            for member_setting, path_setting in ZIP_MEMBERS.items():
                member = getattr(settings, member_setting)
                if member not in by_name:
                    raise SourceError(
                        "zip_member_missing",
                        f"wcvp.zip no longer contains '{member}'. CSV files in the zip: "
                        f"{csvs_in_zip or 'none'}. If Kew renamed it, set "
                        f"{member_setting.upper()} to the new name in .env.",
                        expected=member,
                        csv_files_in_zip=csvs_in_zip,
                    )
                dest = getattr(settings, path_setting)
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                tmp = dest + ".tmp"
                logger.info(f"  ...extracting {member}")
                with zf.open(by_name[member]) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, 8 * 1024 * 1024)
                extracted.append((tmp, dest))
                _validate_csv(tmp, member_setting)
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
    _require(sqlite3, "sqlite3")
    _require(pd, "pandas")
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
    on plant_name_id for the /distribution lookup.
    """
    if not os.path.exists(settings.distribution_csv_path):
        logger.warning(
            f"No distribution file at {settings.distribution_csv_path} — "
            "/distribution will be unavailable."
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
    if sqlite3 is None or not os.path.exists(settings.db_path):
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


def _load_db_state() -> None:
    """Reads columns/distribution flags from whatever DB is on disk."""
    global _columns, _has_distribution
    if sqlite3 is not None and os.path.exists(settings.db_path):
        _columns = read_columns_from_db()
        _has_distribution = db_has_distribution()
        _filter_values.clear()  # a new release may bring new ranks/statuses


# --- Startup / refresh orchestration ----------------------------------------

def _record_failure(e: Exception, action: str) -> None:
    """Turns any failure while refreshing data into a warning with a clear reason."""
    note = _serving_note()
    if isinstance(e, SourceError):
        monitoring.warn(e.code, e.message + note, level=e.level, **e.detail)
    elif isinstance(e, PermissionError):
        monitoring.warn(
            "permission_denied",
            f"{action} failed: no permission to access {e.filename or 'a data file'}. Give the "
            f"app's user read/write access to the data folder.{note}",
            level=ERROR,
        )
    elif isinstance(e, OSError) and e.errno == errno.ENOSPC:
        monitoring.warn("disk_full", f"{action} failed: the disk is full. Free some space.{note}",
                        level=ERROR)
    elif sqlite3 is not None and isinstance(e, sqlite3.Error):
        monitoring.warn("sqlite_error", f"{action} failed with a SQLite error: "
                        f"{str(e).rstrip('.')}.{note}", level=ERROR)
        logger.exception(f"{action} failed")
    else:
        monitoring.warn("update_failed", f"{action} failed: {type(e).__name__}: "
                        f"{str(e).rstrip('.')}.{note}", level=ERROR)
        # Unexpected, so keep the traceback; the known cases above explain themselves.
        logger.exception(f"{action} failed")


def _refresh_locked(force_download: bool = False, force_rebuild: bool = False) -> None:
    """The readiness flow itself; callers must hold both locks."""
    global _last_ready_at

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
    _load_db_state()
    _last_ready_at = datetime.now(timezone.utc)
    monitoring.clear(*REFRESH_CODES)


def ensure_data_ready(force_download: bool = False, force_rebuild: bool = False) -> None:
    """
    Core readiness flow, shared by startup and /reload.
    - Downloads only if a CSV is missing or explicitly forced.
    - Rebuilds SQLite only if needed (missing DB, new download) or explicitly forced.
    A plain process restart with existing CSVs + existing DB does neither —
    it just starts serving immediately.
    """
    _require(sqlite3, "sqlite3")
    _check_data_folder_writable()
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
    monitoring.check_environment(settings, sqlite3)
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        remote = fetch_remote_version()
        _check_data_folder_writable()
    except Exception as e:
        _record_failure(e, "Checking Kew for a new release")
        _last_update_check = {"checked_at": checked_at, "error": str(e), "updated": False}
        raise
    monitoring.clear(*SOURCE_CODES)

    with _build_lock, _process_lock:
        local = read_local_version()
        updated = not _same_version(remote, local)
        if updated:
            logger.info(
                f"New WCVP release (Last-Modified {remote['last_modified']}, "
                f"had {local.get('last_modified') if local else 'unknown'}) - updating."
            )
            try:
                _refresh_locked(force_download=True, force_rebuild=True)
            except Exception as e:
                _record_failure(e, "Updating to the new WCVP release")
                _last_update_check = {
                    "checked_at": checked_at, "remote": remote, "error": str(e), "updated": False,
                }
                raise
        else:
            logger.info(f"WCVP zip unchanged (Last-Modified {remote['last_modified']}).")
    _last_update_check = {"checked_at": checked_at, "remote": remote, "updated": updated}
    return _last_update_check


def _update_loop() -> None:
    """Checks once right after startup, then every UPDATE_CHECK_HOURS."""
    while True:
        try:
            check_for_update()
        except Exception:
            pass  # already logged and shown as a warning; retry next interval
        if _stop_updates.wait(settings.update_check_hours * 3600):
            return


def _prepare_data() -> None:
    try:
        ensure_data_ready()
    except Exception as e:
        _record_failure(e, "Preparing the data at startup")
        _load_db_state()  # keep serving an older DB if there is one
    if sqlite3 is None or not os.path.exists(settings.db_path):
        monitoring.warn(
            "data_unavailable",
            "No database is available, so search, filter and distribution return 503. "
            "See the other warnings for why it couldn't be built.",
            level=ERROR,
        )


_started_pid: Optional[int] = None  # a pid, not a flag: a forked child must start its own
_start_lock = threading.Lock()


def start_services(background: bool = False) -> None:
    """
    Startup work: environment checks, data prep, and the daily update check.
    Runs once per process, however often it's called.

    - Under uvicorn/gunicorn it runs from the startup event and prepares the
      data before serving (background=False).
    - Under cPanel/Passenger (passenger_wsgi.py) there are no startup events,
      and Passenger gives up on an app that takes minutes to start, so the
      data prep runs in a background thread (background=True). Until the
      first build finishes, queries return 503 and /health says it's preparing.
    """
    global _started_pid
    with _start_lock:
        if _started_pid == os.getpid():
            return
        _started_pid = os.getpid()

    monitoring.check_environment(settings, sqlite3)
    run_updates = settings.update_check_enabled and requests is not None

    if not background:
        _prepare_data()
        if run_updates:
            threading.Thread(target=_update_loop, name="wcvp-update-check", daemon=True).start()
        return

    # Reading the column list from an existing DB takes milliseconds; do it now,
    # or the first requests would see no columns while the thread starts up.
    try:
        _load_db_state()
    except Exception as e:
        _record_failure(e, "Reading the existing database")

    def prepare_then_watch():
        if not os.path.exists(settings.db_path):
            monitoring.warn(
                "data_preparing",
                "Downloading wcvp.zip and building the database in the background. The "
                "first start takes a few minutes; queries return 503 until it's done.",
                level=INFO,
            )
        _prepare_data()
        monitoring.clear("data_preparing")
        if run_updates:
            _update_loop()

    threading.Thread(target=prepare_then_watch, name="wcvp-startup", daemon=True).start()


@app.on_event("startup")
def startup_event():
    start_services()


@app.on_event("shutdown")
def shutdown_event():
    _stop_updates.set()


def get_connection() -> sqlite3.Connection:
    if sqlite3 is None:
        raise HTTPException(status_code=503, detail="SQLite is not available on this server. See /health.")
    if not os.path.exists(settings.db_path):
        raise HTTPException(status_code=503, detail="Database not ready yet. See /health.")
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


def _fetch_page(conn, rank_sql: str, rank_params: list, where_sql: str, where_params: list,
                page: int) -> tuple:
    """
    One page of ranked rows plus the total match count. On SQLite 3.25+ the
    total comes from COUNT(*) OVER () in the same pass; older SQLite has no
    window functions, so it takes a second COUNT query.
    """
    has_window = sqlite3.sqlite_version_info >= (3, 25, 0)
    total_sql = ", COUNT(*) OVER () AS _total" if has_window else ""
    rows = conn.execute(
        f"SELECT *, {rank_sql} AS _rank{total_sql} "
        f"FROM {TABLE} WHERE {where_sql} {ORDER_SQL} LIMIT ? OFFSET ?",
        rank_params + where_params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    if rows and has_window:
        return rows, rows[0]["_total"]
    # Older SQLite, or past the last page: still report the real total.
    total = conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where_sql}", where_params).fetchone()[0]
    return rows, total


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


# --- Column filters (/search and /filter) ---------------------------------------

# Extra filters both endpoints accept as plain query parameters, e.g.
# ?taxon_rank=Species&taxon_status=Accepted. Exact and case-insensitive;
# different filters combine with AND, a repeated filter means OR.
FILTER_COLUMNS = {
    "taxon_rank": "e.g. Species, Genus, Subspecies, Variety, Form",
    "taxon_status": "e.g. Accepted, Synonym",
    "family": "e.g. Fabaceae",
    "genus": "e.g. Tamarindus",
    "lifeform_description": "e.g. tree, perennial, shrub or tree",
    "climate_description": "e.g. wet tropical, temperate",
    "reviewed": "Y or N",
}
# Filters with a short, fixed set of values: an unknown value is a 400 that
# lists the valid ones, so a typo like "Speicies" doesn't silently return nothing.
ENUM_FILTERS = ("taxon_rank", "taxon_status", "climate_description", "reviewed")
_filter_values: dict = {}  # column -> its distinct values, loaded on first use


def _valid_filter_values(col: str) -> list:
    if col not in _filter_values:
        conn = get_connection()
        try:
            rows = conn.execute(f'SELECT DISTINCT "{col}" FROM {TABLE}').fetchall()
        finally:
            conn.close()
        _filter_values[col] = sorted(v for (v,) in rows if v)
    return _filter_values[col]


def _filter_query(desc: str):
    return Query(None, description=f"Exact match, case-insensitive ({desc}). "
                                   "Repeat the parameter to allow several values.")


def column_filters(
    taxon_rank: Optional[List[str]] = _filter_query(FILTER_COLUMNS["taxon_rank"]),
    taxon_status: Optional[List[str]] = _filter_query(FILTER_COLUMNS["taxon_status"]),
    family: Optional[List[str]] = _filter_query(FILTER_COLUMNS["family"]),
    genus: Optional[List[str]] = _filter_query(FILTER_COLUMNS["genus"]),
    lifeform_description: Optional[List[str]] = _filter_query(FILTER_COLUMNS["lifeform_description"]),
    climate_description: Optional[List[str]] = _filter_query(FILTER_COLUMNS["climate_description"]),
    reviewed: Optional[List[str]] = _filter_query(FILTER_COLUMNS["reviewed"]),
) -> dict:
    """FastAPI dependency: the filters given, as {column: [values]}."""
    given = {
        "taxon_rank": taxon_rank, "taxon_status": taxon_status, "family": family,
        "genus": genus, "lifeform_description": lifeform_description,
        "climate_description": climate_description, "reviewed": reviewed,
    }
    return {col: values for col, values in given.items() if values is not None}


def _reject_unknown_params(request: Request, own_params: tuple) -> None:
    """400 for parameters the endpoint doesn't know, instead of ignoring them."""
    allowed = set(own_params) | set(FILTER_COLUMNS)
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown parameter(s): {unknown}. This endpoint accepts {list(own_params)} "
                   f"and these filters: {list(FILTER_COLUMNS)}.",
        )


def _filters_sql(filters: dict) -> tuple:
    """Validates the filters and returns (sql, params, applied) to AND onto a query."""
    clauses, params, applied = [], [], {}
    for col, raw_values in filters.items():
        values = [v.strip() for v in raw_values if v.strip()]
        if not values:
            raise HTTPException(status_code=400, detail=f"Filter '{col}' needs a value.")
        if col not in _columns:
            raise HTTPException(status_code=400, detail=f"Filter '{col}' isn't in this data release.")
        if col in ENUM_FILTERS:
            valid = _valid_filter_values(col)
            known = {v.lower() for v in valid}
            invalid = [v for v in values if v.lower() not in known]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {col} value(s): {invalid}. Valid values: {valid}.",
                )
        clauses.append("(" + " OR ".join(f'"{col}" = ? COLLATE NOCASE' for _ in values) + ")")
        params += values
        applied[col] = values[0] if len(values) == 1 else values
    return " AND ".join(clauses), params, applied


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
    """
    Status, loaded data, and every active warning (Kew link unreachable or
    moved, zip/CSV layout changed, missing libraries, permissions, disk space).
    status is "ok", "degraded" (serving, but something needs attention) or
    "unavailable" (no data to serve; HTTP 503).
    """
    db_available = sqlite3 is not None and os.path.exists(settings.db_path)
    db_size_mb = round(os.path.getsize(settings.db_path) / (1024 * 1024), 1) if db_available else None
    row_count = None
    if db_available:
        try:
            conn = get_connection()
            try:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
            finally:
                conn.close()
            monitoring.clear("database_unreadable")
        except Exception as e:
            db_available = False
            monitoring.warn("database_unreadable", f"The database at {settings.db_path} can't be "
                            f"queried: {e}", level=ERROR)

    warnings = monitoring.active()
    if not db_available:
        status = "unavailable"
    elif any(w["level"] in (ERROR, WARNING) for w in warnings):
        status = "degraded"
    else:
        status = "ok"
    body = {
        "status": status,
        "db_size_mb": db_size_mb,
        "row_count": row_count,
        "distribution_loaded": _has_distribution,
        "ready_at": _last_ready_at.isoformat() if _last_ready_at else None,
        "data_version": (read_local_version() or {}).get("last_modified"),
        "last_update_check": _last_update_check,
        "log_file": os.path.abspath(settings.log_path),
        "warnings": warnings,
    }
    return JSONResponse(body, status_code=503 if status == "unavailable" else 200)


@app.get("/columns")
def columns():
    """Inspect actual column names from your CSV."""
    return {"columns": _columns, "searchable_columns": NAME_COLUMNS}


@app.get("/source")
def source():
    """Where the data comes from, which release is loaded, and the last update check."""
    return {
        "zip_url": settings.wcvp_zip_url,
        "zip_members": {s: getattr(settings, s) for s in ZIP_MEMBERS},
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
        raise HTTPException(status_code=502, detail=f"Update check failed: {e}")


@app.post("/reload")
def reload_data(x_reload_token: Optional[str] = Header(None)):
    """
    Explicit refresh: always re-downloads wcvp.zip and always rebuilds the
    SQLite database, even if Kew's file hasn't changed — this endpoint IS
    the "force" trigger. Use /check-update to download only when newer.
    """
    _require_reload_token(x_reload_token)
    monitoring.check_environment(settings, sqlite3)
    try:
        ensure_data_ready(force_download=True, force_rebuild=True)
    except Exception as e:
        _record_failure(e, "Reload")
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
        raise HTTPException(status_code=503, detail="Distribution data is not loaded. See /health.")
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
    filters: dict = Depends(column_filters),
    request: Request = None,
):
    """
    Searches plant names, best matches first: a whole-field match beats a
    starts-with match, which beats a contains match. 10 results per page.

    Narrow it down with column filters, e.g.
    `/search?q=tamarind&taxon_rank=Species&taxon_status=Accepted`.
    """
    _reject_unknown_params(request, ("q", "columns", "exact", "page"))
    search_cols = _parse_name_columns(columns)
    filter_sql, filter_params, applied = _filters_sql(filters)
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
    if filter_sql:
        where_sql = f"({where_sql}) AND {filter_sql}"
        where_params += filter_params

    conn = get_connection()
    try:
        rows, total = _fetch_page(conn, rank_sql, rank_params, where_sql, where_params, page)
    finally:
        conn.close()

    return _page_response(rows, total, page, query=q, searched_columns=search_cols, filters=applied)


@app.get("/filter")
def filter_exact(
    column: str = Query(..., description="One of genus, family, species, infraspecies, taxon_name"),
    value: str = Query(..., min_length=1, description="Exact value to match (case-insensitive)"),
    page: int = Query(1, ge=1),
    filters: dict = Depends(column_filters),
    request: Request = None,
):
    """
    Exact-match filter on one name column, e.g. /filter?column=family&value=Orchidaceae.
    Accepted names first. 10 results per page. Add column filters to narrow it,
    e.g. `&taxon_rank=Genus`.
    """
    _reject_unknown_params(request, ("column", "value", "page"))
    col = _parse_name_columns(column)
    if len(col) != 1:
        raise HTTPException(status_code=400, detail="Pass exactly one column.")
    col = col[0]
    filter_sql, filter_params, applied = _filters_sql(filters)
    where_sql = f'"{col}" = ? COLLATE NOCASE' + (f" AND {filter_sql}" if filter_sql else "")

    conn = get_connection()
    try:
        rows, total = _fetch_page(conn, "0", [], where_sql, [value] + filter_params, page)
    finally:
        conn.close()

    return _page_response(rows, total, page, column=col, value=value, filters=applied)
