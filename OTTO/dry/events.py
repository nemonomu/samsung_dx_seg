"""Flushed UTF-8 events, cumulative stage timings and slow-operation heartbeats."""
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime


class Events:
    def __init__(self, directory, batch_id, heartbeat=10):
        self.batch_id = batch_id
        self.started = time.monotonic()
        self.timings = {}
        self.heartbeat = heartbeat
        self.lock = threading.RLock()
        self.log = (directory / "run.log").open("a", encoding="utf-8")
        self.diagnostics = (directory / "diagnostics.jsonl").open("a", encoding="utf-8")

    def emit(self, event, **values):
        record = dict(time=datetime.now().astimezone().isoformat(timespec="seconds"), event=event,
                      elapsed_seconds=round(time.monotonic() - self.started, 2), **values)
        record["batch_id"] = self.batch_id
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self.lock:
            print(line, flush=True)
            self.log.write(line + "\n")
            self.log.flush()
            if event in {"classification", "field", "rating", "failure", "schema", "summary", "product_result",
                         "pdf_attempt", "pdf_page", "pdf_progress", "pdf_sku_candidate", "pdf_capacity_candidate", "pdf_attempt_end"}:
                self.diagnostics.write(line + "\n")
                self.diagnostics.flush()

    @contextmanager
    def activity(self, name, **values):
        started, done = time.monotonic(), threading.Event()

        def pulse():
            while not done.wait(self.heartbeat):
                self.emit("heartbeat", activity=name,
                          activity_seconds=round(time.monotonic() - started, 2), **values)

        worker = threading.Thread(target=pulse, daemon=True)
        worker.start()
        try:
            yield
        finally:
            done.set()
            worker.join()

    @contextmanager
    def stage(self, name):
        started = time.monotonic()
        child_total = sum(self.timings.values())
        self.emit("stage_start", stage=name)
        try:
            with self.activity(name):
                yield
        finally:
            duration = max(0, time.monotonic() - started - (sum(self.timings.values()) - child_total))
            self.timings[name] = self.timings.get(name, 0) + duration
            self.emit("stage_end", stage=name, seconds=round(duration, 2))

    def close(self):
        self.log.close()
        self.diagnostics.close()
