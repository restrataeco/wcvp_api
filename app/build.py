"""
Builds or updates the WCVP data outside the web server.

    python -m app.build                 build whatever is missing (CSVs, database)
    python -m app.build --check-update  download and rebuild only if Kew has a newer release
    python -m app.build --force         re-download and rebuild now

Run it from the app folder (where .env is). Under cPanel the web app starts
this itself when needed, because a multi-minute build can't survive inside a
web worker there. It's also safe to run by hand or from a cron job: a lock
makes overlapping runs wait for each other, the old data keeps serving until
the new database is swapped in, and the result (with any warnings) goes to
data/build_status.json, which /health and the Swagger page show.
"""

import argparse
import sys

from app import main


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.build", description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-update", action="store_true",
                      help="download and rebuild only if Kew has published a newer wcvp.zip")
    mode.add_argument("--force", action="store_true", help="re-download and rebuild now")
    args = parser.parse_args(argv)

    main.monitoring.check_environment(main.settings, main.sqlite3)
    try:
        if args.check_update:
            result = main.check_for_update()
            print("Updated to a new release." if result["updated"] else "Already up to date.")
        elif args.force:
            main.ensure_data_ready(force_download=True, force_rebuild=True,
                                   action="Rebuilding the data")
            print("Rebuilt.")
        else:
            main.ensure_data_ready(action="Building the data")
            print("Data is ready.")
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
