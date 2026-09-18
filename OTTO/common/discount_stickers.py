"""Read circular promotion stickers from existing Crocotile image URLs.

No browser, OCR, credentials, or extra product API request is needed. Unknown
images are retained for manual review, never inferred from deal.highlight.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from common.translate import translate_discount_type

# Visually verified against the TV/REF/LDY listing on 2026-09-18. Different
# colors and the small 'gesponsert' annotation do not change the discount name.
STICKER_LABELS = {
    "mpp360_429990_995702": "Deal & Gewinne",
    "mpp360_413505_922953": "Deal & Gewinne",
    "mpp360_413504_922954": "Deal & Gewinne",
    "mpp360_130782_162312": "Deal des Monats",
    "mpp360_130783_162313": "Deal des Monats",
    "mpp360_130598_161988": "Deal der Woche",
    "mpp360_134491_167816": "Unser Hero",
    "mpp360_435551_1031782": "Unser Hero",
    "mpp360_258249_414645": "Premium Hero",
    "mpp360_292558_476572": "Technik Highlights",
}
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/svg+xml": ".svg",
}
_MAX_IMAGE_BYTES = 2 * 1024 * 1024


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _image_parts(url: str) -> tuple[str, str, bool]:
    try:
        parts = urlsplit(url)
        trusted = (parts.scheme == "https" and parts.netloc == "i.otto.de"
                   and parts.path.startswith("/i/otto/"))
        image_id = parts.path.rsplit("/", 1)[-1]
        # Resize/format parameters do not change the underlying image asset.
        return image_id, parts._replace(query="", fragment="").geturl(), trusted
    except ValueError:
        return "", url, False


def sticker_fields(image_url: Any, deal_id: Any = None) -> dict[str, Any]:
    url = _clean(image_url)
    image_id, _, trusted = _image_parts(url)
    raw = STICKER_LABELS.get(image_id) if trusted else None
    return {
        "discount_type_raw": raw,
        "discount_type": translate_discount_type(raw),
        "discount_type_image_url": url or None,
        "discount_type_image_id": image_id or None,
        "discount_type_deal_id": _clean(deal_id) or None,
        "discount_sticker_status": "known" if raw else ("unknown" if url else "none"),
    }


def refresh_sticker_fields(row: dict[str, Any]) -> None:
    """Allow offline reprocessing after a mapping update; reject legacy highlights."""
    url = row.get("discount_type_image_url")
    legacy = (not url and not row.get("discount_sticker_status")
              and (row.get("discount_type_raw") or row.get("discount_type")))
    if legacy:
        row["discount_type_legacy_raw"] = row.get("discount_type_raw") or row.get("discount_type")
    was_legacy = row.get("discount_sticker_status") == "legacy_missing_image"
    row.update(sticker_fields(url, row.get("discount_type_deal_id")))
    if legacy or (was_legacy and not url):
        row["discount_sticker_status"] = "legacy_missing_image"


def archive_unknown_image(url: str, cache_dir: Path) -> tuple[str | None, str | None]:
    """Download each new image once; failures never abort product collection."""
    _, identity, trusted = _image_parts(url)
    if not trusted:
        return None, "unsupported_image_url"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for extension in _IMAGE_EXTENSIONS.values():
            cached = cache_dir / (key + extension)
            if cached.is_file() and cached.stat().st_size:
                return str(cached), None
        request = Request(url, headers={"Accept": "image/*", "User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            extension = _IMAGE_EXTENSIONS.get(content_type)
            if extension is None:
                return None, "not_an_image"
            body = response.read(_MAX_IMAGE_BYTES + 1)
        if not body or len(body) > _MAX_IMAGE_BYTES:
            return None, "empty_or_oversized_image"
        path = cache_dir / (key + extension)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache_dir, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return str(path), None
    except Exception as exc:
        return None, type(exc).__name__


def sticker_diagnostics(rows: list[dict[str, Any]], *, cache_dir: Path | None = None,
                        product: str = "", log: bool = False) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    legacy = []
    legacy_seen = set()
    unknown_seen = set()
    for row in rows:
        sku_id = _clean(row.get("variation_id") or row.get("product_id") or row.get("item"))
        status = row.get("discount_sticker_status")
        if status == "legacy_missing_image" and sku_id not in legacy_seen:
            legacy_seen.add(sku_id)
            legacy.append({"sku_id": sku_id, "product_url": row.get("product_url"),
                           "legacy_raw": row.get("discount_type_legacy_raw")})
        if status != "unknown":
            continue
        url = _clean(row.get("discount_type_image_url"))
        _, identity, _ = _image_parts(url)
        if identity not in groups:
            image_file, error = row.get("discount_type_image_file"), row.get("discount_type_image_error")
            if cache_dir is not None:
                image_file, error = archive_unknown_image(url, cache_dir)
            groups[identity] = {
                "image_id": row.get("discount_type_image_id"), "image_url": url,
                "image_file": image_file, "image_error": error, "items": [],
            }
        group = groups[identity]
        row["discount_type_image_file"] = group["image_file"]
        row["discount_type_image_error"] = group["image_error"]
        if (identity, sku_id) not in unknown_seen:
            unknown_seen.add((identity, sku_id))
            group["items"].append({
                "sku_id": sku_id, "product_id": row.get("product_id"),
                "product_url": row.get("product_url"), "retailer_sku_name": row.get("retailer_sku_name"),
            })
    if log:
        for group in groups.values():
            print(f"[WARN][discount_type] product={product} unknown_sticker="
                  f"{group['image_id']} sku_ids={','.join(i['sku_id'] for i in group['items'])} "
                  f"image_url={group['image_url']} image_file={group['image_file']} "
                  f"image_error={group['image_error']} discount_type=NULL", flush=True)
        if legacy:
            print(f"[WARN][discount_type] product={product} legacy_missing_image="
                  f"{len(legacy)}; rerun listing to collect sticker images", flush=True)
    return {
        "unknown_image_count": len(groups),
        "unknown_sku_count": len({sku_id for _, sku_id in unknown_seen}),
        "unknown_images": list(groups.values()),
        "legacy_missing_image_count": len(legacy), "legacy_items": legacy,
    }
