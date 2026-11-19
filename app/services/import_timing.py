# app/services/import_timing.py
"""
Timing instrumentation for the incoming/ auto-import pipeline.

Per file, it records how long each stage took, from the moment the watcher
noticed the PDF in incoming/<Shop>/ until it was moved into processed/
(or duplicates/, needs_review/, failed/):

  queue_wait  -- detected -> worker picked it up (non-zero when several
                 files arrive at once, since files are processed one by one)
  stable_wait -- waiting for the file size to stop changing
  process     -- parsing + dedupe + DB insert/commit
  move        -- moving the file into its outcome folder
  total       -- detected -> done (the end-to-end time for that file)

Each file becomes one row in <INCOMING_ROOT>/logs/import_timings.csv and one
"TIMING" log line.

Files are grouped into batches: a batch starts with the first file detected
while no batch is open, and closes once nothing has been detected or in
progress for BATCH_IDLE_SECONDS. When it closes, one "BATCH" log line and one
row in import_batches.csv give the file count, wall-clock time (first
detected -> last done) and the average per file.
"""

import csv
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

BATCH_IDLE_SECONDS = 10

FILE_FIELDS = [
    "batch_id", "detected_at", "finished_at", "shop", "filename", "outcome", "bill_id",
    "size_kb", "queue_wait_s", "stable_wait_s", "process_s", "move_s", "total_s", "source",
]
BATCH_FIELDS = [
    "batch_id", "started_at", "finished_at", "files", "wall_s",
    "avg_total_s", "avg_process_s", "max_total_s", "outcomes",
]

_csv_lock = threading.Lock()


def _ts(epoch):
    return datetime.fromtimestamp(epoch).isoformat(timespec="milliseconds")


def _append_csv(path, fields, row):
    with _csv_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if new_file:
                writer.writeheader()
            writer.writerow(row)


class FileTimer:
    """Stopwatch for one PDF. Call mark(stage) at the end of each stage."""

    def __init__(self, file_path, detected_at, source):
        self.file_path = file_path
        self.detected_at = detected_at
        self.source = source
        self.started_at = time.time()
        self._last = time.perf_counter()
        self.stages = {"queue_wait": max(0.0, self.started_at - detected_at)}
        self.size_kb = None

    def mark(self, stage):
        now = time.perf_counter()
        self.stages[stage] = self.stages.get(stage, 0.0) + (now - self._last)
        self._last = now
        if stage == "stable_wait":  # file is fully written now, so its size is final
            try:
                self.size_kb = round(os.path.getsize(self.file_path) / 1024, 1)
            except OSError:
                pass


class BatchTracker:
    def __init__(self, log_dir):
        self.file_csv = os.path.join(log_dir, "import_timings.csv")
        self.batch_csv = os.path.join(log_dir, "import_batches.csv")
        self._lock = threading.Lock()
        self._in_progress = 0
        self._batch = None
        self._close_timer = None

    def start(self, file_path, detected_at=None, source="event"):
        """Begin timing one file. detected_at defaults to now."""
        detected_at = detected_at or time.time()
        with self._lock:
            if self._close_timer:
                self._close_timer.cancel()
                self._close_timer = None
            if self._batch is None:
                self._batch = {
                    "id": datetime.fromtimestamp(detected_at).strftime("%Y%m%d-%H%M%S"),
                    "started_at": detected_at,
                    "files": [],
                }
            self._batch["started_at"] = min(self._batch["started_at"], detected_at)
            self._in_progress += 1
            batch_id = self._batch["id"]
        timer = FileTimer(file_path, detected_at, source)
        timer.batch_id = batch_id
        return timer

    def finish(self, timer, shop, outcome, bill_id=None, dest_path=None):
        """Record one file as done. Outcome is processed/duplicate/needs_review/failed/retry_later/skipped."""
        finished_at = time.time()
        total = finished_at - timer.detected_at
        row = {
            "batch_id": timer.batch_id,
            "detected_at": _ts(timer.detected_at),
            "finished_at": _ts(finished_at),
            "shop": shop,
            "filename": os.path.basename(timer.file_path),
            "outcome": outcome,
            "bill_id": bill_id or "",
            "size_kb": timer.size_kb,
            "queue_wait_s": round(timer.stages.get("queue_wait", 0.0), 3),
            "stable_wait_s": round(timer.stages.get("stable_wait", 0.0), 3),
            "process_s": round(timer.stages.get("process", 0.0), 3),
            "move_s": round(timer.stages.get("move", 0.0), 3),
            "total_s": round(total, 3),
            "source": timer.source,
        }
        try:
            _append_csv(self.file_csv, FILE_FIELDS, row)
        except Exception:
            logger.exception("Could not write import timing row")
        logger.info(
            "TIMING %s [%s] %s: total=%.2fs (queue=%.2fs stable=%.2fs process=%.2fs move=%.2fs)",
            row["filename"], shop, outcome, total,
            row["queue_wait_s"], row["stable_wait_s"], row["process_s"], row["move_s"],
        )

        with self._lock:
            self._in_progress = max(0, self._in_progress - 1)
            if self._batch is not None and outcome != "skipped":
                self._batch["files"].append({**row, "_finished": finished_at, "_total": total})
            if self._in_progress == 0:
                self._close_timer = threading.Timer(BATCH_IDLE_SECONDS, self._close_batch)
                self._close_timer.daemon = True
                self._close_timer.start()

    def _close_batch(self):
        with self._lock:
            if self._in_progress or self._batch is None:
                return
            batch, self._batch, self._close_timer = self._batch, None, None
        files = batch["files"]
        if not files:
            return
        finished_at = max(f["_finished"] for f in files)
        wall = finished_at - batch["started_at"]
        outcomes = {}
        for f in files:
            outcomes[f["outcome"]] = outcomes.get(f["outcome"], 0) + 1
        row = {
            "batch_id": batch["id"],
            "started_at": _ts(batch["started_at"]),
            "finished_at": _ts(finished_at),
            "files": len(files),
            "wall_s": round(wall, 3),
            "avg_total_s": round(sum(f["_total"] for f in files) / len(files), 3),
            "avg_process_s": round(sum(f["process_s"] for f in files) / len(files), 3),
            "max_total_s": round(max(f["_total"] for f in files), 3),
            "outcomes": " ".join(f"{k}={v}" for k, v in sorted(outcomes.items())),
        }
        try:
            _append_csv(self.batch_csv, BATCH_FIELDS, row)
        except Exception:
            logger.exception("Could not write import batch row")
        logger.info(
            "BATCH %s: %d file(s) in %.2fs wall time (avg %.2fs/file end-to-end, avg %.2fs parse+DB) [%s]",
            row["batch_id"], row["files"], wall, row["avg_total_s"], row["avg_process_s"], row["outcomes"],
        )
