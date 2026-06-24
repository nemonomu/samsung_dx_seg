# SEG Retail.com Page Structure Scan

## Inputs
- Workbook: `C:\samsung_dx_seg\deloitte.xlsx`
- ZenRows docs used locally: `C:\zenrows_doc\universal-scraper-api`, `C:\zenrows_doc\api-error-codes.md`
- ZenRows request baseline used for scan: `mode=auto`, `proxy_country=de`, `wait=5000`; API key read from `.env` at runtime only.

## Workbook Schema
- Total fields: 59
- Page groups:
  - `MediaMarkt / Main Page` (6): `retailer_sku_name`, `savings`, `original_sku_price`, `final_sku_price`, `sku_status`, `discount_type`
  - `MediaMarkt / Product Page` (12): `delivery_availability`, `pick_up_availability`, `retailer_sku_name_similar`, `screen_size`, `sku`, `estimated_annual_electricity_use`, `model_year`, `star_rating`, `count_of_star_ratings`, `count_of_reviews`, `summarized_review_content`, `detailed_review_content`
  - `MediaMarkt / BSR Page` (1): `bsr_rank`
  - `OTTO / Main Page` (7): `retailer_sku_name`, `final_sku_price`, `original_sku_price`, `savings`, `sku_popularity`, `sku_status`, `discount_type`
  - `OTTO / Product Page` (11): `delivery_availability`, `sku`, `screen_size`, `estimated_annual_electricity_use`, `retailer_sku_name_similar`, `star_rating`, `count_of_star_ratings`, `count_of_reviews`, `recommendation_intent`, `summarized_review_content`, `detailed_review_content`
  - `OTTO / BSR Page` (1): `bsr_rank`
  - `Amazon.de / Main Page` (8): `retailer_sku_name`, `final_sku_price`, `original_sku_price`, `discount_type`, `sku_popularity`, `number_of_units_purchased_past_month`, `sku_status`, `available_quantity_for_purchase`
  - `Amazon.de / Product Page` (12): `delivery_availability`, `fastest_delivery`, `inventory_status`, `screen_size`, `model_year`, `sku`, `estimated_annual_electricity_use`, `retailer_sku_name_similar`, `star_rating`, `count_of_star_ratings`, `summarized_review_content`, `detailed_review_content`
  - `Amazon.de / BSR Page` (1): `bsr_rank`

## Live Scan Artifacts
- Raw HTML and scan JSON: `C:\samsung_dx_seg\artifacts\page_structure_scan`
- Parsed schema rows: `C:\samsung_dx_seg\artifacts\deloitte_schema_rows.json`
- Product card samples: `C:\samsung_dx_seg\artifacts\page_structure_samples.json`
- Label contexts: `C:\samsung_dx_seg\artifacts\page_structure_contexts.json`

