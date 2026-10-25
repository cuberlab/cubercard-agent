"""The 64 shared FSRS vectors (same file the iOS app and Roa Cards use), one test each."""
import json
from pathlib import Path

import pytest

from cubercard import fsrs

PAYLOAD = json.loads((Path(__file__).parent / "fixtures/fsrs_vectors.json").read_text())
# Server bootstrap `scheduler` (server/src/lib/scheduler.ts) — the CLI must replay with the same values.
SERVER_SCHEDULER = {
    "algorithm": "fsrs-5",
    "params": [0.40255, 1.18385, 3.173, 15.69105, 7.1949, 0.5345, 1.4604, 0.0046, 1.54575, 0.1192, 1.01925, 1.9395,
               0.11, 0.29605, 2.2698, 0.2315, 2.9898, 0.51655, 0.6621],
    "desired_retention": 0.9,
    "learning_steps_min": [1, 10],
    "relearning_steps_min": [10],
    "maximum_interval_days": 36500,
    "day_starts_hour": 4,
}


def test_vector_count_and_params():
    assert len(PAYLOAD["vectors"]) == 64
    assert PAYLOAD["params"] == list(fsrs.PARAMS)
    assert fsrs.scheduler_config() == SERVER_SCHEDULER


@pytest.mark.parametrize("vector", PAYLOAD["vectors"], ids=[v["name"] for v in PAYLOAD["vectors"]])
def test_vector(vector):
    result = fsrs.replay(vector["events"], vector["initial_state"])
    assert set(result) == set(vector["expected"])
    for key, expected in vector["expected"].items():
        if isinstance(expected, float):
            assert result[key] == pytest.approx(expected, abs=5e-7), key
        else:
            assert result[key] == expected, key
