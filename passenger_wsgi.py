"""
Entry point for cPanel "Setup Python App" (Phusion Passenger).

Passenger speaks WSGI, and FastAPI is an ASGI app, so a2wsgi wraps it.
a2wsgi doesn't run FastAPI's startup events, so the startup work is started
here instead — in the background, because Passenger gives up on an app that
takes minutes to start, and the first start downloads and builds the data.

In cPanel: Application startup file = passenger_wsgi.py,
           Application Entry point  = application
"""

import os
import sys

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_ROOT)
os.chdir(APP_ROOT)  # .env, data/ and logs/ are relative to the app folder

from a2wsgi import ASGIMiddleware  # noqa: E402

from app.main import app, start_services  # noqa: E402

start_services(background=True)

application = ASGIMiddleware(app)
