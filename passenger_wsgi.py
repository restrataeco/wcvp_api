"""
Entry point for cPanel "Setup Python App" (Passenger or LiteSpeed's lswsgi).

Both speak WSGI, and FastAPI is an ASGI app, so a2wsgi wraps it. Two things
make this file more than a one-liner:

- Fork safety. LiteSpeed loads this file once and then forks worker
  processes from it. Threads don't survive a fork, and a2wsgi runs its event
  loop in a thread, so if the wrapper were created here at import time,
  every request in a worker would wait forever on a loop that isn't running.
  The wrapper and the startup work (update check, data prep) are therefore
  created on the first request in each process.
- Startup work. a2wsgi doesn't run FastAPI's startup events, so
  start_services() is called here, in the background, because the host gives
  up on an app that takes minutes to answer.

If the app can't be imported (e.g. requirements not installed), every
request gets a plain-text page saying why instead of the host's generic 500,
and the full traceback goes to startup_error.log in this folder.

In cPanel: Application startup file = passenger_wsgi.py,
           Application Entry point  = application
"""

import os
import sys
import threading

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_ROOT)
os.chdir(APP_ROOT)  # .env, data/ and logs/ are relative to the app folder

try:
    from a2wsgi import ASGIMiddleware

    from app.main import app, start_services

    _wrapped = {"pid": None, "app": None}
    _init_lock = threading.Lock()

    def application(environ, start_response):
        # Set up once per process — after any fork, never in the parent.
        if _wrapped["pid"] != os.getpid():
            with _init_lock:
                if _wrapped["pid"] != os.getpid():
                    _wrapped["app"] = ASGIMiddleware(app)
                    start_services(background=True)
                    _wrapped["pid"] = os.getpid()
        return _wrapped["app"](environ, start_response)

except Exception:
    import traceback
    from datetime import datetime, timezone

    _traceback = traceback.format_exc()
    _summary = _traceback.strip().splitlines()[-1]
    try:
        with open(os.path.join(APP_ROOT, "startup_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now(timezone.utc).isoformat()} ===\n{_traceback}")
    except OSError:
        pass

    _HINT = (
        "Usually this means the dependencies aren't installed in this app's virtual "
        "environment: in cPanel > Setup Python App, add requirements.txt under "
        "'Configuration files', click 'Run Pip Install', then 'Restart'.\n"
        "For more detail, run check_setup.py ('Execute python script' on the same page), "
        "or read startup_error.log in the application folder."
    )

    def application(environ, start_response):
        body = f"WCVP API failed to start.\n\n{_summary}\n\n{_HINT}\n"
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [body.encode("utf-8")]
