# app/services/incoming_watcher.py
"""
Auto-import pipeline for bills dropped into INCOMING_FOLDER/<Shop>/*.pdf.

Reuses process_one_pdf_upload() -- the same parse/dedupe/insert logic the
manual /upload_batch route uses -- so a file dropped on disk is treated
identically to one uploaded through the web UI. The shop name is taken from
the immediate parent folder (incoming/Rewe/, incoming/Kaufland/, ...), since
there is no logged-in session to supply it.

Credit card statements work the same way: a PDF dropped into
incoming/Amazon_Visa/ (see CREDIT_CARD_FOLDERS) goes through
process_credit_card_statement_upload() instead and ends up in
CreditCardTransactions, filed under the same outcome folders below as
amazon_visa/amazon_visa_<YYYY-MM>.pdf.

Outcomes are filed under sibling folders of the same root:
  processed/<Shop>/     -- inserted successfully
  duplicates/<Shop>/    -- identical content already exists for that date
  needs_review/<Shop>/  -- same date, different content; needs a human to
                           confirm it's really a second bill (mirrors the
                           "needs_confirmation" status the web UI would show)
  failed/<Shop>/         -- parsing or data error; retrying won't help

Whatever messy name a source file arrives with (e.g. a browser download
like "Dein REWE eBon vom 07.09.2026.pdf") is discarded on the way into
these folders -- the file is renamed to its billId (e.g. R_07_09_2026.pdf)
once that's known from the bill's own parsed content, so the audit trail
stays consistent regardless of how the file was originally named. Only
genuinely failed files (where no billId could be derived at all) keep a
sanitized version of their original name.

A file is left in place (not moved anywhere) when the database itself is
unreachable (connection drop, DB not started yet, etc.) -- that's a transient
condition, not a verdict on the bill, so the file stays in incoming/<Shop>/
and is retried on the next pass (either the next filesystem event, or the
periodic re-sweep in watch_incoming.py).

All functions here must run inside a Flask app context.
"""

import os
import re
import shutil
import logging
import queue
import threading
import time

from sqlalchemy.exc import DBAPIError, OperationalError
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from werkzeug.utils import secure_filename

from app.models.models import get_session
from app.services.import_timing import BatchTracker
from app.services.card_reconciliation import safe_auto_match
from app.services.credit_cards import AMAZON_VISA_PROVIDER, process_credit_card_statement_upload
from app.utils.pdf_upload_helpers import process_one_pdf_upload

logger = logging.getLogger(__name__)

# incoming/<folder>/ names (lowercased, spaces/dashes -> "_") that hold credit
# card statements rather than shop bills, mapped to their provider.
CREDIT_CARD_FOLDERS = {
    "amazon_visa": AMAZON_VISA_PROVIDER,
    "amazonvisa": AMAZON_VISA_PROVIDER,
    "amazon": AMAZON_VISA_PROVIDER,
}

# A filesystem event only fires once, on drop -- this is what catches a file
# that was left in place after a transient DB error (see process_incoming_file),
# plus anything dropped while the watcher wasn't running at all.
RESCAN_INTERVAL_SECONDS = 300

_tracker = None
_tracker_lock = threading.Lock()


