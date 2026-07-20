"""Warmed browser session that collects a full MediaMarkt PDP in one visit.

MediaMarkt's lazy GraphQL endpoint (/api/v1/graphql) only answers 200 inside a
warmed real-browser session (cf_clearance cookie) — a bare ZenRows GET replay is
rejected (422). So we connect a real browser via the ZenRows scraping browser
(CDP, proxy_country=de; local IP never exposed), warm it once, and per PDP:
  1. navigate → capture the SSR HTML (9 in-page fields via parse_pdp_html)
  2. run in-page fetch() against the persisted GraphQL queries to pull the 3
     lazy fields (KI summary, top-20 reviews, Alternativen) — the fetch inherits
     the page's cf_clearance cookies, so it succeeds where an external GET can't.

Persisted-query hashes are PWA-build specific; re-capture with
step00_capture_pdp_har.py if they start returning 404/PersistedQueryNotFound.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
from urllib.parse import urlencode

from common.zenrows import DEFAULT_PROXY_COUNTRY, build_scraping_browser_url

MMKT_HOME = "https://www.mediamarkt.de/"
GRAPHQL_ENDPOINT = "https://www.mediamarkt.de/api/v1/graphql"
# App headers the MediaMarkt PWA sends with every GraphQL call. Without the
# x-operation + x-mms-* set the endpoint returns 403 (the browser auto-adds
# referer / user-agent / sec-ch-ua, so those are omitted here).
GRAPHQL_BASE_HEADERS = {
    "apollographql-client-name": "pwa-client-pqm",
    "apollographql-client-version": (
        os.getenv("MMKT_GRAPHQL_CLIENT_VERSION") or "8.461.2"
    ).strip(),
    "accept": "*/*",
    "content-type": "application/json",
    "x-mms-country": "DE",
    "x-mms-language": "de",
    "x-mms-salesline": "Media",
}
CONSENT_SELECTORS = (
    "#pwa-consent-layer-accept-all",
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
)
DETAIL_SIGNALS = ("Modelljahr", "Bildschirmdiagonale", "Leistungsaufnahme")

# Persisted queries verified against a real PDP (build 0ef32db, 2026-07-15).
PERSISTED = {
    "GetReviewsSummary": "273a424b84399fff753d18781c2c6bf872169c4216566b9ce22f1879d9366726",
    "GetProductReviews": "0b159c715666ae7dbd6d0c632743eae31886b749a93e50280d39897f491d46f7",
    "GetComparisonTableRecommendations": "e28d11ae56a8af43d659d312250d3dae0fea3d953ef6e43f54b7b5ba99001e89",
}

# cofrConfig block shared by the recommendation queries.
_COFR_CONFIG = {
    "isEnabled": True, "baseDomain": "https://www.mediamarkt.de", "channel": "DESKTOP",
    "isLegacyDataExcluded": False,
    "features": {
        "badges": {"isFreeShippingBadgeIncluded": False},
        "crossSalesLine": {"isEnabled": True, "isOutputForced": False},
        "onlineStatus": {"isPermanentlyNaIndexEnabled": True},
        "pickup": {"isStrictPickupDisplayStatusEnabled": False},
        "price": {
            "strikePriceTypes": [
                {"strikePriceType": "lop"},
                {"strikePriceType": "rrp", "shouldBeStruck": True, "showDiscountBadge": True,
                 "isLegalTextInlineAllowed": False},
            ],
            "isBasePriceRequiredFlagRespected": False, "isDiscountLabelEnabled": True,
            "isDiscountPercentageShown": True, "isDisplayPriceWithStrikePriceRrpThemed": True,
            "isLongerStrikePricePrefixAllowed": False, "isPromoPriceFiltered": True,
            "isPromoPriceUsedAsDisplayPriceInApp": False, "isHistoryChartEnabled": False,
            "discountPercentageMinimum": 10, "discountPercentageMinimumFractionDigits": 0,
        },
        "delivery": {"isDeliveryStatusByEarliestDateEnabled": True, "isLocationSourcingEnabled": True,
                     "isLocationSourcingMarketplaceEnabled": True},
        "refurbishedGoods": {"isEnabled": True},
    },
    "client": {},
}


def _reviews_vars(sku_id: str, page: int) -> dict[str, Any]:
    return {"reviewPage": page, "includeMedia": False, "filterInput": {},
            "id": str(sku_id), "sortingType": "nativeLocale"}


def _summary_vars(sku_id: str) -> dict[str, Any]:
    return {"productId": str(sku_id), "formatType": "paragraph"}


def _comparison_vars(sku_id: str) -> dict[str, Any]:
    return {
        "hasMarketplace": True, "isCustomerBehaviorInfluenceActive": True, "locale": "de-DE",
        "salesLine": "Media", "isRefurbishedGoodsActive": True, "isPdpFaqSectionActive": True,
        "shouldIncludeYourekoRatingExp1150": True, "isDemonstrationModelAvailabilityActive": True,
        "isCrossLinkingActive": False, "isPdpLoyaltyPointsActive": True,
        "isRepairabilityIndexActive": False, "touchpoint": "WEB_DESKTOP", "ref": str(sku_id),
        "limit": 5, "recommendationContext": "PRODUCT", "type": "ALTERNATIVES",
        "cofrConfig": _COFR_CONFIG,
    }


# The PWA app appends this "pwa" context block to every GraphQL extensions param;
# the server returns 500 (INTERNAL_SERVER_ERROR) without it.
_PWA_EXT = {
    "captureChannel": "DESKTOP", "salesLine": "Media", "country": "DE", "language": "de",
    "globalLoyaltyProgram": True, "isOneAccountProgramActive": True,
    "shouldInactiveContractsBeHidden": True, "isUsingXccCustomerComponent": True,
    "isCheckoutPhoneCompareActive": True,
}


def build_gql_url(operation: str, variables: dict[str, Any]) -> str:
    ext = {
        "persistedQuery": {"version": 1, "sha256Hash": PERSISTED[operation]},
        "pwa": _PWA_EXT,
    }
    return GRAPHQL_ENDPOINT + "?" + urlencode({
        "operationName": operation,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(ext, separators=(",", ":")),
    })


def detail_present(html: str) -> bool:
    return any(sig in html for sig in DETAIL_SIGNALS)


class PdpBrowserSession:
    """Connect once, warm up, then fetch each PDP's HTML + lazy GraphQL data."""

    def __init__(
        self,
        *,
        proxy_country: str = DEFAULT_PROXY_COUNTRY,
        nav_timeout_ms: int = 90000,
        settle_ms: int = 1200,
        warmup_wait_ms: int = 3000,
        review_pages: int = 4,
        block_resources: bool = True,
    ) -> None:
        self.proxy_country = proxy_country
        self.nav_timeout_ms = nav_timeout_ms
        self.settle_ms = settle_ms
        self.warmup_wait_ms = warmup_wait_ms
        self.review_pages = review_pages
        self.block_resources = block_resources
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.warmup_status: dict[str, Any] = {}

    def __enter__(self) -> "PdpBrowserSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(
            build_scraping_browser_url(proxy_country=self.proxy_country)
        )
        self._context = self._browser.new_context(
            locale="de-DE", timezone_id="Europe/Berlin",
            viewport={"width": 1440, "height": 1600},
        )
        if self.block_resources:
            self._context.route("**/*", self._route_filter)
        self._page = self._context.new_page()
        self._warmup()

    # Drop heavy resources we never read — the SSR __PRELOADED_STATE__ is inline
    # HTML and our GraphQL data comes from fetch(), so images/media/fonts/css are
    # pure waste through the remote proxy. Scripts are kept (Cloudflare clearance).
    _BLOCK_TYPES = {"image", "media", "font", "stylesheet"}

    def _route_filter(self, route) -> None:
        try:
            if route.request.resource_type in self._BLOCK_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def reconnect(self) -> None:
        """Tear down a dead session and open a fresh warmed one (new ZenRows IP)."""
        self.close()
        self.open()

    def _click_consent(self) -> str | None:
        for sel in CONSENT_SELECTORS:
            try:
                loc = self._page.locator(sel).first
                if loc.count():
                    loc.click(timeout=3000)
                    self._page.wait_for_timeout(700)
                    return sel
            except Exception:
                continue
        return None

    def _warmup(self) -> None:
        status: dict[str, Any] = {"home_status": None, "consent": None, "error": None}
        try:
            home = self._page.goto(MMKT_HOME, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            status["home_status"] = home.status if home else None
            status["consent"] = self._click_consent()
            self._page.wait_for_timeout(self.warmup_wait_ms)
        except Exception as exc:
            status["error"] = type(exc).__name__ + ": " + str(exc)
        self.warmup_status = status

    def _build_call(self, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": build_gql_url(operation, variables),
            "headers": {
                **GRAPHQL_BASE_HEADERS,
                "x-operation": operation,
                "x-flow-id": str(uuid.uuid4()),
                "x-cacheable": "false" if operation == "GetReviewsSummary" else "true",
            },
        }

    def _gql_fetch_many(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fire several in-page fetch() calls concurrently (Promise.all)."""
        try:
            return self._page.evaluate(
                """async (calls) => Promise.all(calls.map(async ({url, headers}) => {
                    try {
                        const r = await fetch(url, {headers, credentials: 'include'});
                        let body = null;
                        try { body = await r.json(); } catch (e) {}
                        return {status: r.status, body};
                    } catch (e) { return {status: null, error: String(e)}; }
                }))""",
                calls,
            )
        except Exception as exc:
            return [{"status": None, "error": type(exc).__name__ + ": " + str(exc)}] * len(calls)

    def fetch_page_response(self, url: str) -> dict[str, Any]:
        """In-page fetch() of a page URL → raw SSR HTML text (page cookies, no
        navigation). Used to recover fields that live only in the PDP description
        body (e.g. REF ref_capacity)."""
        try:
            res = self._page.evaluate(
                """async (url) => {
                    try { const r = await fetch(url, {credentials: 'include'});
                          let b = null; try { b = await r.text(); } catch (e) {}
                          return {status: r.status, body: b}; }
                    catch (e) { return {status: null, error: String(e)}; }
                }""",
                url,
            )
        except Exception as exc:
            return {
                "status": None,
                "body": "",
                "error": type(exc).__name__ + ": " + str(exc),
            }
        if not isinstance(res, dict):
            return {"status": None, "body": "", "error": "invalid fetch response"}
        return {
            "status": res.get("status"),
            "body": res.get("body") or "",
            "error": res.get("error"),
        }

    def fetch_page_text(self, url: str) -> str:
        """Compatibility wrapper for description-only recovery callers."""
        return self.fetch_page_response(url)["body"]

    def fetch_pdp_detail(self, url: str, sku_id: str) -> dict[str, Any]:
        """GraphQL-ONLY (no navigation): one concurrent batch of comparison +
        summary + reviews. The comparison response carries the main product's
        specs/delivery/pickup/ratings + similar."""
        started = time.perf_counter()
        # Fire all GraphQL queries for this PDP concurrently in one round trip.
        calls = [self._build_call("GetComparisonTableRecommendations", _comparison_vars(sku_id))]
        calls.append(self._build_call("GetReviewsSummary", _summary_vars(sku_id)))
        calls += [
            self._build_call("GetProductReviews", _reviews_vars(sku_id, p))
            for p in range(1, self.review_pages + 1)
        ]
        results = self._gql_fetch_many(calls)
        comparison = results[0]
        summary = results[1]
        review_pages = results[2:2 + self.review_pages]

        def _body(r: dict[str, Any] | None) -> Any:
            return r.get("body") if isinstance(r, dict) else None

        return {
            "sku_id": sku_id,
            "url": url,
            "nav_status": (comparison or {}).get("status"),
            "detail_present": (comparison or {}).get("status") == 200,
            "html": "",
            "summary_resp": _body(summary),
            "review_resps": [_body(r) for r in review_pages],
            "comparison_resp": _body(comparison),
            "gql_status": {
                "comparison": (comparison or {}).get("status"),
                "summary": (summary or {}).get("status"),
                "reviews": [(r or {}).get("status") for r in review_pages],
            },
            "error": None,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }

    def close(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._browser, "close", None)):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None
