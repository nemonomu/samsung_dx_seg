"""Explicit, public-field-only events. Never tee third-party stdout or errors."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

_active = None


def emit(event: str, **fields) -> None:
    if _active is not None:
        _active.emit(event, **fields)
    else:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def context(stage: str, **fields) -> None:
    if _active is not None:
        _active.set_context(stage, **fields)


def progress(**fields) -> None:
    if _active is not None:
        with _active.lock:
            _active.fields.update(fields)


def failure(exc: BaseException, *, code: str | None = None) -> None:
    frames = traceback.extract_tb(exc.__traceback__)
    last = frames[-1] if frames else None
    # Raw exception strings/driver output/SQL parameters must never reach logs.
    state = getattr(exc, "pgcode", None)
    emit("failure", error_type=type(exc).__name__, code=code or "UNEXPECTED_ERROR",
         sqlstate=state if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state) else None,
         location=f"{Path(last.filename).name}:{last.lineno}:{last.name}" if last else None)


@contextlib.contextmanager
def quiet_external():
    """Discard UC/library output; our events write directly to the saved screen."""
    with io.StringIO() as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        yield


class RunLog:
    def __init__(self, root: Path, *, heartbeat_s: float = 30):
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.path = root / f"mmkt_dry_{stamp}.log"
        self.handle = self.path.open("x", encoding="utf-8")
        self.screen = sys.stdout
        self.started = time.monotonic()
        self.fields = {"stage": "start"}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.heartbeat_s = heartbeat_s
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def set_context(self, stage, **fields):
        with self.lock:
            self.fields = {"stage": stage, **fields}

    def emit(self, event, **fields):
        with self.lock:
            line = json.dumps({"time": datetime.now().isoformat(timespec="seconds"),
                               "elapsed_s": round(time.monotonic() - self.started, 1),
                               **self.fields, "event": event, **fields}, ensure_ascii=False)
            self.handle.write(line + "\n")
            self.handle.flush()
            print(line, file=self.screen, flush=True)

    def _heartbeat(self):
        while not self.stop.wait(self.heartbeat_s):
            self.emit("waiting", note="현재 단계 처리 중")

    def __enter__(self):
        global _active
        self.previous = _active
        _active = self
        self.thread.start()
        return self

    def __exit__(self, *args):
        global _active
        self.stop.set()
        self.thread.join(timeout=1)
        _active = self.previous
        self.handle.close()
