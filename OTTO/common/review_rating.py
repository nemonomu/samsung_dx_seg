"""Validated review-summary values; no listing or historical-rating fallback."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def rating_count(text: str) -> int | None:
    text = text.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    # Accept grouped thousands, never turn decimal counts such as 20.5 into 205.
    if re.fullmatch(r"\d{1,3}(?P<sep>[.,\s\u00a0\u202f])\d{3}(?:(?P=sep)\d{3})*", text):
        return int(re.sub(r"\D", "", text))
    return None


def parse_rating(soup) -> dict:
    result = {"average_rating": None, "rating_count": None,
              "rating_state": "failed", "rating_reason": None}
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if (any(word in title for word in ("access denied", "captcha", "zugriff verweigert", "just a moment"))
            or soup.select_one('#challenge-form, iframe[src*="captcha"], form[action*="captcha"]')):
        result["rating_reason"] = "blocked_page"
        return result
    wrappers = soup.select(".pdp_cr-rating")
    # These two no-review areas and their wording were observed on a live review page.
    empty = any(re.fullmatch(r"Für diesen Artikel wurde noch keine Bewertung abgegeben\.?",
                            " ".join(n.get_text(" ", strip=True).split()), re.I)
                for n in soup.select(".js_pdp_cr_sidebar__noReviewsText, .cr_list-wrapper--no-review p"))
    if not wrappers:
        if empty and not soup.select_one(".pdp_cr-item-content"):
            result.update(average_rating="0.0", rating_count=0, rating_state="unrated")
        else:
            result["rating_reason"] = "rating_summary_missing"
        return result
    if len(wrappers) != 1 or empty:
        result["rating_reason"] = "ambiguous_rating_summary"
        return result
    wrapper = wrappers[0]
    scores = wrapper.select(".pdp_cr-rating-score")
    reasons = []
    if len(scores) == 1:
        text = " ".join(scores[0].get_text(" ", strip=True).split())
        match = re.fullmatch(r"([0-5](?:[.,]\d+)?)(?:\s+von\s+5)?", text)
        if match and 0 <= float(match[1].replace(",", ".")) <= 5:
            result["average_rating"] = match[1].replace(",", ".")
    if result["average_rating"] is None:
        reasons.append("average_missing_or_invalid")
    counts = re.findall(r"\(([^()]*)\)", wrapper.get_text(" ", strip=True))
    if len(counts) == 1:
        result["rating_count"] = rating_count(counts[0])
    if result["rating_count"] is None:
        reasons.append("count_missing_or_invalid")
    mean, count = result["average_rating"], result["rating_count"]
    if mean is not None and count is not None:
        if float(mean) == 0 and count == 0:
            if soup.select_one(".pdp_cr-item-content"):
                result.update(average_rating=None, rating_count=None)
                reasons.append("zero_summary_with_review_content")
            else:
                result.update(average_rating="0.0", rating_state="unrated")
        elif 1 <= float(mean) <= 5 and count > 0:
            result["rating_state"] = "rated"
        else:
            # A contradictory zero is not a valid independent field.
            if count > 0:
                result["average_rating"] = None
                reasons.append("zero_average_with_ratings")
            else:
                result["rating_count"] = None
                reasons.append("positive_average_without_ratings")
    elif mean is not None and float(mean) < 1:
        result["average_rating"] = None
        reasons.append("unconfirmed_zero_average")
    result["rating_reason"] = ";".join(reasons) or None
    return result


def response_problem(response: dict, requested_url: str) -> str | None:
    status = response.get("status")
    if status != 200:
        return "blocked_http" if status in (401, 403, 429) else (
            f"http_status={status}" if status is not None else "request_failed")
    final_url = response.get("final_url")
    if final_url:
        expected, actual = urlsplit(requested_url), urlsplit(final_url)
        if (actual.hostname != expected.hostname
                or actual.path.rstrip("/") != expected.path.rstrip("/")):
            return "unexpected_review_page"
    return None


def review_product_ids(soup) -> list[str]:
    """Product links in the observed review sidebar, excluding recommendations."""
    result = set()
    for link in soup.select('.js_cr_sidebar a[href*="/p/"]'):
        match = re.search(r"-([A-Z0-9]+)/?$", urlsplit(link.get("href", "")).path, re.I)
        if match:
            result.add(match[1].upper())
    return sorted(result)


def product_problem(parsed: dict, requested_url: str) -> str | None:
    expected = re.fullmatch(r"/kundenbewertungen/([A-Z0-9]+)/?", urlsplit(requested_url).path, re.I)
    observed = parsed.get("review_product_ids") or []
    if expected and observed and observed != [expected[1].upper()]:
        return "unexpected_review_product"
    return None
