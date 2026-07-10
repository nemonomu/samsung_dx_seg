-- SEG Amazon TV detail selector update
-- Rule: estimated_annual_electricity_use uses Jaehrlicher Energieverbrauch first,
-- then Elektrische Leistung when annual energy consumption is absent.

UPDATE dx_seg.dx_seg_xpath_selectors
SET
    xpath_primary = '(//*[not(*) and (contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''jährlicher energieverbrauch'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''jaehrlicher energieverbrauch''))]/following-sibling::*[1])[1]',
    fallback_xpath = '(//*[not(*) and normalize-space(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''))=''elektrische leistung'']/following-sibling::*[1])[1]',
    is_active = TRUE,
    updated_at = NOW()
WHERE site_account = 'Amazon'
  AND page_type = 'detail'
  AND domain = 'tv'
  AND data_field = 'estimated_annual_electricity_use';

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
    'estimated_annual_electricity_use',
    '(//*[not(*) and (contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''jährlicher energieverbrauch'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''jaehrlicher energieverbrauch''))]/following-sibling::*[1])[1]',
    '(//*[not(*) and normalize-space(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''))=''elektrische leistung'']/following-sibling::*[1])[1]',
    TRUE,
    NOW(),
    NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM dx_seg.dx_seg_xpath_selectors
    WHERE site_account = 'Amazon'
      AND page_type = 'detail'
      AND domain = 'tv'
      AND data_field = 'estimated_annual_electricity_use'
);
