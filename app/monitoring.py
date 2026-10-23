"""
Logging, active warnings, and environment checks.

Problems that need someone's attention (Kew's link is down or moved, the zip
or CSV layout changed, a library, permission or disk space is missing) are
registered here as warnings. Each one is logged once when it appears and
once when it clears, and while active it shows on /health and at the top of
the Swagger page (/docs).
"""

import importlib
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

logger = logging.getLogger("wcvp_api")

ERROR, WARNING, INFO = "error", "warning", "info"
_LEVEL_ICON = {ERROR: "🔴", WARNING: "🟠", INFO: "🔵"}

_warnings: dict = {}
_lock = threading.Lock()

# What each optional library is needed for; filled by optional_import().
LIBRARY_PURPOSE = {
    "sqlite3": "storing and querying the data (Python must be built with SQLite support)",
    "pandas": "reading the CSVs when building the database",
    "requests": "downloading wcvp.zip and checking Kew for updates",
    "filelock": "keeping several workers from building at the same time",
    "zlib": "extracting wcvp.zip (Python must be built with zlib support)",
}
missing_libraries: dict = {}


def optional_import(name: str):
    """Imports a library, or records it as missing and returns None, so the
    app can still start and report the problem instead of crashing."""
    try:
        return importlib.import_module(name)
    except ImportError as e:
        missing_libraries[name] = str(e)
        return None


# --- Logging ------------------------------------------------------------------

def setup_logging(log_path: str, max_mb: int, backup_count: int) -> None:
    """Console plus a rotating log file. If the file can't be opened, logs go
    to the console only and a warning says why."""
    root = logging.getLogger()
    if getattr(root, "_wcvp_configured", False):  # uvicorn --reload re-imports
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    root._wcvp_configured = True
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=max_mb * 1024 * 1024, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError as e:
        warn(
            "log_file_unwritable",
            f"Can't write the log file {log_path} ({e.strerror or e}). Logging to the console "
            "only. Check the folder exists and the app's user may write to it, or set LOG_PATH.",
        )


# --- Warnings registry ----------------------------------------------------------

def warn(code: str, message: str, level: str = WARNING, **detail) -> None:
    """Registers (or updates) an active warning. Logged when new or changed."""
    with _lock:
        previous = _warnings.get(code)
        _warnings[code] = {
            "code": code,
            "level": level,
            "message": message,
            "detail": detail or None,
            "since": previous["since"] if previous else datetime.now(timezone.utc).isoformat(),
        }
    if not previous or previous["message"] != message:
        log = logger.error if level == ERROR else logger.warning if level == WARNING else logger.info
        log(f"[{code}] {message}")


def clear(*codes: str) -> None:
    for code in codes:
        with _lock:
            removed = _warnings.pop(code, None)
        if removed:
            logger.info(f"[{code}] resolved.")


def clear_prefix(prefix: str) -> None:
    with _lock:
        codes = [c for c in _warnings if c.startswith(prefix)]
    clear(*codes)


def active() -> list:
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    with _lock:
        return sorted(_warnings.values(), key=lambda w: (order[w["level"]], w["since"]))


def description_with_warnings(base: str) -> str:
    """Swagger page description, with a banner listing active warnings."""
    items = active()
    if not items:
        return base
    lines = [f"## ⚠️ {len(items)} active warning{'s' if len(items) > 1 else ''}", ""]
    for w in items:
        lines.append(
            f"- {_LEVEL_ICON[w['level']]} **{w['level'].upper()} · `{w['code']}`** — "
            f"{w['message']} _(since {w['since'][:19].replace('T', ' ')} UTC)_"
        )
    lines += ["", "Details are in `GET /health` and the log file.", "", "---", "", base]
    return "\n".join(lines)


# --- Environment checks ---------------------------------------------------------

def writable_dir_problem(path: str) -> Optional[str]:
    """
    None if we can create and delete a file in `path`, else the reason.
    Uses one direct attempt rather than tempfile.mkstemp: on Windows mkstemp
    treats "access denied" as "name taken" and retries ~238,000 names.
    """
    probe = os.path.join(path, f".write_test_{os.getpid()}_{threading.get_ident()}")
    try:
        os.makedirs(path, exist_ok=True)
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        os.remove(probe)
        return None
    except FileExistsError:
        os.remove(probe)  # left over from a crashed check
        return writable_dir_problem(path)
    except OSError as e:
        return e.strerror or str(e)


def check_environment(settings, sqlite3) -> None:
    """
    Startup/pre-update checks: required libraries, zip support, SQLite
    usability and version, read/write permissions on the data files, and
    free disk space. Each problem becomes a warning; fixed ones are cleared.
    """
    for lib, purpose in LIBRARY_PURPOSE.items():
        code = f"library_missing_{lib}"
        if lib in missing_libraries:
            warn(
                code,
                f"Python library '{lib}' is not available; it's needed for {purpose}. "
                f"Install it (pip install -r requirements.txt) and restart.",
                level=ERROR,
                error=missing_libraries[lib],
            )
        else:
            clear(code)

    data_dir = os.path.dirname(settings.db_path) or "."
    folders = {data_dir}
    for path in (settings.csv_path, settings.distribution_csv_path):
        folders.add(os.path.dirname(path) or ".")
    for folder in sorted(folders):
        code = f"folder_not_writable:{folder}"
        reason = writable_dir_problem(folder)
        if reason:
            warn(
                code,
                f"Can't write to {os.path.abspath(folder)} ({reason}). Downloading, extracting "
                "and rebuilding the data will fail. Give the app's user write access.",
                level=ERROR,
            )
        else:
            clear(code)

    for path, what in (
        (settings.csv_path, "names CSV"),
        (settings.distribution_csv_path, "distribution CSV"),
        (settings.db_path, "database"),
    ):
        code = f"file_not_readable:{path}"
        if os.path.exists(path) and not os.access(path, os.R_OK):
            warn(code, f"The {what} {os.path.abspath(path)} exists but can't be read. "
                       "Check its file permissions.", level=ERROR)
        else:
            clear(code)

    if sqlite3 is not None:
        version = sqlite3.sqlite_version_info
        if version < (3, 25, 0):
            warn(
                "sqlite_old",
                f"SQLite {sqlite3.sqlite_version} is older than 3.25. Search still works, but "
                "each page needs a second query for the total, so it's slower. Upgrade SQLite "
                "(or use the Docker image) for full speed.",
            )
        else:
            clear("sqlite_old")
        if not writable_dir_problem(data_dir):
            probe = os.path.join(data_dir, ".sqlite_test.db")
            try:
                conn = sqlite3.connect(probe)
                conn.execute("CREATE TABLE t (x TEXT)")
                conn.execute("INSERT INTO t VALUES ('ok')")
                conn.commit()
                conn.close()
                clear("sqlite_unusable")
            except Exception as e:
                warn("sqlite_unusable", f"SQLite can't create a database in "
                     f"{os.path.abspath(data_dir)}: {e}", level=ERROR)
            finally:
                if os.path.exists(probe):
                    os.remove(probe)

    try:
        free_gb = shutil.disk_usage(data_dir).free / 1024 ** 3
        if free_gb < settings.min_free_disk_gb:
            warn(
                "disk_space_low",
                f"Only {free_gb:.1f} GB free in {os.path.abspath(data_dir)}; an update needs about "
                f"{settings.min_free_disk_gb:g} GB (zip, both CSVs and a second database). "
                "Updates may fail until space is freed.",
                free_gb=round(free_gb, 2),
            )
        else:
            clear("disk_space_low")
    except OSError:
        pass
