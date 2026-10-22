"""Two necessary persisted queries per target SKU; no full LDY PDP pipeline.

Comparison supplies specifications, requesting limit=0 / COFR disabled. The
server can still include recommendations; only the exact target is processed.
Reviews page 1 supplies totalResults and rating distribution. Persisted queries
have a fixed server selection: incidental fields can arrive in memory but are
never retained, logged or paginated. No AI summary or PDP HTML fetch is used.
"""
from __future__ import annotations

import copy
import time
from typing import Any

from common.parsers import _comparison_main, _rating_distribution_stats, text_clean
from common.pdp_browser import _comparison_vars, _reviews_vars
from dry.policy import exclusion_reason
from dry.fields import capacity_candidate, select_fields
from dry.events import emit, failure, progress, quiet_external


class CollectionError(RuntimeError):
    """Only static codes, never raw HTTP/driver/DB exception messages."""


def normalized_capacity(raw: Any) -> str | None:
    candidate = capacity_candidate(raw)
    return candidate["value"] if candidate else None


def parse_specs(payload: dict, sku_id: str, listing_name: str | None = None) -> dict:
    main, _ = _comparison_main(payload, sku_id)
    if main is None:
        raise CollectionError("DETAIL_TARGET_NOT_FOUND")
    # A successful specification response can include other products despite
    # limit=0. Their presence is not an error and must never change the target.
    product = (main.get("productAggregate") or {}).get("product") or {}
    if not isinstance(product, dict):
        raise CollectionError("DETAIL_PRODUCT_INVALID")
    features = {}
    records = []
    for group in (product.get("featureGroupsWithProductId") or {}).get("featureGroups") or []:
        for feature in group.get("features") or []:
            if isinstance(feature, dict) and feature.get("name") and feature.get("value") is not None:
                if not isinstance(feature["value"], (str, int, float)):
                    raise CollectionError("DETAIL_FEATURE_VALUE_INVALID")
                value = text_clean(feature["value"])
                unit = feature.get("unit")
                if unit is not None and not isinstance(unit, str):
                    raise CollectionError("DETAIL_FEATURE_UNIT_INVALID")
                if not features.get(str(feature["name"])):
                    features[str(feature["name"])] = value
                    records.append({"name": str(feature["name"]), "value": value, "unit": unit})
    if not features:
        raise CollectionError("DETAIL_FEATURES_MISSING")
    title = text_clean(product.get("title") or product.get("name"))
    fields, sources = select_fields(records, [v for v in (listing_name, title) if v])
    return {**fields, "_sources": sources, "_features": features, "_detail_name": title}


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
        item = str(variables.get("ref") or variables.get("id") or "")
        for attempt in range(self.retries + 1):
            self.api_calls += 1
            progress(operation=operation, attempt=attempt + 1)
            emit("api_start", item=item, operation=operation, attempt=attempt + 1)
            try:
                with quiet_external():
                    response = self.session.gql(operation, variables)
            except Exception as exc:
                failure(exc, code="API_TRANSPORT_EXCEPTION")
                response = {}
            if not isinstance(response, dict):
                response = {}
            status = response.get("status")
            payload = response.get("data")
            errors = payload.get("errors") if isinstance(payload, dict) else None
            api_error = ("ACCESS_DENIED" if status == 403 else "RATE_LIMITED" if status == 429 else
                         "HTTP_OR_TRANSPORT_FAILED" if status != 200 else "GRAPHQL_ERROR" if errors else
                         "INVALID_JSON" if not isinstance(payload, dict) else
                         "GRAPHQL_DATA_MISSING" if not isinstance(payload.get("data"), dict) else None)
            emit("api_result", item=item, operation=operation, http=status,
                 graphql_errors=bool(errors), attempt=attempt + 1, error_code=api_error)
            if status == 200 and isinstance(payload, dict) and not errors and isinstance(payload.get("data"), dict):
                return payload
            if attempt < self.retries:
                emit("api_retry", item=item, operation=operation, next_attempt=attempt + 2, wait_s=self.sleep_s)
                time.sleep(self.sleep_s)
        emit("api_failed", item=item, operation=operation, attempts=self.retries + 1)
        raise CollectionError("DETAIL_API_FAILED")

    def get(self, sku_id: str, row: dict | None = None) -> dict:
        if sku_id not in self.cache:
            variables = copy.deepcopy(_comparison_vars(sku_id))
            variables["limit"] = 0
            variables["cofrConfig"]["isEnabled"] = False
            spec = parse_specs(self.query("GetComparisonTableRecommendations", variables), sku_id,
                               (row or {}).get("retailer_sku_name"))
            for field, record in spec["_sources"].items():
                emit("field_selected", item=sku_id, field=field, **record)
            reason = exclusion_reason(row or {}, features=spec["_features"], detail_name=spec["_detail_name"])
            ratings = ({} if reason else
                       parse_ratings(self.query("GetProductReviews", _reviews_vars(sku_id, 1))))
            self.cache[sku_id] = {**spec, **ratings}
            if self.sleep_s:
                time.sleep(self.sleep_s)
        return self.cache[sku_id]
