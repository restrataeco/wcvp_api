"""
Checks the server can run the WCVP API, without a terminal.

cPanel: Setup Python App > your app > "Execute python script" > enter
check_setup.py > Run Script. (Or from a shell in the app's virtualenv:
python check_setup.py.) Each line says OK or what to fix.
"""

import importlib
import os
import sys

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_ROOT)
os.chdir(APP_ROOT)

problems = 0


def report(ok: bool, label: str, detail: str = "") -> None:
    global problems
    problems += 0 if ok else 1
    print(f"[{'OK ' if ok else 'FIX'}] {label}" + (f" - {detail}" if detail else ""))


print(f"Python {sys.version.split()[0]} at {sys.executable}")
print(f"App folder: {APP_ROOT}\n")

report(sys.version_info >= (3, 9), "Python 3.9 or newer", sys.version.split()[0])

for module, package in [
    ("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pandas", "pandas"),
    ("pydantic_settings", "pydantic-settings"), ("requests", "requests"),
    ("filelock", "filelock"), ("a2wsgi", "a2wsgi"), ("sqlite3", None), ("zlib", None),
]:
    try:
        mod = importlib.import_module(module)
        report(True, f"import {module}", getattr(mod, "__version__", getattr(mod, "sqlite_version", "")))
    except Exception as e:
        fix = (f"run 'Run Pip Install' with requirements.txt ({package} is missing)" if package
               else "this Python was built without it; ask the host or pick another Python version")
        report(False, f"import {module}", f"{type(e).__name__}: {e}. Fix: {fix}")

for name in ("passenger_wsgi.py", "requirements.txt", ".env", "app/main.py"):
    report(os.path.exists(name), f"file {name} present",
           "" if os.path.exists(name) else "upload it to the application root")

try:
    from app.config import settings

    report(True, "settings load (.env)")
    data_dir = os.path.dirname(settings.db_path) or "."
    os.makedirs(data_dir, exist_ok=True)
    probe = os.path.join(data_dir, ".check_setup_write_test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        report(True, f"can write to {os.path.abspath(data_dir)}")
    except OSError as e:
        report(False, f"can write to {os.path.abspath(data_dir)}", str(e))
    free_gb = __import__("shutil").disk_usage(data_dir).free / 1024 ** 3
    report(free_gb >= settings.min_free_disk_gb, "free disk space", f"{free_gb:.1f} GB free")
    try:
        import requests

        r = requests.head(settings.wcvp_zip_url, timeout=30, allow_redirects=True)
        report(r.ok, f"reach {settings.wcvp_zip_url}",
               f"HTTP {r.status_code}, Last-Modified {r.headers.get('Last-Modified')}")
    except Exception as e:
        report(False, f"reach {settings.wcvp_zip_url}", f"{type(e).__name__}: {e}")
    report(settings.reload_token not in ("", "change-me-to-a-real-secret"),
           "RELOAD_TOKEN is set to a real secret",
           "" if settings.reload_token not in ("", "change-me-to-a-real-secret")
           else "edit .env and set RELOAD_TOKEN to a long random value")
except Exception as e:
    report(False, "settings load (.env)", f"{type(e).__name__}: {e}")

try:
    import app.main  # noqa: F401  (imports only; doesn't start the download/build)

    report(True, "app imports (passenger_wsgi.py will load)")
except Exception as e:
    report(False, "app imports", f"{type(e).__name__}: {e}")

print("\nAll good - click Restart and open /health." if not problems
      else f"\n{problems} thing(s) to fix above, then click Restart.")
