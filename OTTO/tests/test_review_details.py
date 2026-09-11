from __future__ import annotations

import sys
import tempfile
import unittest
from html import escape
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

OTTO_ROOT = Path(__file__).resolve().parents[1]
if str(OTTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OTTO_ROOT))

from common import full_output
from common.parsers import format_detailed_review_content, parse_detail_reviews


def card(review_id: str, text: str, *, css_class: str = "pdp_cr-item-content") -> str:
    return (
        f'<div class="{css_class}" data-review-id="{review_id}" data-rating="5">'
        '<h3 class="js_pdp_cr-item__title">Review title</h3>'
        f'<div class="js_pdp_cr-item__reviewText">{escape(text)}</div></div>'
    )


def gallery(*review_ids: str) -> str:
    return '<div class="pdp_cr-image-gallery">' + ''.join(
        f'<oc-card-v2 class="pdp_cr-image-gallery__image" data-review-id="{rid}"></oc-card-v2>'
        for rid in review_ids
    ) + '</div>'


def review_page(cards: str, *, photos: str = "", popups: str = "", last_page: int = 1) -> str:
    return (
        '<html><body><div class="pdp_cr-rating">'
        '<div class="pdp_cr-rating-score"><span class="oc-headline-300">4.3</span></div>'
        'von 5 (32)</div>'
        f'{photos}<div class="js_pdp_cr-list"><div id="cr-review-list">{cards}</div></div>'
        f'<span class="js_cr_list-wrapper__pagination-last-page">{last_page}</span>'
        f'{popups}</body></html>'
    )


def parse(html: str):
    return parse_detail_reviews(BeautifulSoup(html, "lxml"))


class OttoReviewDetailTests(unittest.TestCase):
    def test_gallery_before_list_does_not_consume_body_ids(self):
        # Structures/counts observed on the reported TV pages; synthetic text.
        cases = [("SONY", 5, 3), ("TCL", 8, 1), ("PHILIPS", 18, 1), ("SAMSUNG", 4, 1)]
        for model, body_count, photo_count in cases:
            with self.subTest(model=model):
                ids = [f"r{i}" for i in range(body_count)]
                rows = parse(review_page(
                    ''.join(card(rid, f"Body {rid}") for rid in ids),
                    photos=gallery(*ids[:photo_count], *ids[:photo_count]),
                ))
                self.assertEqual([row["review_id"] for row in rows], ids)
                self.assertEqual(sum(bool(row["review_text"]) for row in rows), body_count)

    def test_popups_outside_list_do_not_add_or_replace_reviews(self):
        html = review_page(card("r1", "Actual review"), popups=card("r1", "Popup copy") + card("r2", "Popup only"))
        # Include a popup ahead of the list too; it must not win by DOM order.
        rows = parse(card("r1", "Earlier popup") + html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_text"], "Actual review")

    def test_empty_copy_is_replaced_by_body_without_changing_order(self):
        rows = parse(review_page(card("r1", " ") + card("r2", "Second") + card("r1", "First")))
        self.assertEqual([row["review_id"] for row in rows], ["r1", "r2"])
        self.assertEqual(format_detailed_review_content(rows), "review1 - First ||| review2 - Second")

    def test_later_empty_or_duplicate_body_does_not_replace_first_body(self):
        rows = parse(review_page(card("r1", "First") + card("r1", "") + card("r1", "First")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_text"], "First")

    def test_rating_only_card_is_retained_but_not_formatted_as_body(self):
        rows = parse(review_page(card("r1", "") + card("r2", "Written")))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rating"], 5)
        self.assertIsNone(rows[0]["review_text"])
        self.assertEqual(format_detailed_review_content(rows), "review1 - Written")

    def test_same_text_on_different_ids_is_not_deduplicated(self):
        rows = parse(review_page(card("r1", "Good") + card("r2", "Good")))
        self.assertEqual(len(rows), 2)

    def test_missing_ids_do_not_collapse_distinct_cards(self):
        rows = parse(review_page(card("", "First") + card("", "Second")))
        self.assertEqual([row["review_text"] for row in rows], ["First", "Second"])

    def test_legacy_card_inside_list_remains_supported(self):
        rows = parse('<div class="js_pdp_cr-list">' + card("r1", "Legacy", css_class="js_pdp_cr-item") + '</div>')
        self.assertEqual(rows[0]["review_text"], "Legacy")

    def test_gallery_and_popup_only_page_has_no_review_rows(self):
        self.assertEqual(parse(gallery("r1") + card("r1", "Popup only")), [])

    def test_pagination_collects_later_photo_linked_body_and_keeps_twenty_limit(self):
        pages = [
            review_page(card("r1", "Body 1") + card("rating-only", ""),
                        photos=gallery("r1", "r2"), last_page=3),
            review_page(card("r1", "Body 1") + ''.join(card(f"r{i}", f"Body {i}") for i in range(2, 23)),
                        photos=gallery("r2", "r3"), last_page=3),
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(full_output, "fetch_html", side_effect=[
                    {"status": 200, "body": page.encode(), "error": None} for page in pages
                ]) as fetch, \
                patch.object(full_output.raw_html, "save"):
            result = full_output.collect_review(
                "https://example.test/reviews/product/", Path(tmpdir), "product"
            )
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].args[0], "https://example.test/reviews/product/?page=2")
        self.assertEqual(result["detailed_review_content"], ' ||| '.join(
            f"review{i} - Body {i}" for i in range(1, 21)
        ))
        self.assertEqual(result["rating_count"], 32)
        self.assertEqual(result["average_rating"], "4.3")
        ids = [row["review_id"] for row in result["reviews"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
