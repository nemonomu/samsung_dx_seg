"""Diagnose why estimated_annual_electricity_use / ref_capacity come out NULL.

Run on the machine that produced the empty data:  python diag_datasheet.py [tv|ref]
It reports pdfplumber availability and, for the first few targets, the datasheet
fetch status + parsed power/volume — so a missing dependency vs a fetch/parse
failure is obvious.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import datasheet
from common.io_util import category_output_root

cat = (sys.argv[1] if len(sys.argv) > 1 else "tv").lower()

print("python:", sys.version.split()[0])
try:
    import pdfplumber
    print("pdfplumber: OK", getattr(pdfplumber, "__version__", "?"))
except Exception as exc:
    print("pdfplumber: MISSING ->", repr(exc))
    print(">>> fix: pip install pdfplumber")

targets = category_output_root(cat) / "otto_final_targets.csv"
if not targets.exists():
    print("no targets file:", targets)
    sys.exit(0)

import csv
rows = list(csv.DictReader(open(targets, encoding="utf-8-sig")))
with_uri = [r for r in rows if (r.get("energy_datasheet_uri") or "").strip()]
print(f"\ntargets={len(rows)}  with energy_datasheet_uri={len(with_uri)}")
if not with_uri:
    print(">>> no datasheet URLs captured in listing -> electricity/capacity cannot be parsed")
    sys.exit(0)

for r in with_uri[:3]:
    uri = r["energy_datasheet_uri"].strip()
    body, status, err = datasheet.fetch_datasheet_bytes(uri, 45)
    ds = datasheet.parse(body)
    hdr = datasheet.power_by_label(ds, hdr=True)
    sdr = datasheet.power_by_label(ds, hdr=False)
    print(f"\nrank={r.get('main_rank')} {str(r.get('retailer_sku_name'))[:40]}")
    print(f"  fetch: status={status} bytes={len(body)} err={err}")
    print(f"  parse: items={len(ds.get('items') or {})} rows={len(ds.get('rows') or [])} text_len={len(ds.get('text') or '')} parse_err={ds.get('error')}")
    print(f"  power: hdr={hdr!r} sdr={sdr!r}")
    if cat == "ref":
        print(f"  volume(Gesamtrauminhalt): {datasheet.value_with_unit(ds, 'Gesamtrauminhalt', 'l')!r}")
