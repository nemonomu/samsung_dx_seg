-- Amazon.de SEG XPath selector seed.
-- Apply when you want DB-owned selectors instead of code defaults.

CREATE SCHEMA IF NOT EXISTS dx_seg;

CREATE TABLE IF NOT EXISTS dx_seg.dx_seg_xpath_selectors (
  id BIGSERIAL PRIMARY KEY,
  site_account TEXT NOT NULL,
  page_type TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT 'product',
  data_field TEXT NOT NULL,
  xpath_primary TEXT NOT NULL,
  fallback_xpath TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (site_account, page_type, domain, data_field)
);

CREATE INDEX IF NOT EXISTS idx_dx_seg_xpath_lookup
  ON dx_seg.dx_seg_xpath_selectors (site_account, page_type, domain, is_active);

INSERT INTO dx_seg.dx_seg_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, is_active)
VALUES
  ('Amazon.de', 'main', 'listing', 'base_container', '//div[@data-component-type=''s-search-result'' and @data-asin]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'product_url', './/a[contains(@href,''/dp/'') or contains(@href,''/gp/product/'') or contains(@href,''/sspa/click'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'retailer_sku_name', './/h2//span | .//h2', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'final_sku_price', './/span[contains(@class,''a-price'') and not(contains(@class,''a-text-price''))]//span[contains(@class,''a-offscreen'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'original_sku_price', './/span[contains(@class,''a-text-price'')]//span[contains(@class,''a-offscreen'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'discount_type', './/*[contains(@class,''a-badge-text'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'sku_popularity', './/*[contains(@class,''a-badge-label'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'number_of_units_purchased_past_month', './/span[contains(normalize-space(text()),''gekauft'') or contains(normalize-space(text()),''bought'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'sku_status', './/*[contains(@class,''puis-sponsored-label-text'') or contains(.,''Gesponsert'') or contains(.,''Sponsored'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'star_rating', './/i[contains(@class,''a-icon-star-small'')]//span | .//*[contains(@aria-label,''von 5'') or contains(@aria-label,''out of 5'')][1]', NULL, TRUE),
  ('Amazon.de', 'main', 'listing', 'count_of_star_ratings', './/a[contains(@href,''customerReviews'')]//span[contains(@class,''a-size-base'')][1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'base_container', '//div[starts-with(@id,''gridItemRoot'')] | //li[contains(@class,''zg-item-immersion'')]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'product_url', './/a[contains(@href,''/dp/'') or contains(@href,''/gp/product/'')][1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'retailer_sku_name', './/img[@alt][1] | .//*[contains(@class,''p13n-sc-truncate'')][1] | .//a[contains(@class,''a-link-normal'')]//span[1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'final_sku_price', './/span[contains(@class,''a-price'') and not(contains(@class,''a-text-price''))]//span[contains(@class,''a-offscreen'')][1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'original_sku_price', './/span[contains(@class,''a-text-price'')]//span[contains(@class,''a-offscreen'')][1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'star_rating', './/*[contains(@aria-label,''von 5'') or contains(@aria-label,''out of 5'')][1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'count_of_star_ratings', './/a[contains(@href,''customerReviews'')]//span[1]', NULL, TRUE),
  ('Amazon.de', 'bsr', 'listing', 'bsr_rank', './/*[contains(@class,''zg-bdg-text'')][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'retailer_sku_name', '//*[@id=''productTitle'']', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'product_url', '//link[@rel=''canonical''][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'final_sku_price', '//*[@id=''corePriceDisplay_desktop_feature_div'']//span[contains(@class,''a-price'') and not(contains(@class,''a-text-price''))]//span[contains(@class,''a-offscreen'')][1] | //span[contains(@class,''priceToPay'')]//span[contains(@class,''a-offscreen'')][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'original_sku_price', '//*[@id=''corePriceDisplay_desktop_feature_div'']//span[contains(@class,''a-text-price'')]//span[contains(@class,''a-offscreen'')][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'star_rating', '//*[@id=''averageCustomerReviews'']//*[contains(@class,''a-icon-alt'')][1] | //*[@id=''acrPopover''][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'count_of_star_ratings', '//*[@id=''acrCustomerReviewText''][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'inventory_status', '//*[@id=''availability''][1] | //*[@id=''availabilityInsideBuyBox_feature_div''][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'delivery_availability', '//*[@id=''mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE''][1] | //*[@id=''deliveryBlockMessage''][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'fastest_delivery', '//*[@id=''mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE''][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'available_quantity_for_purchase', '//*[contains(.,''Nur noch'') and contains(.,''auf Lager'')][1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'screen_size', '//*[contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''screen'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''display'')]/following-sibling::*[1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'model_year', '//*[contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''modelljahr'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''model year'')]/following-sibling::*[1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'sku', '//*[contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''modellnummer'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''model number'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''part number'')]/following-sibling::*[1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'estimated_annual_electricity_use', '//*[contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''elektrische leistung'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''wattage'') or contains(translate(normalize-space(.),''ABCDEFGHIJKLMNOPQRSTUVWXYZ'',''abcdefghijklmnopqrstuvwxyz''),''energy'')]/following-sibling::*[1]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'retailer_sku_name_similar', '//*[@id=''sp_detail'']//a[contains(@href,''/dp/'')] | //*[@id=''similarities_feature_div'']//a[contains(@href,''/dp/'')] | //*[@id=''anonCarousel1'']//a[contains(@href,''/dp/'')]', NULL, TRUE),
  ('Amazon.de', 'detail', 'product', 'detailed_review_content', '//div[@data-hook=''review'']//*[@data-hook=''reviewRichContentContainer'' or @data-hook=''reviewText'' or @data-hook=''review-body'']', NULL, TRUE)
ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
  xpath_primary = EXCLUDED.xpath_primary,
  fallback_xpath = EXCLUDED.fallback_xpath,
  is_active = EXCLUDED.is_active,
  updated_at = NOW();
