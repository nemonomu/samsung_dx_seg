-- SEG Amazon.de structural discount_type selectors for TV and REF.
--
-- The XPath rows identify only the current product's deal badge. Label policy
-- (canonical translations, safe seasonal labels, and coupon/noise rejection)
-- remains in common/translations.py.
--
-- This script changes selector rows only. It does not update historical retail data.

BEGIN;

WITH desired(page_type, xpath_primary, fallback_xpath) AS (
    VALUES
    (
        'main',
        $xpath$(
          .//*[contains(@id,"DEAL_") and contains(@id,"-label")]
          //span[contains(concat(" ",normalize-space(@class)," ")," a-badge-text ")]
        )[1]$xpath$,
        $xpath$(
          .//*[contains(@id,"DEAL_") and contains(@id,"-label")]
        )[1]$xpath$
    ),
    (
        'detail',
        $xpath$(
          //*[@id="apex_desktop"]
          //*[@id="dealBadge_feature_div"]
          //*[@id="dealBadgeSupportingText"]
        )[1]$xpath$,
        $xpath$(
          //*[@id="apex_desktop"]
          //*[@id="dealBadge_feature_div"]
          //*[contains(concat(" ",normalize-space(@class)," ")," dealBadgeTextColor ")
              and not(contains(concat(" ",normalize-space(@class)," ")," aok-hidden "))]
        )[1]$xpath$
    )
)
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
    desired.page_type,
    products.domain,
    'discount_type',
    desired.xpath_primary,
    desired.fallback_xpath,
    TRUE,
    NOW(),
    NOW()
FROM desired
CROSS JOIN (VALUES ('tv'), ('ref')) AS products(domain)
ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
    xpath_primary = EXCLUDED.xpath_primary,
    fallback_xpath = EXCLUDED.fallback_xpath,
    is_active = TRUE,
    updated_at = NOW();

SELECT
    site_account,
    domain,
    page_type,
    data_field,
    xpath_primary,
    fallback_xpath,
    is_active,
    updated_at
FROM dx_seg.dx_seg_xpath_selectors
WHERE site_account = 'Amazon'
  AND domain IN ('tv', 'ref')
  AND page_type IN ('main', 'detail')
  AND data_field = 'discount_type'
ORDER BY domain, page_type;

COMMIT;
