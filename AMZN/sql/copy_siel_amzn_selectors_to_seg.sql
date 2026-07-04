-- Copy SIEL Amazon XPath selectors into the SEG-owned selector table.
-- Run this in the target PostgreSQL database after confirming both tables exist.

DELETE FROM dx_seg.dx_seg_xpath_selectors
 WHERE site_account = 'Amazon'
   AND page_type IN ('main', 'bsr', 'detail')
   AND domain IN ('tv', 'ref');

INSERT INTO dx_seg.dx_seg_xpath_selectors (
    site_account,
    page_type,
    domain,
    data_field,
    xpath_primary,
    fallback_xpath,
    is_active
)
SELECT
    site_account,
    page_type,
    domain,
    data_field,
    xpath_primary,
    fallback_xpath,
    is_active
  FROM dx_siel_xpath_selectors
 WHERE site_account = 'Amazon'
   AND page_type IN ('main', 'bsr', 'detail')
   AND domain IN ('tv', 'ref')
   AND is_active = TRUE;
