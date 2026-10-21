import json
from pathlib import Path
import pytest
from agent_state_scenarios import CASES, scenario

BASELINE = Path(__file__).parent / 'fixtures' / 'agent-state-controller-44cc833.json'


@pytest.mark.parametrize('name', CASES)
def test_controller_behavior_matches_pre_refactor_baseline(name):
    expected = json.loads(BASELINE.read_text(encoding='utf-8'))
    assert scenario(name) == expected['cases'][name]
