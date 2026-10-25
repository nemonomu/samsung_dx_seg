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
from decimal import Decimal, InvalidOperation
from typing import Any

from common.parsers import _comparison_main, _rating_distribution_stats, text_clean
from common.pdp_browser import _comparison_vars, _reviews_vars
from dry.policy import exclusion_reason
from dry.fields import capacity_candidate, select_fields
from dry.events import emit, failure, progress, quiet_external


class CollectionError(RuntimeError):
    """Only static codes, never raw HTTP/driver/DB exception messages."""


class CommonAPIError(CollectionError):
    """Exhausted retries with evidence of an operation-wide failure."""


def normalized_capacity(raw: Any) -> str | None:
    candidate = capacity_candidate(raw)
    return candidate["value"] if candidate else None


def parse_purchase_status(payload: dict, sku_id: str) -> dict:
    """Retain only observed public availability fields of the exact target.

    Missing status is unknown; malformed present containers/booleans are parsing
    errors. Never read free-text messages, onlineStatus or recommendation state.
    """
    main, _ = _comparison_main(payload, sku_id)
    if main is None:
        raise CollectionError("DETAIL_TARGET_NOT_FOUND")

    def optional_object(parent, key):
        value = parent.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise CollectionError("DETAIL_AVAILABILITY_INVALID")
        return value

    cofr = optional_object(main, "cofrProductAggregate")
    online = optional_object(cofr, "cofrOnlineStatusFeature")
    delivery = optional_object(optional_object(cofr, "cofrDeliveryFeature"), "delivery")
    pickup = optional_object(cofr, "cofrPickupFeature")
    buyable = online.get("isAvailableAndBuyable")
    if buyable is not None and not isinstance(buyable, bool):
        raise CollectionError("DETAIL_AVAILABILITY_INVALID")

    def display_status(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise CollectionError("DETAIL_AVAILABILITY_INVALID")
        # Unknown server enums are not proof of unavailability. Do not emit an
        # arbitrary original value or confuse a diagnostic mask with an enum.
        return value if value in {"NOT_AVAILABLE", "AVAILABLE", "PARTIAL_AVAILABLE", "PARTIALLY_AVAILABLE"} else "UNKNOWN"

    state = {"available_and_buyable": buyable,
             "delivery_status": display_status(delivery.get("displayStatus")),
             "pickup_status": display_status(pickup.get("displayStatus"))}
    state["unavailable"] = (buyable is False and state["delivery_status"] == "NOT_AVAILABLE"
                            and state["pickup_status"] == "NOT_AVAILABLE")
    return state


def parse_specs(payload: dict, sku_id: str, listing_name: str | None = None) -> dict:
    main, _ = _comparison_main(payload, sku_id)
    if main is None:
        raise CollectionError("DETAIL_TARGET_NOT_FOUND")
    # A successful specification response can include other products despite
    # limit=0. Their presence is not an error and must never change the target.
    product = (main.get("productAggregate") or {}).get("product")
    if not isinstance(product, dict) or not product:
        raise CollectionError("DETAIL_PRODUCT_INVALID")
    if "featureGroupsWithProductId" not in product:
        raise CollectionError("DETAIL_FEATURE_GROUPS_MISSING")
    container = product["featureGroupsWithProductId"]
    if container is not None and (not isinstance(container, dict) or "featureGroups" not in container):
        raise CollectionError("DETAIL_FEATURE_GROUPS_INVALID")
    groups = container.get("featureGroups") if container is not None else None
    if groups is not None and not isinstance(groups, list):
        raise CollectionError("DETAIL_FEATURE_GROUPS_INVALID")
    features = {}
    records = []
    for group in groups or []:
        if not isinstance(group, dict) or "features" not in group:
            raise CollectionError("DETAIL_FEATURE_GROUP_INVALID")
        entries = group["features"]
        if entries is not None and not isinstance(entries, list):
            raise CollectionError("DETAIL_FEATURE_GROUP_INVALID")
        for feature in entries or []:
            if not isinstance(feature, dict):
                raise CollectionError("DETAIL_FEATURE_INVALID")
            if feature.get("name") and feature.get("value") is not None:
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
        # Explicitly empty/null specs are valid missing data. Missing keys or
        # malformed containers above are parsing failures, never this fallback.
        emit("specs_empty", item=sku_id, fallback="title_then_null")
    title = text_clean(product.get("title") or product.get("name"))
    fields, sources = select_fields(records, [v for v in (listing_name, title) if v])
    return {**fields, "_sources": sources, "_features": features, "_detail_name": title}


def _dryer_rating_stats(distribution) -> tuple[int | None, float | None]:
    """The API omits zero-count score buckets; validate those it supplies.

    Completing absent buckets with zero is only an in-memory calculation input.
    Never mutate or persist the response, and never accept invalid score/counts.
    The shared parser and other product lines keep their existing behavior.
    """
    if distribution is None or distribution == []:
        return None, None
    if not isinstance(distribution, list) or len(distribution) > 5:
        raise CollectionError("REVIEW_DISTRIBUTION_INVALID")
    counts = {}
    for bucket in distribution:
        if not isinstance(bucket, dict):
            raise CollectionError("REVIEW_DISTRIBUTION_INVALID")
        raw = (bucket.get("value"), bucket.get("count"))
        if any(isinstance(value, bool) or not isinstance(value, (int, float, str)) for value in raw):
            raise CollectionError("REVIEW_DISTRIBUTION_INVALID")
        try:
            score, count = (Decimal(str(value)) for value in raw)
        except (InvalidOperation, ValueError, TypeError):
            raise CollectionError("REVIEW_DISTRIBUTION_INVALID") from None
        if (not score.is_finite() or not count.is_finite()
                or score != score.to_integral_value() or count != count.to_integral_value()
                or not 1 <= score <= 5 or count < 0 or int(score) in counts):
            raise CollectionError("REVIEW_DISTRIBUTION_INVALID")
        counts[int(score)] = int(count)
    return _rating_distribution_stats([{"value": score, "count": counts.get(score, 0)}
                                       for score in range(1, 6)])


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
    rating = root.get("rating")
    if rating is not None and not isinstance(rating, dict):
        raise CollectionError("REVIEW_RATING_INVALID")
    distribution = (rating or {}).get("distribution")
    n_star, star = _dryer_rating_stats(distribution)
    if distribution:
        emit("rating_distribution_parsed", provided_buckets=len(distribution),
             sparse=len(distribution) < 5, count_of_star_ratings=n_star, star_rating=star)
    return {"count_of_reviews": count, "star_rating": star, "count_of_star_ratings": n_star}


class DetailCollector:
    def __init__(self, session, *, retries: int = 2, sleep_s: float = .5):
        self.session = session
        self.retries = retries
        self.sleep_s = sleep_s
        self.cache: dict[str, dict] = {}
        self.api_calls = 0
        self.operation_failures = {}

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
            api_error = ("AUTH_REQUIRED" if status == 401 else "ACCESS_DENIED" if status == 403 else "RATE_LIMITED" if status == 429 else
                         "HTTP_OR_TRANSPORT_FAILED" if status != 200 else "GRAPHQL_ERROR" if errors else
                         "INVALID_JSON" if not isinstance(payload, dict) else
                         "GRAPHQL_DATA_MISSING" if not isinstance(payload.get("data"), dict) else None)
            emit("api_result", item=item, operation=operation, http=status,
                 graphql_errors=bool(errors), attempt=attempt + 1, error_code=api_error)
            if status == 200 and isinstance(payload, dict) and not errors and isinstance(payload.get("data"), dict):
                self.operation_failures.pop(operation, None)
                return payload
            if attempt < self.retries:
                emit("api_retry", item=item, operation=operation, next_attempt=attempt + 2, wait_s=self.sleep_s)
                time.sleep(self.sleep_s)
        emit("api_failed", item=item, operation=operation, attempts=self.retries + 1)
        codes = {entry.get("extensions", {}).get("code") for entry in errors
                 if isinstance(entry, dict) and isinstance(entry.get("extensions"), dict)
                 and isinstance(entry["extensions"].get("code"), str)} if isinstance(errors, list) else set()
        structural = bool(codes & {"GRAPHQL_VALIDATION_FAILED", "PERSISTED_QUERY_NOT_FOUND", "UNAUTHENTICATED"})
        shared = (api_error if api_error in {"AUTH_REQUIRED", "ACCESS_DENIED", "RATE_LIMITED", "INVALID_JSON", "GRAPHQL_DATA_MISSING"}
                  else "SERVER_OR_TRANSPORT_FAILED" if status is None or isinstance(status, int) and status >= 500 else None)
        if shared:
            old_code, items = self.operation_failures.get(operation, (None, set()))
            items = items | {item} if old_code == shared else {item}
            self.operation_failures[operation] = (shared, items)
        else:
            self.operation_failures.pop(operation, None)
            items = set()
        # One product's 403 is still isolated. Identical exhausted failures on
        # two distinct products, or explicit query-validation/auth codes, stop
        # the operation instead of silently skipping the whole catalog.
        if structural or shared and item and len(items) >= 2:
            emit("api_common_failure", operation=operation, item=item, http=status,
                 reason="query_structure_or_auth" if structural else shared,
                 distinct_products=len(items), attempts=self.retries + 1)
            raise CommonAPIError("DETAIL_API_COMMON_FAILURE")
        raise CollectionError("DETAIL_API_FAILED")

    def get(self, sku_id: str, row: dict | None = None) -> dict:
        if sku_id not in self.cache:
            variables = copy.deepcopy(_comparison_vars(sku_id))
            variables["limit"] = 0
            variables["cofrConfig"]["isEnabled"] = False
            payload = self.query("GetComparisonTableRecommendations", variables)
            spec = parse_specs(payload, sku_id,
                               (row or {}).get("retailer_sku_name"))
            for field, record in spec["_sources"].items():
                emit("field_selected", item=sku_id, field=field, **record)
            reason = exclusion_reason(row or {}, features=spec["_features"], detail_name=spec["_detail_name"])
            if reason == "unconfirmed_product_type":
                # Do not cache an unverified product without rating statistics.
                # A later listing occurrence can provide better title evidence.
                raise CollectionError("UNCONFIRMED_PRODUCT_TYPE")
            if not reason:
                spec["_purchase_status"] = parse_purchase_status(payload, sku_id)
                emit("purchase_status", item=sku_id, **spec["_purchase_status"])
            ratings = ({} if reason else
                       parse_ratings(self.query("GetProductReviews", _reviews_vars(sku_id, 1))))
            self.cache[sku_id] = {**spec, **ratings}
            if self.sleep_s:
                time.sleep(self.sleep_s)
        return self.cache[sku_id]
