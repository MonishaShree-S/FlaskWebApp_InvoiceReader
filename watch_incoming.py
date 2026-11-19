"""
Standalone entrypoint for the incoming/ auto-import pipeline.

This is only needed if you want the watcher running WITHOUT the Flask web
app -- normally it now starts automatically as part of `python run.py` (see
start_incoming_watcher() in app/services/incoming_watcher.py, called from
run.py before app.run()).

Watches INCOMING_FOLDER/<Shop>/*.pdf (default C:\\SiranthaRechnungSplit\\incoming,
override with the INCOMING_ROOT env var) and, for every PDF that shows up,
runs it through the same parse/dedupe/insert logic as the manual bill upload
routes, then files the original into processed/ (or duplicates/, needs_review/,
failed/) under the same root -- see app/services/incoming_watcher.py.
Amazon Visa credit card statements go into incoming/Amazon_Visa/.

    python watch_incoming.py

Runs until interrupted (Ctrl+C). Make sure AUTO_IMPORT_USERID is set in .env
before starting it.
"""

import os
import time
import logging

# Several parts of this codebase (db.properties lookup, flask_session's
# cache directory, admin_actions.log, ...) resolve relative to the process's
# current working directory rather than the project root. That's harmless
# when launched from the project root, but this script may also be started
# from elsewhere. Pin the cwd here, before anything else imports.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.services.incoming_watcher import start_incoming_watcher

LOG_DIR = os.path.join(os.environ.get("INCOMING_ROOT", r"C:\SiranthaRechnungSplit"), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "watch_incoming.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


def main():
    app = create_app()
    start_incoming_watcher(app)  # non-blocking -- runs on daemon threads
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
