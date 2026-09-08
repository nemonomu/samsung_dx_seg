-- Add the untranslated main-page quantity field for TV and REF.
-- Run in the target PostgreSQL database, then restart the crawler to reload selectors.
-- Leaves inventory_status, sku_status, and historical retail rows unchanged.
BEGIN;

UPDATE dx_seg.dx_seg_xpath_selectors
SET xpath_primary = './/span[contains(concat('' '', normalize-space(@class), '' ''), '' a-color-price '') and ((starts-with(normalize-space(.),''Nur noch '') and contains(normalize-space(.),''auf Lager'')) or (starts-with(normalize-space(.),''Only '') and contains(normalize-space(.),''left in stock'')))][1]',
    fallback_xpath = './/span[@aria-label and ((starts-with(normalize-space(.),''Nur noch '') and contains(normalize-space(.),''auf Lager'')) or (starts-with(normalize-space(.),''Only '') and contains(normalize-space(.),''left in stock'')))][1]',
    is_active = TRUE
WHERE site_account = 'Amazon'
  AND domain IN ('tv', 'ref')
  AND page_type = 'main'
  AND data_field = 'available_quantity_for_purchase';

INSERT INTO dx_seg.dx_seg_xpath_selectors (
    site_account, domain, page_type, data_field,
    xpath_primary, fallback_xpath, is_active
)
SELECT 'Amazon', d.domain, 'main', 'available_quantity_for_purchase',
       './/span[contains(concat('' '', normalize-space(@class), '' ''), '' a-color-price '') and ((starts-with(normalize-space(.),''Nur noch '') and contains(normalize-space(.),''auf Lager'')) or (starts-with(normalize-space(.),''Only '') and contains(normalize-space(.),''left in stock'')))][1]',
       './/span[@aria-label and ((starts-with(normalize-space(.),''Nur noch '') and contains(normalize-space(.),''auf Lager'')) or (starts-with(normalize-space(.),''Only '') and contains(normalize-space(.),''left in stock'')))][1]',
       TRUE
FROM (VALUES ('tv'), ('ref')) AS d(domain)
WHERE NOT EXISTS (
    SELECT 1 FROM dx_seg.dx_seg_xpath_selectors AS s
    WHERE s.site_account = 'Amazon' AND s.domain = d.domain
      AND s.page_type = 'main'
      AND s.data_field = 'available_quantity_for_purchase'
);

SELECT site_account, domain, page_type, data_field,
       xpath_primary, fallback_xpath, is_active
FROM dx_seg.dx_seg_xpath_selectors
WHERE site_account = 'Amazon' AND domain IN ('tv', 'ref')
  AND page_type = 'main' AND data_field = 'available_quantity_for_purchase'
ORDER BY domain;

COMMIT;
