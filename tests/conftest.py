from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_DATA = ROOT / "test-data"
RECORDINGS = TEST_DATA / "recordings"
EXPECTED = TEST_DATA / "expected.json"


def pytest_collection_modifyitems(config, items):
    if RECORDINGS.is_dir() and EXPECTED.is_file():
        return
    skip = pytest.mark.skip(reason="local-only test-data/ recordings not present")
    for item in items:
        if "recordings" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def root() -> Path:
    return ROOT
