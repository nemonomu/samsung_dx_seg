import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ref import config as ref
from ldy import config as ldy


def test_excludes_reported_toys_and_accessories_but_keeps_real_appliances():
    for name in (
        "MUSTERKIND® Kinder-Kühlschrank Ficus, weiß/natur",
    ):
        assert not ref.classify(name)[0]
    for name in (
        "Theo Klein Kinder-Haushaltsset Theo Klein Bosch Waschmaschine, Kinderhaushaltsgerät",
        "Mutig Trocknerball 6 Stück Wäscheball,Mit 4 Wäschesäcken,Öko Waschball für Waschmaschine",
        "MOPUEA Wäschespinne-Schutzhülle Schützhülle für Waschmaschine und Trockner mit Reißverschlus",
    ):
        assert not ldy.classify(name)[0]
    assert ref.classify("GORENJE Kühlschrank R 619 EEW5")[0]
    assert ldy.classify("Bosch Waschmaschine WGG244")[0]
