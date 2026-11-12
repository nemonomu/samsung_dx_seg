from __future__ import annotations

import csv
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import compare, datasheet, full_output
from ref import config as ref


def test_shared_pdf_model_in_separate_cell_uses_exact_option():
    target = {"variation_id": "1687958964"}
    ctx = {"model": {"1687958964": {"Modellbezeichnung": "HKGK16155CW"}}}
    ds = {"sku": "HKGK16155CW HKGK16155CB", "rows": [["Modellkennung", "HKGK16155CW, HKGK16155CB"]]}
    assert ref.resolve_sku(target, ds, ctx) == "HKGK16155CW"


def test_full_output_selects_the_requested_model_from_a_shared_ref_datasheet(tmp_path):
    cases = [
        ("1687958964", "HKGK16155CW, HKGK16155CI, HKGK16155CBI", "HKGK16155CW", "HKGK16155CW"),
        ("1844964878", "HFD18983CWDI, HFD18983CWDBI", "HFD18983CWDBI", "HFD18983CWDBI"),
        ("2040612927", "PRCF 1984 E", "PRCF 984 E", "PRCF 1984 E"),
        ("unmatched-option", "HFD18983CWDI, HFD18983CWDBI", "HFD18983CW", ""),
        ("no-comparison", "HFD18983CWDI, HFD18983CWDBI", None, ""),
        ("ambiguous-comparison", "AB123, CD456", "AB123, CD456", ""),
        ("comma-in-model", "ABC123, PRO", "ABC123", "ABC123 PRO"),
        ("no-comma", "ABC123 DEF456", "ABC123", "ABC123 DEF456"),
        ("no-pdf-model", "", "ABC123 weiss", "ABC123"),
    ]
    targets = []
    comparison_cells = {}
    parsed_pdfs = {}
    for rank, (variation_id, pdf_models, option_model, _) in enumerate(cases, 1):
        targets.append({
            "main_rank": str(rank),
            "product_id": "1000",
            "variation_id": variation_id,
            "energy_datasheet_uri": variation_id,
            "retailer_sku_name": "Fridge-freezer 200 l",
        })
        if option_model:
            comparison_cells[variation_id] = {"Modellbezeichnung": option_model}
        rows = [[f"Modellkennung: {pdf_models}", ""]]
        parsed_pdfs[variation_id] = {"rows": rows, "sku": datasheet._sku({}, "", rows)}

    with (tmp_path / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(targets[0]))
        writer.writeheader()
        writer.writerows(targets)

    with (
        patch.object(full_output, "category_output_root", return_value=tmp_path),
        patch.object(compare, "characteristics_map", return_value=comparison_cells),
        patch.object(datasheet, "fetch_datasheet_bytes", side_effect=lambda url, timeout: (url, 200, None)),
        patch.object(datasheet, "parse", side_effect=parsed_pdfs.__getitem__),
        patch.object(full_output, "fetch_similar_product_names", return_value={}),
        patch.object(full_output, "collect_review", return_value={}),
    ):
        full_output.run(ref, pdp_supplement="none", detail_sleep=0)

    with (tmp_path / "otto_full_output.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["sku"] for row in rows] == [expected for _, _, _, expected in cases]
