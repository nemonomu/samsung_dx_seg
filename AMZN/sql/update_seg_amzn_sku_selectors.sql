-- Back up and update SEG Amazon detail SKU selectors.
-- This creates a backup table but does not alter the source selector table schema.

BEGIN;

CREATE TABLE IF NOT EXISTS dx_seg.dx_seg_xpath_selectors_backup AS
SELECT *
FROM dx_seg.dx_seg_xpath_selectors
WITH NO DATA;

INSERT INTO dx_seg.dx_seg_xpath_selectors_backup
SELECT s.*
FROM dx_seg.dx_seg_xpath_selectors AS s
WHERE s.site_account = 'Amazon'
  AND s.page_type = 'detail'
  AND s.domain IN ('tv', 'ref')
  AND s.data_field = 'sku'
  AND NOT EXISTS (
      SELECT 1
      FROM dx_seg.dx_seg_xpath_selectors_backup AS b
      WHERE b.id = s.id
  );

UPDATE dx_seg.dx_seg_xpath_selectors
SET xpath_primary = '(//table//tr[.//*[self::th or self::td or self::span][normalize-space(.)="Hersteller-Modellnummer"]]/td[last()])[1]',
    fallback_xpath = '(//table//tr[.//*[self::th or self::td or self::span][normalize-space(.)="Manufacturer Model Number"]]/td[last()])[1]',
    updated_at = NOW()
WHERE site_account = 'Amazon'
  AND page_type = 'detail'
  AND domain = 'tv'
  AND data_field = 'sku';

UPDATE dx_seg.dx_seg_xpath_selectors
SET xpath_primary = '(//table//tr[.//*[self::th or self::td or self::span][normalize-space(.)="Modellnummer"]]/td[last()])[1]',
    fallback_xpath = '(//table//tr[.//*[self::th or self::td or self::span][normalize-space(.)="Model Number"]]/td[last()])[1]',
    updated_at = NOW()
WHERE site_account = 'Amazon'
  AND page_type = 'detail'
  AND domain = 'ref'
  AND data_field = 'sku';

SELECT id, domain, data_field, xpath_primary, fallback_xpath, is_active, updated_at
FROM dx_seg.dx_seg_xpath_selectors
WHERE site_account = 'Amazon'
  AND page_type = 'detail'
  AND domain IN ('tv', 'ref')
  AND data_field = 'sku'
ORDER BY domain;

COMMIT;

-- Rollback, if needed:
-- BEGIN;
-- UPDATE dx_seg.dx_seg_xpath_selectors AS target
-- SET xpath_primary = backup.xpath_primary,
--     fallback_xpath = backup.fallback_xpath,
--     is_active = backup.is_active,
--     updated_at = backup.updated_at
-- FROM dx_seg.dx_seg_xpath_selectors_backup AS backup
-- WHERE target.id = backup.id;
-- COMMIT;