def _get_tracker(app):
    """Timing tracker (see import_timing.py); CSVs go to <INCOMING_ROOT>/logs/."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            root = os.path.dirname(os.path.normpath(app.config["INCOMING_FOLDER"]))
            _tracker = BatchTracker(os.path.join(root, "logs"))
        return _tracker


class LocalFileAdapter:
    """Minimal werkzeug FileStorage look-alike backed by a file already on
    disk, so process_one_pdf_upload() can be reused unchanged for files
    found by the watcher instead of submitted via an HTTP request."""

    def __init__(self, path):
        self.filename = os.path.basename(path)
        self._file = open(path, "rb")
        self.stream = self._file

    def save(self, dest_path):
        self.stream.seek(0)
        with open(dest_path, "wb") as dest:
            shutil.copyfileobj(self.stream, dest)

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass


def _wait_until_stable(path, checks=2, interval=1.0):
    """Wait until a file's size stops changing between polls, so a file
    that's still being written/synced into incoming/ isn't read too early."""
    last_size = -1
    stable_count = 0
    while stable_count < checks:
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size == last_size:
            stable_count += 1
        else:
            stable_count = 0
            last_size = size
        time.sleep(interval)
    return True


def _unique_destination(directory, filename):
    os.makedirs(directory, exist_ok=True)
    dest = os.path.join(directory, filename)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(filename)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{stem}_{counter}{ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _move_to(app, config_key, shopname, src_path, dest_filename=None):
    """Move src_path into app.config[config_key]/<shopname>/, renamed to
    dest_filename when given -- used to replace messy source names (e.g.
    "Dein REWE eBon vom 07.09.2026.pdf") with the clean billId-based name
    once the bill's own content/outcome is known. Falls back to the
    original basename when no clean name is available (e.g. parsing failed
    before any billId could be derived)."""
    shop_dir = os.path.join(app.config[config_key], shopname)
    dest = _unique_destination(shop_dir, dest_filename or os.path.basename(src_path))
    shutil.move(src_path, dest)
    return dest


def process_incoming_file(app, file_path, detected_at=None, source="sweep"):
    """Process a single PDF found under incoming/<Shop>/... Safe to call
    more than once for the same path (e.g. duplicate filesystem events) --
    the second call is a no-op once the file has been moved out.

    detected_at is when the watcher first saw the file (epoch seconds); it's
    the start point for the end-to-end timing recorded in import_timing.py."""
    if not os.path.isfile(file_path) or not file_path.lower().endswith(".pdf"):
        return

    shopname = os.path.basename(os.path.dirname(file_path)).strip().lower()
    if not shopname:
        logger.warning("Skipping %s: not inside a shop subfolder (incoming/<Shop>/...).", file_path)
        return

    tracker = _get_tracker(app)
    timer = tracker.start(file_path, detected_at=detected_at, source=source)

    stable = _wait_until_stable(file_path)
    timer.mark("stable_wait")
    if not stable:
        tracker.finish(timer, shopname, "skipped")
        return

    userid = app.config.get("AUTO_IMPORT_USERID")
    if not userid:
        logger.error("AUTO_IMPORT_USERID is not configured; cannot process %s", file_path)
        tracker.finish(timer, shopname, "skipped")
        return

    provider = CREDIT_CARD_FOLDERS.get(re.sub(r"[\s\-]+", "_", shopname))
    if provider:
        shopname = provider  # outcome folders are filed under the provider name

    adapter = LocalFileAdapter(file_path)
    try:
        with get_session() as db_session:
            try:
                if provider:
                    result = process_credit_card_statement_upload(
                        file_storage=adapter,
                        userid=int(userid),
                        provider=provider,
                        db_session=db_session,
                    )
                else:
                    result = process_one_pdf_upload(
                        file_storage=adapter,
                        userid=int(userid),
                        shopname=shopname,
                        db_session=db_session,
                        force_insert=False,
                    )
                status = result.get("status")

                if status == "inserted":
                    db_session.commit()
                    adapter.close()
                    timer.mark("process")
                    dest = _move_to(app, "PROCESSED_FOLDER", shopname, file_path, dest_filename=f"{result.get('billId')}.pdf")
                    timer.mark("move")
                    logger.info("Inserted %s as billId=%s -> %s", file_path, result.get("billId"), dest)
                    tracker.finish(timer, shopname, "processed", result.get("billId"))
                    # Link the new bill / statement to its card charges / receipts.
                    safe_auto_match(int(userid))

                elif status == "skipped":
                    db_session.rollback()
                    adapter.close()
                    timer.mark("process")
                    duplicate_of = result.get("billId") or "duplicate"
                    if duplicate_of.lower().endswith(".pdf"):
                        # statement_ids from manual credit card uploads embed the uploaded filename
                        duplicate_of = duplicate_of[:-4]
                    dest = _move_to(app, "DUPLICATES_FOLDER", shopname, file_path, dest_filename=f"{duplicate_of}.pdf")
                    timer.mark("move")
                    logger.info("Duplicate content, matches billId=%s: %s -> %s", result.get("billId"), file_path, dest)
                    tracker.finish(timer, shopname, "duplicate", result.get("billId"))

                elif status == "needs_confirmation":
                    db_session.rollback()
                    adapter.close()
                    timer.mark("process")
                    proposed = result.get("proposed_billId") or result.get("conflicting_billId") or "review"
                    dest = _move_to(app, "NEEDS_REVIEW_FOLDER", shopname, file_path, dest_filename=f"{proposed}.pdf")
                    timer.mark("move")
                    logger.warning("%s needs manual confirmation (%s) -> %s", file_path, result.get("reason"), dest)
                    tracker.finish(timer, shopname, "needs_review", proposed)

                else:
                    db_session.rollback()
                    adapter.close()
                    timer.mark("process")
                    dest = _move_to(app, "FAILED_FOLDER", shopname, file_path, dest_filename=secure_filename(os.path.basename(file_path)))
                    timer.mark("move")
                    logger.error("Unexpected status %r for %s -> %s", status, file_path, dest)
                    tracker.finish(timer, shopname, "failed")

            except (OperationalError, DBAPIError) as db_err:
                # Database is unreachable right now (connection drop, DB not
                # up yet, etc.) -- this says nothing about whether the bill
                # itself is valid, so leave the file where it is and retry
                # later instead of exiling it to failed/.
                db_session.rollback()
                adapter.close()
                logger.warning("DB unavailable while processing %s, will retry later: %s", file_path, db_err)
                timer.mark("process")
                tracker.finish(timer, shopname, "retry_later")

            except Exception:
                # Genuine parsing/data problem -- retrying won't help, so
                # file it away for manual inspection instead of retrying
                # forever.
                db_session.rollback()
                adapter.close()
                logger.exception("Failed to process %s", file_path)
                timer.mark("process")
                try:
                    _move_to(app, "FAILED_FOLDER", shopname, file_path, dest_filename=secure_filename(os.path.basename(file_path)))
                except Exception:
                    logger.exception("Also failed to move %s into the failed/ folder", file_path)
                timer.mark("move")
                tracker.finish(timer, shopname, "failed")
    finally:
        adapter.close()


def scan_incoming_once(app):
    """One-shot sweep of everything already sitting in incoming/<Shop>/ --
    covers files dropped while the watcher wasn't running."""
    incoming_root = app.config["INCOMING_FOLDER"]
    if not os.path.isdir(incoming_root):
        return
    for shop_dir in sorted(os.listdir(incoming_root)):
        full_shop_dir = os.path.join(incoming_root, shop_dir)
        if not os.path.isdir(full_shop_dir):
            continue
        for filename in sorted(os.listdir(full_shop_dir)):
            file_path = os.path.join(full_shop_dir, filename)
            if os.path.isfile(file_path) and filename.lower().endswith(".pdf"):
                process_incoming_file(app, file_path)


