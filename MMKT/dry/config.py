"""DRY settings. Shared configuration imports do not read DB secrets."""
from pathlib import Path
from common.config import ACCOUNT_NAME, COUNTRY, product_output_root

LISTING_URL = "https://www.mediamarkt.de/de/category/trockner-4.html?query=W%C3%A4schetrockner"
BSR_URL = LISTING_URL + "&sort=salescount+desc"
MAIN_TARGET_UNIQUE = 100
BSR_TARGET_UNIQUE = 100
OUTPUT_ROOT = product_output_root("dry")
LOG_ROOT = Path(__file__).resolve().parent / "data" / "logs"
DB_TABLE = ("dx_seg", "dx_seg_ldy_dryer_retail_test")
PRODUCTION_TABLE = ("dx_seg", "dx_seg_ldy_dryer_retail")

COLUMNS = (
    "id", "country", "account_name", "item", "sku", "retailer_sku_name",
    "product_url", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "main_rank", "bsr_rank",
    "calendar_week", "crawl_datetime", "batch_id", "loading_type", "capacity",
)

# Dryer labels confirmed on public MediaMarkt technical-data tables; verify
# their presence in the remote API on the first DRY run. No wash capacity alias.
CAPACITY_FEATURES = ("Füllmenge Baumwolle (Trocknen)", "Füllmenge Baumwolle (Trocknen) in kg", "Nennkapazität")
LOADING_FEATURES = ("Beladung",)
LOADING_TRANSLATIONS = {"frontlader": "Front load", "toplader": "Top load"}
