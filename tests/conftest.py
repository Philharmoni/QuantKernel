import csv
from pathlib import Path

import pytest


@pytest.fixture
def golden_root():
    return Path(__file__).parent / "fixtures" / "golden"


@pytest.fixture
def raw_rows(golden_root):
    def read(table):
        with (golden_root / f"{table}.csv").open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))
    return read
