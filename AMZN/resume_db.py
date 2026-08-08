"""Resume an Amazon SEG batch DB load from an existing JSONL file.

This entry point intentionally runs no crawl and sends no email.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import merge_insert  # noqa: E402


PRODUCT_CONFIGS = {
    "tv": "TV.config",
    "ref": "REF.config",
}


def _load_config(product: str):
    return importlib.import_module(PRODUCT_CONFIGS[product.lower()])


def _record_key(record: dict[str, Any]) -> str:
    return str(record.get("asin") or record.get("item") or "").strip()


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_no}: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"invalid JSONL at line {line_no}: object required")
            records.append(record)
    if not records:
        raise ValueError("JSONL contains no records")
    return records


def validate_resume_jsonl(path: Path, product: str) -> dict[str, Any]:
    records = _read_jsonl_strict(path)
    selected_product = product.upper()
    record_products = {
        str(record.get("product")).strip().upper()
        for record in records
        if record.get("product") not in (None, "")
    }
    if record_products and record_products != {selected_product}:
        raise ValueError(
            f"product mismatch: selected={selected_product} JSONL={sorted(record_products)}"
        )

    target_keys = {
        key
        for record in records
        if record.get("stage") in {"main", "bsr"}
        if (key := _record_key(record))
    }
    details = [record for record in records if record.get("stage") == "detail"]
    if not target_keys:
        raise ValueError("JSONL contains no main/bsr targets")
    if not details:
        raise ValueError("JSONL contains no detail records")

    detail_keys = [_record_key(record) for record in details]
    if any(not key for key in detail_keys):
        raise ValueError("detail record without ASIN/item found")
    duplicate_keys = sorted(key for key, count in Counter(detail_keys).items() if count > 1)
    if duplicate_keys:
        raise ValueError(
            f"duplicate detail ASIN/item found: count={len(duplicate_keys)} first={duplicate_keys[0]}"
        )
    fatal_details = [record for record in details if record.get("_fatal") is True]
    fatal_detail_errors = [
        record
        for record in records
        if record.get("_fatal") is True
        and (
            record.get("stage") == "detail_error"
            or record.get("error_stage") == "detail"
        )
    ]
    if fatal_details or fatal_detail_errors:
        raise ValueError(
            "fatal detail records found: "
            f"count={len(fatal_details) + len(fatal_detail_errors)}"
        )

    detail_key_set = set(detail_keys)
    missing = sorted(target_keys - detail_key_set)
    unexpected = sorted(detail_key_set - target_keys)
    if missing or unexpected:
        raise ValueError(
            "detail completeness check failed: "
            f"targets={len(target_keys)} details={len(detail_key_set)} "
            f"missing={len(missing)} unexpected={len(unexpected)}"
        )

    batch_ids = sorted(
        {
            str(record.get("batch_id")).strip()
            for record in details
            if record.get("batch_id") not in (None, "")
        }
    )
    if len(batch_ids) != 1:
        raise ValueError(f"exactly one detail batch_id required: found={len(batch_ids)}")

    return {
        "records": len(records),
        "targets": len(target_keys),
        "details": len(details),
        "batch_ids": batch_ids,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load an existing Amazon SEG JSONL into DB without crawling or email."
    )
    parser.add_argument("--product", required=True, choices=sorted(PRODUCT_CONFIGS))
    parser.add_argument("--jsonl", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument(
        "--interactive",
        action="store_true",
        help="run dry-run, ask for confirmation, and then insert",
    )
    return parser.parse_args(argv)


def _normalize_jsonl_path(value: Path | str) -> Path:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return Path(text).expanduser().resolve()


def _validate_jsonl_path(jsonl_path: Path) -> None:
    if not jsonl_path.is_file():
        raise ValueError(f"JSONL file not found: {jsonl_path}")
    if jsonl_path.suffix.lower() != ".jsonl":
        raise ValueError(f".jsonl file required: {jsonl_path}")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _execute_resume(
    product: str,
    jsonl_path: Path,
    *,
    dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_jsonl_path(jsonl_path)
    validation = validate_resume_jsonl(jsonl_path, product)
    cfg = _load_config(product)
    manifest = merge_insert.insert_jsonl(
        cfg,
        jsonl_path,
        dry_run=dry_run,
        expected_rows=validation["details"],
    )
    return validation, manifest


def _result(
    product: str,
    jsonl_path: Path,
    dry_run: bool,
    validation: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    success = manifest.get("success") is True
    if not dry_run:
        success = (
            success
            and manifest.get("skipped") is False
            and manifest.get("inserted_total") == validation["details"]
        )
    return {
        "success": success,
        "product": product.upper(),
        "jsonl_path": str(jsonl_path),
        "dry_run": dry_run,
        "targets": validation["targets"],
        "details": validation["details"],
        "rows_full": manifest.get("rows_full"),
        "inserted_total": manifest.get("inserted_total"),
        "skipped": manifest.get("skipped"),
        "manifest": str(
            Path(manifest.get("preview_csv", "")).parent
            / "step14_jsonl_db_save_manifest.json"
        ),
    }


def _print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _run_interactive(args: argparse.Namespace) -> int:
    jsonl_value = args.jsonl
    if jsonl_value is None:
        try:
            jsonl_value = input(
                "JSONL path (you can drag the file into this window): "
            ).strip()
        except EOFError:
            jsonl_value = ""
    if not str(jsonl_value).strip():
        print("[resume-db] JSONL path is required", file=sys.stderr)
        return 2
    jsonl_path = _normalize_jsonl_path(jsonl_value)

    try:
        validation, manifest = _execute_resume(
            args.product,
            jsonl_path,
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001 - interactive CLI needs a clear error.
        print(f"[resume-db] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    dry_result = _result(args.product, jsonl_path, True, validation, manifest)
    _print_result(dry_result)
    if not dry_result["success"]:
        return 1

    checked_digest = _file_digest(jsonl_path)
    try:
        answer = input(
            f"Dry-run passed for {validation['details']} rows. Insert into DB? [y/N]: "
        ).strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("[resume-db] DB insert cancelled")
        return 0
    if _file_digest(jsonl_path) != checked_digest:
        print("[resume-db] JSONL changed after dry-run; DB insert cancelled", file=sys.stderr)
        return 1

    try:
        validation, manifest = _execute_resume(
            args.product,
            jsonl_path,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001 - interactive CLI needs a clear error.
        print(f"[resume-db] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    result = _result(args.product, jsonl_path, False, validation, manifest)
    _print_result(result)
    return 0 if result["success"] else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interactive:
        return _run_interactive(args)
    if args.jsonl is None:
        print("[resume-db] --jsonl is required", file=sys.stderr)
        return 2
    jsonl_path = _normalize_jsonl_path(args.jsonl)

    try:
        validation, manifest = _execute_resume(
            args.product,
            jsonl_path,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must return a clear non-zero result.
        print(f"[resume-db] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    result = _result(args.product, jsonl_path, args.dry_run, validation, manifest)
    _print_result(result)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
