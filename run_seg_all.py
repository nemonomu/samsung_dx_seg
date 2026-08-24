"""Run all SEG crawlers with one timestamped, automatically retained log."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO


LOG_RETENTION_DAYS = 10
_LOG_NAME = re.compile(r"^seg_(\d{8}_\d{6}_\d{6})\.log$")
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(\bauthorization\b\s*[:=]\s*)(?:Bearer\s+)?[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret)\b"
    r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^&\s,;]+)"
)
_URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)[^@\s]+@")


@dataclass(frozen=True)
class Step:
    name: str
    cwd: Path
    command: tuple[str, ...]


def redact_sensitive(text: str) -> str:
    """Prevent common credential forms from being persisted in crawler logs."""
    redacted = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", text)
    redacted = _BEARER_VALUE.sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)
    return _URL_PASSWORD.sub(r"\1[REDACTED]@", redacted)


def cleanup_old_logs(
    log_dir: Path,
    *,
    now: datetime | None = None,
    retention_days: int = LOG_RETENTION_DAYS,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Delete only inactive SEG log files whose run time is older than retention."""
    if retention_days <= 0:
        raise ValueError("retention_days must be greater than zero")
    if not log_dir.exists():
        return (), ()

    cutoff = (now or datetime.now()) - timedelta(days=retention_days)
    deleted: list[Path] = []
    errors: list[str] = []
    try:
        candidates = tuple(log_dir.iterdir())
    except OSError as exc:
        return (), (f"log directory: {exc}",)

    for candidate in candidates:
        match = _LOG_NAME.fullmatch(candidate.name)
        if match is None:
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            run_time = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S_%f")
            modified_time = datetime.fromtimestamp(candidate.stat().st_mtime)
            if run_time >= cutoff or modified_time >= cutoff:
                continue
            candidate.unlink()
            deleted.append(candidate)
        except (OSError, ValueError) as exc:
            errors.append(f"{candidate.name}: {exc}")

    return tuple(deleted), tuple(errors)


def emit(log_file: TextIO, message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {redact_sensitive(message)}"
    print(line, flush=True)
    log_file.write(line + "\n")
    log_file.flush()


def run_step(step: Step, log_file: TextIO) -> int:
    emit(log_file, f"===== START {step.name} =====")
    emit(log_file, f"[{step.name}] command={subprocess.list2cmdline(step.command)}")
    started = time.monotonic()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"

    try:
        process = subprocess.Popen(
            step.command,
            cwd=step.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        emit(log_file, f"[{step.name}] LAUNCH FAILED: {type(exc).__name__}: {exc}")
        emit(log_file, f"===== END {step.name} exit_code=1 =====")
        return 1

    assert process.stdout is not None
    with process.stdout:
        for output_line in process.stdout:
            emit(log_file, f"[{step.name}] {output_line.rstrip()}")
    return_code = process.wait()
    elapsed = time.monotonic() - started
    status = "SUCCESS" if return_code == 0 else "FAILED"
    emit(
        log_file,
        f"===== END {step.name} status={status} exit_code={return_code} elapsed_seconds={elapsed:.1f} =====",
    )
    return return_code


def default_steps(root: Path, python_executable: str = sys.executable) -> tuple[Step, ...]:
    mmkt = root / "MMKT"
    otto = root / "OTTO"
    return (
        Step("MMKT TV", mmkt, (python_executable, "run.py", "--product", "tv", "--concurrency", "1")),
        Step("OTTO TV", otto, (python_executable, "tv\\run.py")),
        Step("MMKT REF", mmkt, (python_executable, "run.py", "--product", "ref", "--concurrency", "1")),
        Step("OTTO REF", otto, (python_executable, "ref\\run.py")),
        Step("MMKT LDY", mmkt, (python_executable, "run.py", "--product", "ldy", "--concurrency", "1")),
        Step("OTTO LDY", otto, (python_executable, "ldy\\run.py")),
    )


def run_all(steps: tuple[Step, ...], log_file: TextIO) -> int:
    failures: list[str] = []
    for step in steps:
        return_code = run_step(step, log_file)
        if return_code != 0:
            failures.append(f"{step.name}({return_code})")

    if failures:
        emit(log_file, f"===== SEG RUN FAILED: {', '.join(failures)} =====")
        return 1
    emit(log_file, "===== SEG RUN SUCCESS =====")
    return 0


def main() -> int:
    root = Path(__file__).resolve().parent
    log_dir = root / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"SEG log directory creation failed: {exc}", file=sys.stderr)
        return 1

    started_at = datetime.now()
    deleted, cleanup_errors = cleanup_old_logs(log_dir, now=started_at)
    log_path = log_dir / f"seg_{started_at:%Y%m%d_%H%M%S_%f}.log"

    try:
        with log_path.open("x", encoding="utf-8-sig", buffering=1) as log_file:
            emit(log_file, "===== SEG RUN START =====")
            emit(log_file, f"log_file={log_path}")
            emit(log_file, f"log_retention_days={LOG_RETENTION_DAYS} removed_old_logs={len(deleted)}")
            for error in cleanup_errors:
                emit(log_file, f"[LOG CLEANUP][WARN] {error}")
            try:
                return run_all(default_steps(root), log_file)
            except KeyboardInterrupt:
                emit(log_file, "===== SEG RUN INTERRUPTED =====")
                return 130
            except Exception as exc:
                emit(log_file, f"===== SEG RUN INTERNAL ERROR: {type(exc).__name__}: {exc} =====")
                for line in traceback.format_exc().splitlines():
                    emit(log_file, f"[TRACEBACK] {line}")
                return 1
    except OSError as exc:
        print(f"SEG log file creation/write failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
