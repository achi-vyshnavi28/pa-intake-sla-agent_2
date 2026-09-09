import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def full_pipeline():
    """Regenerate the entire pipeline once per test session so every test
    module runs against a fresh, consistent state: synthetic data -> ETL
    -> agent. This is what lets the detection-performance tests measure
    real precision/recall against the ground-truth fixture rather than
    asserting hand-typed numbers."""
    import data.generate_data as gen
    import etl.staging_to_core as etl
    import agent.umguard as umguard

    gen.generate()
    etl.run()
    umguard.run()
    return True


@pytest.fixture(scope="session")
def ground_truth():
    return json.loads((ROOT / "data" / "ground_truth.json").read_text())


@pytest.fixture(scope="session")
def core_db_path():
    return ROOT / "data" / "amaranth_pa_core.db"


@pytest.fixture(scope="session")
def output_dir():
    return ROOT / "output"
