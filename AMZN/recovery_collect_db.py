"""Interactive recovery of missing Amazon details, followed by a verified DB load.

The source JSONL is immutable. Checkpoints and exports live in a sibling recovery
directory. --check only inspects files and does not import the crawler or DB code.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse


def key(record: dict[str, Any]) -> str:
    return str(record.get("asin") or record.get("item") or "").strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_records(path: Path, *, allow_torn_tail: bool = False) -> tuple[list[dict], bool]:
    lines = path.read_bytes().splitlines(keepends=True)
    records = []
    ignored_tail = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if allow_torn_tail and index == len(lines) - 1 and not line.endswith(b"\n"):
                ignored_tail = True
                continue
            raise ValueError(f"JSONL 손상: {index + 1}번째 줄") from exc
        if not isinstance(record, dict):
            raise ValueError(f"JSONL 객체가 아닌 줄: {index + 1}")
        records.append(record)
    return records, ignored_tail


def inspect_records(records: list[dict], product: str) -> dict[str, Any]:
    listings, details, targets = [], {}, {}
    batches = set()
    for record in records:
        stage = record.get("stage")
        if stage not in {"main", "bsr", "detail", "detail_error"}:
            continue
        if str(record.get("product", "")).upper() != product.upper():
            raise ValueError("상품 구분 불일치: REF/TV에 맞는 BAT와 JSONL을 선택하세요.")
        account = str(record.get("account_name") or "Amazon").lower()
        if account not in {"amazon", "amazon.de", "amzn"}:
            raise ValueError("Amazon 수집 파일이 아닙니다.")
        if record.get("batch_id"):
            batches.add(str(record["batch_id"]))
        item = key(record)
        if not item:
            raise ValueError("ASIN/item이 없는 수집 기록입니다.")
        if stage in {"main", "bsr"}:
            if record.get("_fatal") or record.get("_error"):
                raise ValueError("목록 수집 오류가 있습니다. 이 도구는 상세 수집 복구용입니다.")
            listings.append(record)
            target = targets.setdefault(item, dict(record))
            for field, value in record.items():
                if value not in (None, "") and (not target.get(field) or field in {"main_rank", "bsr_rank"}):
                    target[field] = value
        elif stage == "detail" and not record.get("_fatal") and not record.get("_error"):
            if item in details:
                raise ValueError(f"중복 상세 ASIN: {item}")
            details[item] = record
    if not targets or {row["stage"] for row in listings} != {"main", "bsr"}:
        raise ValueError("MAIN 및 BSR 목록이 모두 있는 원본 JSONL이 필요합니다.")
    if len(batches) != 1:
        raise ValueError("하나의 batch_id만 있는 JSONL이 필요합니다.")
    if set(details) - set(targets):
        raise ValueError("목록에 없는 상세 ASIN이 있습니다.")
    batch_id = next(iter(batches))
    if any(row.get("batch_id") != batch_id for row in details.values()):
        raise ValueError("상세 기록의 batch_id가 누락되었거나 일치하지 않습니다.")
    for rank, target in enumerate((r for r in targets.values() if r.get("main_rank")), 1):
        target["main_rank"] = rank
    missing = [row for item, row in targets.items() if item not in details]
    for row in missing:
        url = urlparse(str(row.get("product_url") or ""))
        if url.scheme not in {"https", "http"} or url.hostname not in {"amazon.de", "www.amazon.de"}:
            raise ValueError(f"Amazon.de 상품 URL이 없습니다: {key(row)}")
    return dict(listings=listings, details=details, targets=targets, missing=missing, batch_id=batch_id)


def atomic_jsonl(path: Path, records: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)


@contextmanager
def recovery_lock(folder: Path):
    """OS-held lock: a terminated process releases it without stale-lock cleanup."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "recovery.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("같은 파일의 복구가 이미 실행 중입니다.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(source: Path, product: str) -> dict[str, Any]:
    if source.suffix.lower() != ".jsonl" or not source.is_file():
        raise ValueError("존재하는 원본 .jsonl 파일을 지정하세요.")
    if source.name == "recovered.jsonl":
        raise ValueError("recovered.jsonl 대신 처음 선택했던 원본 JSONL을 지정하세요.")
    original, torn_tail = read_records(source, allow_torn_tail=True)
    original_state = inspect_records(original, product)
    folder = source.parent / "recovery" / source.stem
    checkpoint = folder / "recovered.jsonl"
    identity = dict(source=str(source), source_sha256=digest(source), product=product.upper())
    state = original_state
    if checkpoint.exists():
        manifest = json.loads((folder / "source.json").read_text(encoding="utf-8"))
        if manifest != identity:
            raise ValueError("원본 파일이 변경되었습니다. 기존 복구 파일과 혼합할 수 없습니다.")
        records, _ = read_records(checkpoint)
        state = inspect_records(records, product)
        if state["listings"] != original_state["listings"] or state["batch_id"] != original_state["batch_id"]:
            raise ValueError("복구 파일의 목록 또는 batch_id가 원본과 다릅니다.")
        for item, record in original_state["details"].items():
            if state["details"].get(item) != record:
                raise ValueError("기존 수집 결과가 복구 파일에서 변경되었습니다.")
    state.update(source=source, folder=folder, checkpoint=checkpoint, identity=identity, torn_tail=torn_tail)
    return state


def save_checkpoint(state: dict) -> None:
    atomic_jsonl(state["checkpoint"], state["listings"] + list(state["details"].values()))


def initialize(state: dict) -> None:
    if not state["checkpoint"].exists():
        manifest = state["folder"] / "source.json"
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(state["identity"], ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, manifest)
        save_checkpoint(state)


@contextmanager
def output_folder(product: str, folder: Path):
    # Reuse the crawler's existing output override only within this CLI process.
    name = f"AMZN_{product.upper()}_OUTPUT_ROOT"
    previous = os.environ.get(name)
    os.environ[name] = str(folder)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def collect_missing(state: dict, product: str, *, headless: bool = False) -> None:
    from common import detail
    from common.io_util import write_csv

    cfg = importlib.import_module(f"{product.upper()}.config")
    pending = {key(row) for row in state["missing"]}
    write_csv(state["folder"] / "amzn_final_targets.csv", state["missing"])

    def emit(record: dict) -> None:
        if record.get("stage") != "detail" or record.get("_fatal") or record.get("_error"):
            return
        item = key(record)
        if item not in pending or item in state["details"]:
            raise ValueError("미수집 대상이 아닌 상세 결과가 반환되었습니다.")
        if record.get("batch_id") != state["batch_id"] or record.get("product") != product.upper():
            raise ValueError("수집 결과의 상품 또는 batch_id가 일치하지 않습니다.")
        state["details"][item] = record
        save_checkpoint(state)
        print(f"복구 진행: {len(state['details'])}/{len(state['targets'])}", flush=True)

    with output_folder(product, state["folder"]):
        manifest = detail.run(cfg, batch_id=state["batch_id"], emit=emit, headless=headless)
    if manifest.get("success") is not True or not pending.issubset(state["details"]):
        raise ValueError("상세 수집이 완료되지 않았습니다. DB 적재를 중단합니다.")


def export_and_load(state: dict, product: str, *, dry_run: bool) -> dict:
    import resume_db
    from common.io_util import write_csv

    validation = resume_db.validate_resume_jsonl(state["checkpoint"], product)
    if validation["targets"] != len(state["targets"]):
        raise ValueError("전체 대상 건수가 일치하지 않습니다.")
    cfg = resume_db._load_config(product)
    with output_folder(product, state["folder"]):
        write_csv(state["folder"] / "amzn_final_targets.csv", list(state["targets"].values()))
        write_csv(state["folder"] / "amzn_detail.csv", list(state["details"].values()))
        manifest = resume_db.merge_insert.insert_jsonl(
            cfg, state["checkpoint"], dry_run=dry_run, expected_rows=validation["details"],
        )
    if manifest.get("success") is not True or manifest.get("rows_full") != validation["details"]:
        raise ValueError("전체 결과 생성 또는 DB 적재 검증에 실패했습니다.")
    if not dry_run and (manifest.get("skipped") is not False or manifest.get("inserted_total") != validation["details"]):
        raise ValueError("DB 적재 건수가 일치하지 않거나 적재가 생략되었습니다.")
    return manifest


def confirm(message: str) -> bool:
    try:
        return input(message + " [Y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def describe(state: dict, product: str) -> None:
    print(f"\nAmazon {product.upper()} 수집 복구 및 DB 적재")
    print("상세 수집이 중간에 멈췄을 때 사용합니다. 목록은 다시 수집하지 않습니다.")
    print(f"배치: {state['batch_id']}")
    print(f"전체 {len(state['targets'])}건 / 완료 {len(state['details'])}건 / 미수집 {len(state['missing'])}건")
    print(f"복구 결과 폴더: {state['folder']}")
    if state["torn_tail"]:
        print("원본 마지막 줄이 잘려 있습니다. 해당 줄은 제외하고 미완료 상품을 다시 수집합니다.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, choices=["ref", "tv"])
    parser.add_argument("--jsonl", type=Path)
    parser.add_argument("--check", action="store_true", help="파일 확인만 수행; 수집/파일 쓰기/DB 접속 없음")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = str(args.jsonl) if args.jsonl else input("원본 JSONL 파일을 이 창에 끌어다 놓고 Enter: ").strip()
        if not value:
            raise ValueError("원본 JSONL 경로를 입력하세요.")
        source = Path(value.strip().strip('"').strip("'")).expanduser().resolve()
        state = load_state(source, args.product)
        describe(state, args.product)
        if args.check:
            return 0
        initial_digest = state["identity"]["source_sha256"]
        if not confirm("미수집 복구를 시작할까요?" if state["missing"] else "수집이 완료되어 있습니다. DB 적재 전 검증을 진행할까요?"):
            print("취소했습니다. 수집 및 DB 적재를 실행하지 않았습니다.")
            return 0
        with recovery_lock(state["folder"]):
            state = load_state(source, args.product)
            if state["identity"]["source_sha256"] != initial_digest:
                raise ValueError("확인 중 원본이 변경되었습니다. 실행 중인 수집이 끝난 뒤 다시 시도하세요.")
            initialize(state)
            if state["missing"]:
                collect_missing(state, args.product, headless=args.headless)
            state = load_state(source, args.product)
            if state["missing"]:
                raise ValueError("미수집 항목이 남아 있어 DB 적재를 중단합니다.")
            export_and_load(state, args.product, dry_run=True)
            checked_digest = digest(state["checkpoint"])
            print(f"전체 {len(state['targets'])}건 검증 완료. 동일 batch_id의 Amazon 데이터는 교체됩니다.")
            if not confirm(f"{len(state['targets'])}건을 DB에 적재할까요?"):
                print("DB 적재를 취소했습니다. 복구 결과는 저장되어 있습니다.")
                return 0
            if digest(source) != initial_digest or digest(state["checkpoint"]) != checked_digest:
                raise ValueError("검증 후 파일이 변경되어 DB 적재를 중단합니다.")
            export_and_load(state, args.product, dry_run=False)
            print(f"완료: {len(state['targets'])}건 DB 적재 성공. 결과: {state['folder']}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"복구 중단: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("저장된 복구 결과는 유지됩니다. 같은 BAT에서 같은 원본 JSONL을 선택해 다시 실행하세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