class _IncomingPdfHandler(FileSystemEventHandler):
    """Only records when a PDF showed up and queues it; the worker thread
    does the processing. watchdog dispatches events one at a time, so
    processing inside the handler would delay noticing the next file and
    make its measured time look shorter than it really was."""

    def __init__(self, app, work_queue):
        self.app = app
        self.work_queue = work_queue

    def _handle(self, path):
        if not path.lower().endswith(".pdf"):
            return
        self.work_queue.put((path, time.time()))

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def start_incoming_watcher(app):
    """Starts the incoming/ auto-import pipeline as background daemon
    threads and returns immediately -- safe to call once from an app's
    entrypoint (run.py, or watch_incoming.py for standalone use) right
    before app.run()/serving starts. Daemon threads die with the process,
    so this only runs for as long as that process is alive.
    """
    for key in ("INCOMING_FOLDER", "PROCESSED_FOLDER", "DUPLICATES_FOLDER", "NEEDS_REVIEW_FOLDER", "FAILED_FOLDER"):
        os.makedirs(app.config[key], exist_ok=True)
    os.makedirs(os.path.join(app.config["INCOMING_FOLDER"], "Amazon_Visa"), exist_ok=True)

    if not app.config.get("AUTO_IMPORT_USERID"):
        logger.error("AUTO_IMPORT_USERID is not set (see .env) -- incoming/ watcher not started.")
        return

    with app.app_context():
        logger.info("Sweeping existing files in %s ...", app.config["INCOMING_FOLDER"])
        scan_incoming_once(app)

    work_queue = queue.Queue()

    def _worker_loop():
        while True:
            path, detected_at = work_queue.get()
            try:
                with app.app_context():
                    process_incoming_file(app, path, detected_at=detected_at, source="event")
            except Exception:
                logger.exception("Unhandled error processing %s", path)

    threading.Thread(target=_worker_loop, daemon=True, name="incoming-watcher-worker").start()

    observer = Observer()
    observer.schedule(_IncomingPdfHandler(app, work_queue), app.config["INCOMING_FOLDER"], recursive=True)
    observer.daemon = True
    observer.start()

    def _rescan_loop():
        while True:
            time.sleep(RESCAN_INTERVAL_SECONDS)
            with app.app_context():
                scan_incoming_once(app)

    threading.Thread(target=_rescan_loop, daemon=True, name="incoming-watcher-rescan").start()

    logger.info(
        "incoming/ watcher started, watching %s (rescanning every %ss)",
        app.config["INCOMING_FOLDER"], RESCAN_INTERVAL_SECONDS,
    )
