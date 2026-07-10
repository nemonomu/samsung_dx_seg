-- SEG Amazon TV inventory selector update.
-- Do not populate available_quantity_for_purchase. Store stock quantity/status text in inventory_status.

UPDATE dx_seg.dx_seg_xpath_selectors
SET
    is_active = FALSE,
    updated_at = NOW()
WHERE site_account = 'Amazon'
  AND domain = 'tv'
  AND data_field = 'available_quantity_for_purchase';

UPDATE dx_seg.dx_seg_xpath_selectors
SET
    xpath_primary = '//*[@id="availability"]/span',
    fallback_xpath = NULL,
    is_active = TRUE,
    updated_at = NOW()
WHERE site_account = 'Amazon'
  AND page_type = 'detail'
  AND domain = 'tv'
  AND data_field = 'inventory_status';

INSERT INTO dx_seg.dx_seg_xpath_selectors (
    site_account,
    page_type,
    domain,
    data_field,
    xpath_primary,
    fallback_xpath,
    is_active,
    created_at,
    updated_at
)
SELECT
    'Amazon',
    'detail',
    'tv',
    'inventory_status',
    '//*[@id="availability"]/span',
    NULL,
    TRUE,
    NOW(),
    NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM dx_seg.dx_seg_xpath_selectors
    WHERE site_account = 'Amazon'
      AND page_type = 'detail'
      AND domain = 'tv'
      AND data_field = 'inventory_status'
);

UPDATE dx_seg.dx_seg_xpath_selectors
SET
    xpath_primary = './/span[(contains(normalize-space(.),''Nur noch'') or contains(normalize-space(.),''Only'')) and (contains(normalize-space(.),''auf Lager'') or contains(normalize-space(.),''in stock'') or contains(normalize-space(.),''left in stock''))][1]',
    fallback_xpath = NULL,
    is_active = TRUE,
    updated_at = NOW()
WHERE site_account = 'Amazon'
  AND page_type = 'main'
  AND domain = 'tv'
  AND data_field = 'inventory_status';

INSERT INTO dx_seg.dx_seg_xpath_selectors (
    site_account,
    page_type,
    domain,
    data_field,
    xpath_primary,
    fallback_xpath,
    is_active,
    created_at,
    updated_at
)
SELECT
    'Amazon',
    'main',
    'tv',
    'inventory_status',
    './/span[(contains(normalize-space(.),''Nur noch'') or contains(normalize-space(.),''Only'')) and (contains(normalize-space(.),''auf Lager'') or contains(normalize-space(.),''in stock'') or contains(normalize-space(.),''left in stock''))][1]',
    NULL,
    TRUE,
    NOW(),
    NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM dx_seg.dx_seg_xpath_selectors
    WHERE site_account = 'Amazon'
      AND page_type = 'main'
      AND domain = 'tv'
      AND data_field = 'inventory_status'
);