## ZenRows Fetch Status
| Target | Status | Bytes | Product candidates | Note |
|---|---:|---:|---:|---|
| `mediamarkt_main` | 200 | 1999946 | 12 | OK |
| `otto_main` | 200 | 605440 | 114 | OK |
| `amazon_de_main` | 200 | 1946308 | 59 | OK |
| `mediamarkt_product_sample` | 200 | 1336054 | 0 | OK |
| `otto_product_sample` | 422 | 148 | 0 | Failed: {"code":"RESP001","instance":"/v1","status":422,"title":"Could not get content (RESP001)","type":"https://docs.zenrows.c |
| `amazon_de_product_sample` | 200 | 2967018 | 0 | OK |

## MediaMarkt Structure
- Main URL from workbook: `https://www.mediamarkt.de/de/category/fernseher-nach-gr%C3%B6%C3%9Fen-4708.html`; workbook says sort by Best result and Berlin ZIP `10117`.
- Main product grid is server-rendered enough for parsing: `article[data-test="mms-product-card"]` returned 12 cards in the first scan.
- Main card text contains listing fields: `retailer_sku_name`, `savings`, `original_sku_price`, `final_sku_price`, `sku_status`/badges, rating count, delivery/pickup snippets. Product links follow `/de/product/_...html`.
- Product page sample succeeded. Key selectors/signals: `h1` for product name, `data-test="mms-product-price"` for price block, `data-test^="mms-cofr-delivery_"` for delivery status, `table tr` for technical specs and energy fields, `data-test="mms-customer-rating"` for rating block.
- Product page text has energy labels such as `Energieverbrauch ... kWh pro 1 000 h`; `estimated_annual_electricity_use` should be parsed from the technical table, not from listing snippets.
- BSR evidence is weak in the live sample: a `Bestseller` badge exists, but no numeric rank was found in the first product sample. Treat `bsr_rank` as requiring a separate confirmation pass.

## OTTO Structure
- Main URL from workbook: `https://www.otto.de/suche/fernseher/`; workbook says sort by `Topseller` and collect first 300 SKUs.
- Main product grid is accessible: `article[data-qa="reptile-product-tile"]` returned 120 cards; page count text showed `2.475 Produkte`.
- Each card carries stable identifiers: `data-article-number`, `data-product-id`, `data-variation-id`, `data-list-position`, `data-local-list-position`, `data-origin`. These should be persisted as provenance.
- Main card text covers `retailer_sku_name`, `final_sku_price`, `original_sku_price`, `savings`, `sku_popularity` (`Sehr beliebt`), `sku_status` (`gesponsert`, `Fast ausverkauft`), and delivery lead time.
- OTTO also embeds many `application/ld+json` Product blocks and a large `application/json; format=devalue` route payload under `/dundee/tilelist?...sortiertnach=topseller`; this is the best candidate for structured listing extraction.
- Product detail is not yet accessible through Universal Scraper API in this scan. Variants `mode=auto`, `mode=auto` without wait, `js_render=true&premium_proxy=true`, and `premium_proxy=true` all returned ZenRows `RESP001`. The docs describe `RESP001` as could-not-get-content; for OTTO detail, next investigation should use Scraping Browser/session or identify OTTO route/API payloads from the static JS.
- BSR for OTTO is probably list rank under `sortiertnach=topseller` (`data-list-position`), but this needs explicit acceptance because the workbook calls it `BSR Page` while no separate BSR URL was provided.

## Amazon.de Structure
- Main URL from workbook: `https://www.amazon.de/s?k=fernseher&ref=nb_sb_ss`; workbook says sort by Featured and collect first 300 SKUs.
- Main search result grid is accessible: valid records are `div.s-result-item[data-asin][data-component-type="s-search-result"]` with non-empty `data-asin`.
- Main cards contain title, price, RRP, discount, ratings, `bought in past month`, sponsored/featured flags, and product links under `/dp/{ASIN}`.
- Product page sample succeeded. Key selectors/signals: `#productTitle`, `#corePriceDisplay_desktop_feature_div`, `#availability`, `#averageCustomerReviews`, `#acrCustomerReviewText`, `#feature-bullets`; BSR/review/energy text exists in page body but needs exact selector hardening per ASIN.
- Amazon listing has many sponsored/brand modules mixed with organic results. The crawler should filter blank `data-asin`, ad containers without product detail URL, and non-TV accessories before counting toward the 300 SKU target.

## Immediate Engineering Notes
- Build pipeline around three page roles from the workbook: Main listing -> Product detail -> BSR/rank evidence.
- Store source URL, list position, retailer IDs (`data-asin`, OTTO variation ID, MediaMarkt article number if parsed) with every row; they are needed for DB merge/retry provenance.
- Use ZenRows Universal Scraper API for MediaMarkt/Amazon initial collection. For OTTO listing, Universal Scraper API is enough; for OTTO detail, plan a fallback before committing parser design.
- Do not treat listing delivery as final for Product Page delivery fields unless the field is explicitly defined as Main Page. Product-page delivery and pickup should be validated from detail pages or accepted listing fallback rules.
