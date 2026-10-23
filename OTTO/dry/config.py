"""DRY settings: no credential loading and no shared pipeline side effects."""
from pathlib import Path

from common.io_util import COUNTRY, RETAILER

ACCOUNT_NAME = RETAILER
DEFAULT_LIMIT = 100
DEFAULT_TABLE = "dx_seg.dx_seg_ldy_dryer_retail_test"
OUTPUT_ROOT = Path(__file__).resolve().parent / "data" / "output"
SEARCH_TERM = "trockner"
SEARCH_URL = "https://www.otto.de/suche/trockner/"
COLUMNS = (
    "id", "country", "account_name", "item", "sku", "retailer_sku_name",
    "product_url", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "main_rank", "bsr_rank",
    "calendar_week", "crawl_datetime", "batch_id", "loading_type", "capacity",
)
OPTIONAL_COLUMNS = {
    "sku", "count_of_reviews", "star_rating", "count_of_star_ratings",
    "final_sku_price", "original_sku_price", "savings", "loading_type", "capacity",
}
CAPACITY_LABELS = (
    "Füllmenge Baumwolle (Trocknen)", "Nennkapazität (Trocknen)",
    "Kapazität Trocknen", "Trockenkapazität", "Beladungsmenge", "Füllmenge",
    "Fassungsvermögen", "Nennkapazität", "Ladevolumen in kg",
)
TYPE_LABELS = ("Produkttyp", "Geräteart", "Produktart", "Set-Bestandteile")
CATEGORY_LABELS = ("Kategorie", "Produktkategorie")
COMPARE_LABELS = ("Modellbezeichnung", "Beladung", "Bauart", *CAPACITY_LABELS,
                  *TYPE_LABELS, *CATEGORY_LABELS)


class DryError(Exception):
    """Only fixed, non-sensitive codes may be exposed to users."""
