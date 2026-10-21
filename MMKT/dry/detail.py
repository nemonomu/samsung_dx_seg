"""Two necessary persisted queries per target SKU; no full LDY PDP pipeline.

Comparison supplies specifications, with recommendation limit=0 / COFR disabled.
Reviews page 1 supplies totalResults and rating distribution. Persisted queries
have a fixed server selection: incidental fields can arrive in memory but are
never retained, logged or paginated. No AI summary or PDP HTML fetch is used.
"""
from __future__ import annotations

import copy
import re
import time
from typing import Any

from common.parsers import _comparison_main, _rating_distribution_stats, _sku_from_features_or_name, text_clean
from common.pdp_browser import _comparison_vars, _reviews_vars
from dry import config
from dry.policy import exclusion_reason, normalized


class CollectionError(RuntimeError):
    """Only static codes, never raw HTTP/driver/DB exception messages."""


def normalized_capacity(raw: Any) -> str | None:
    value = text_clean(raw)
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(?:kg)?", value or "", re.I)
    return match.group(1) + "kg" if match else None


def parse_specs(payload: dict, sku_id: str) -> dict:
    main, others = _comparison_main(payload, sku_id)
    if main is None:
        raise CollectionError("DETAIL_TARGET_NOT_FOUND")
    if others:
        raise CollectionError("DETAIL_RECOMMENDATION_LIMIT_IGNORED")
    product = (main.get("productAggregate") or {}).get("product") or {}
    if not isinstance(product, dict):
        raise CollectionError("DETAIL_PRODUCT_INVALID")
    features = {}
    for group in (product.get("featureGroupsWithProductId") or {}).get("featureGroups") or []:
        for feature in group.get("features") or []:
            if isinstance(feature, dict) and feature.get("name") and feature.get("value") is not None:
                if not features.get(str(feature["name"])):
                    features[str(feature["name"])] = text_clean(feature["value"])
    if not features:
        raise CollectionError("DETAIL_FEATURES_MISSING")
    title = text_clean(product.get("title") or product.get("name"))
    capacity = next((normalized_capacity(features.get(key)) for key in config.CAPACITY_FEATURES
                     if normalized_capacity(features.get(key)) is not None), None)
    raw_loading = next((features.get(key) for key in config.LOADING_FEATURES if features.get(key)), None)
    return {"sku": _sku_from_features_or_name(features, title),
            "loading_type": config.LOADING_TRANSLATIONS.get(normalized(raw_loading)),
            "capacity": capacity, "_features": features, "_detail_name": title}


def parse_ratings(payload: dict) -> dict:
    data = payload.get("data")
    root = data.get("reviews") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        raise CollectionError("REVIEW_STATISTICS_MISSING")
    if not {"totalResults", "rating"}.intersection(root):
        raise CollectionError("REVIEW_STATISTICS_MISSING")
    count = root.get("totalResults")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise CollectionError("REVIEW_COUNT_INVALID")
    rating = root.get("rating") or {}
    n_star, star = _rating_distribution_stats(rating.get("distribution"))
    return {"count_of_reviews": count, "star_rating": star, "count_of_star_ratings": n_star}


class DetailCollector:
    def __init__(self, session, *, retries: int = 2, sleep_s: float = .5):
        self.session = session
        self.retries = retries
        self.sleep_s = sleep_s
        self.cache: dict[str, dict] = {}
        self.api_calls = 0

    def query(self, operation: str, variables: dict) -> dict:
        for attempt in range(self.retries + 1):
            self.api_calls += 1
            try:
                response = self.session.gql(operation, variables)
            except Exception:
                response = {}
            status = response.get("status")
            payload = response.get("data")
            errors = payload.get("errors") if isinstance(payload, dict) else None
            print(f"[dry-api] operation={operation} http={status} graphql_errors={bool(errors)} attempt={attempt + 1}", flush=True)
            if status == 200 and isinstance(payload, dict) and not errors and isinstance(payload.get("data"), dict):
                return payload
            if attempt < self.retries:
                time.sleep(self.sleep_s)
        raise CollectionError("DETAIL_API_FAILED")

    def get(self, sku_id: str, row: dict | None = None) -> dict:
        if sku_id not in self.cache:
            variables = copy.deepcopy(_comparison_vars(sku_id))
            variables["limit"] = 0
            variables["cofrConfig"]["isEnabled"] = False
            spec = parse_specs(self.query("GetComparisonTableRecommendations", variables), sku_id)
            if spec["sku"] is None and row:
                spec["sku"] = _sku_from_features_or_name({}, row.get("retailer_sku_name"))
            reason = exclusion_reason(row or {}, features=spec["_features"], detail_name=spec["_detail_name"])
            ratings = ({} if reason else
                       parse_ratings(self.query("GetProductReviews", _reviews_vars(sku_id, 1))))
            self.cache[sku_id] = {**spec, **ratings}
            if self.sleep_s:
                time.sleep(self.sleep_s)
        return self.cache[sku_id]
