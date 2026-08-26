from meeting_agent.copilot import TRIGGER, _extract_json


def test_trigger_detects_question_and_risk():
    assert TRIGGER.search("Do we know whether production is at risk?")
    assert not TRIGGER.search("Hello everyone and welcome.")


def test_json_fence_parser():
    result = _extract_json('```json\n{"suggestions": []}\n```')
    assert result == {"suggestions": []}
