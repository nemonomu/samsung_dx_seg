"""Flushed UTF-8 events, cumulative stage timings and slow-operation heartbeats."""
import json
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime


class Events:
    def __init__(self, directory, batch_id, heartbeat=10):
        self.batch_id = batch_id
        self.started = time.monotonic()
        self.timings = {}
        self.heartbeat = heartbeat
        self.lock = threading.RLock()
        self.sequence = 0
        self.request_counts, self.cache_hits, self.parse_counts, self.reused_failures = (Counter() for _ in range(4))
        self.log = (directory / "run.log").open("a", encoding="utf-8")
        self.diagnostics = (directory / "diagnostics.jsonl").open("a", encoding="utf-8")

    def emit(self, event, **values):
        with self.lock:
            self.sequence += 1
            record = {"time":datetime.now().astimezone().isoformat(timespec="seconds"), "event":event,
                      "elapsed_seconds":round(time.monotonic() - self.started, 2), **values,
                      "record_id":self.sequence, "batch_id":self.batch_id}
            endpoint = values.get("endpoint")
            counter = {"request_start": self.request_counts, "cache_hit": self.cache_hits,
                       "parsed": self.parse_counts, "failure_reused": self.reused_failures}.get(event)
            if counter is not None and endpoint:
                counter[endpoint] += values.get("count", 1)
            console = record
            if event in {"classification", "field", "rating", "failure", "schema", "summary", "product_result",
                         "listing_candidates", "pdf_attempt", "pdf_page", "pdf_progress", "pdf_sku_candidate",
                         "pdf_capacity_candidate", "pdf_attempt_end"}:
                self.diagnostics.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                self.diagnostics.flush()
                # Keep large evidence in one canonical file, with a stable reference.
                console = {k: v for k, v in record.items() if k not in {"evidence", "candidates", "result", "issues"}}
                console["diagnostic_record"] = record["record_id"]
                if "candidates" in values:
                    console["candidate_count"] = len(values["candidates"])
                if event == "field":
                    winner = next((v for v in values.get("candidates", []) if v.get("source") == values.get("source")
                                   and v.get("normalized") == values.get("selected")), {})
                    console["raw"] = winner.get("raw")
                if values.get("evidence"):
                    console.update(source=values["evidence"][0]["source"], raw=values["evidence"][0]["raw"])
                if "issues" in values:
                    console["issue_reasons"] = [i.get("reason") for i in values["issues"]]
            if event == "pdf_opened":
                self.parse_counts["product_pdf"] += 1
            line = json.dumps(console, ensure_ascii=False, default=str)
            print(line, flush=True)
            self.log.write(line + "\n")
            self.log.flush()

    def statistics(self):
        return dict(requests=dict(self.request_counts), cache_hits=dict(self.cache_hits),
                    parses=dict(self.parse_counts), reused_failures=dict(self.reused_failures))

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
