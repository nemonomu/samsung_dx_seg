"""Killable PDF request/parse attempt; transport only bounded bytes and field results."""
import multiprocessing
import time
from contextlib import contextmanager


class _ForwardEvents:
    def __init__(self, connection):
        self.connection = connection

    def emit(self, event, **values):
        self.connection.send(("event", event, values))

    @contextmanager
    def activity(self, name, **values):
        yield  # The supervising process owns heartbeat and hard deadlines.


def _pdf_worker(connection, uri, body, needed, request_timeout):
    from dry.client import Client, RequestFailure, parse_pdf
    events = _ForwardEvents(connection)
    try:
        if body is None:
            try:
                body, _ = Client(events, timeout=request_timeout, pause=0).fetch(
                    uri, "product_pdf", kind="pdf", attempts=1)
            except RequestFailure as exc:
                status = exc.status
                connection.send(("done", {"_error": str(exc), "_error_kind": "request",
                    "_retryable": status is None or status in (200, 408, 425, 429) or status >= 500}))
                return
            if not body.startswith(b"%PDF"):
                connection.send(("done", {"_error": "pdf_document_invalid", "_error_kind": "extraction", "_retryable": True}))
                return
            connection.send(("body", body))  # Memory cache, never logged or saved.
        connection.send(("parse",))
        result = parse_pdf(body, need_sku=needed["sku"], need_capacity=needed["capacity"],
                           need_loading=needed["loading_type"], progress=events.emit)
        connection.send(("done", result))
    except Exception as exc:
        connection.send(("done", {"_error": "pdf_worker:" + type(exc).__name__, "_error_kind": "extraction", "_retryable": False}))
    finally:
        connection.close()


def pdf_attempt(uri, body, needed, request_timeout, parse_timeout, progress):
    """Stop stalled DNS/HTTP/PDF work, keeping completed-page fields and PDF bytes."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_pdf_worker, args=(sender, uri, body, needed, request_timeout), daemon=True)
    result = {}
    opened = False
    stage = "parse" if body is not None else "request"
    deadline = time.monotonic() + (parse_timeout if body is not None else request_timeout)
    started = False
    try:
        worker.start()
        started = True
        sender.close()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                result.update(_error="pdf_" + stage + "_timeout", _error_kind="request" if stage == "request" else "extraction", _retryable=True)
                break
            if not receiver.poll(min(left, 0.1)):
                if not worker.is_alive():
                    result.update(_error="pdf_worker_exited", _error_kind="extraction", _retryable=True)
                    break
                continue
            try:
                message = receiver.recv()
            except EOFError:
                result.update(_error="pdf_worker_exited", _error_kind="extraction", _retryable=True)
                break
            if message[0] == "body":
                body = message[1]
            elif message[0] == "parse":
                if stage != "parse":
                    stage, deadline = "parse", time.monotonic() + parse_timeout
            elif message[0] == "event":
                _, event, values = message
                if event == "pdf_opened":
                    opened = True
                if event == "pdf_progress":
                    result.update(values["result"])
                progress(event, **values)
            elif message[0] == "done":
                result.update(message[1])
                break
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(_error="pdf_process:" + type(exc).__name__, _error_kind="extraction", _retryable=False)
    finally:
        if started:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=1)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=1)
            worker.close()
        receiver.close()
        sender.close()
    if result.get("_error", "").startswith("pdf_open:") or (result.get("_error") == "pdf_parse_timeout" and not opened):
        body = None  # An unreadable document was not successfully acquired.
    return result, body
