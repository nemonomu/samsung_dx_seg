-- SEG Amazon.de discount_type selector whitelist for TV and REF.
--
-- Verified on live Amazon.de main/detail pages before this migration:
--   * keep: Befristetes Angebot, Zeitlich begrenztes Angebot,
--           Limited Time Offer, Hot deal, Limited time deal,
--           Endet in ... / Angebot endet in ... / Ends in ...
--   * reject: Du zahlst ... Coupon ... angewendet, Amazon's Choice/Tipp,
--             Prime Exclusive Offer, Top Offer, and all other promotions.
--
-- This script changes selector rows only. It does not update historical retail data.

BEGIN;

WITH desired(page_type, xpath_primary) AS (
    VALUES
    (
        'main',
        $xpath$.//*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[
            translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time offer"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="hot deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="ends in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"ends in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="befristetes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="zeitlich begrenztes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"endet in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="angebot endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"angebot endet in ")
        ]$xpath$
    ),
    (
        'detail',
        $xpath$//*[@id="dealBadgeSupportingText"][
            translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time offer"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="hot deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="ends in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"ends in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="befristetes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="zeitlich begrenztes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"endet in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="angebot endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"angebot endet in ")
        ]
        |
        //*[@id="dealBadge_feature_div"]//span[
            translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time offer"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="hot deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="ends in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"ends in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="befristetes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="zeitlich begrenztes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"endet in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="angebot endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"angebot endet in ")
        ]
        |
        //*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[
            translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time offer"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="hot deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="limited time deal"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="ends in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"ends in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="befristetes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="zeitlich begrenztes angebot"
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"endet in ")
            or translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="angebot endet in"
            or starts-with(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"angebot endet in ")
        ]$xpath$
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
    NULL,
    TRUE,
    NOW(),
    NOW()
FROM desired
CROSS JOIN (VALUES ('tv'), ('ref')) AS products(domain)
ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
    xpath_primary = EXCLUDED.xpath_primary,
    fallback_xpath = NULL,
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
