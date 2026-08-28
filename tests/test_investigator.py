import json

from meeting_agent.config import Settings
from meeting_agent.investigator import Investigator, ToolRegistry


def _project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "storage.py").write_text(
        "def append_transcript(event):\n    return event\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    return Investigator(Settings(), tmp_path)


# ----------------------------------------------------------------- registry


def test_a_connector_registers_without_touching_the_loop():
    """Jira and BigQuery arrive this way: a schema and a handler, nothing else."""
    registry = ToolRegistry()
    registry.register(
        "jira_issue",
        "Fetch a Jira issue.",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        lambda key: f"issue {key}: open",
    )

    assert registry.schemas[0]["name"] == "jira_issue"
    assert registry.call("jira_issue", {"key": "ENG-1"}) == "issue ENG-1: open"


def test_an_unknown_or_broken_tool_never_ends_the_investigation():
    registry = ToolRegistry()
    registry.register("boom", "Explodes.", {"type": "object", "properties": {}}, _explode)

    assert "No such tool" in registry.call("absent", {})
    assert "boom failed" in registry.call("boom", {})
    assert "Bad arguments" in registry.call("boom", {"unexpected": 1})


def _explode():
    raise RuntimeError("upstream is down")


# -------------------------------------------------------------- local tools


def test_local_tools_are_registered_with_schemas(tmp_path):
    names = {schema["name"] for schema in _project(tmp_path).tools.schemas}

    assert names == {"search_project", "read_project_file", "git_history", "search_past_meetings"}


def test_reading_outside_the_project_or_a_secret_is_refused(tmp_path):
    investigator = _project(tmp_path)

    for attempt in ("../outside.py", "/etc/passwd", ".env"):
        assert "refused" in investigator.read_project_file(attempt).lower()
    assert "sk-secret" not in investigator.read_project_file(".env")


def test_search_project_finds_the_relevant_file(tmp_path):
    assert "src/storage.py" in _project(tmp_path).search_project("append the transcript")


# ---------------------------------------------------------------- tool loop


class _FakeClient:
    """Replays scripted Responses API turns and records what was sent."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.sent = []

    async def post(self, url, headers=None, json=None):
        self.sent.append(json)
        return _FakeResponse(self.turns.pop(0))


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _call(name, arguments, call_id="call_1"):
    return {
        "output": [{"type": "function_call", "name": name, "call_id": call_id, "arguments": json.dumps(arguments)}],
        "usage": {"input_tokens": 900, "output_tokens": 40, "input_tokens_details": {"cached_tokens": 400}},
    }


def _text(answer):
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
        "usage": {"input_tokens": 1_500, "output_tokens": 120, "input_tokens_details": {"cached_tokens": 800}},
    }


async def test_the_loop_executes_tools_and_returns_the_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = _FakeClient([_call("search_project", {"query": "transcript"}), _text("storage.py:1 appends it.")])
    investigator = Investigator(Settings(), tmp_path, client=client)
    (tmp_path / "storage.py").write_text("def append_transcript(): pass\n", encoding="utf-8")

    answer = await investigator.investigate("Where is the transcript appended?", "REMOTE: where do we append?")

    assert answer == "storage.py:1 appends it."
    # The tool result was fed back keyed by call_id, which is what the API matches on.
    second_turn = client.sent[1]["input"]
    result = [item for item in second_turn if item.get("type") == "function_call_output"][0]
    assert result["call_id"] == "call_1"
    assert "append_transcript" in result["output"]


async def test_usage_is_summed_across_every_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = _FakeClient([_call("git_history", {}), _text("done")])
    investigator = Investigator(Settings(), tmp_path, client=client)

    await investigator.investigate("anything", "REMOTE: anything")

    assert investigator.last_usage["turns"] == 2
    assert investigator.last_usage["input_tokens"] == 2_400
    assert investigator.last_usage["cache_read_input_tokens"] == 1_200


async def test_a_loop_that_never_answers_is_bounded(tmp_path, monkeypatch):
    """Without a turn limit a model that keeps calling tools would run for the whole
    meeting."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings.model_validate({"copilot": {"investigation_max_turns": 3}})
    client = _FakeClient([_call("git_history", {}, f"call_{i}") for i in range(3)])
    investigator = Investigator(settings, tmp_path, client=client)

    answer = await investigator.investigate("anything", "REMOTE: anything")

    assert answer == ""
    assert len(client.sent) == 3


async def test_a_malformed_tool_argument_does_not_crash_the_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    broken = _call("search_project", {})
    broken["output"][0]["arguments"] = "{not json"
    client = _FakeClient([broken, _text("answered anyway")])
    investigator = Investigator(Settings(), tmp_path, client=client)

    assert await investigator.investigate("anything", "REMOTE: anything") == "answered anyway"


async def test_an_api_error_carries_the_reason_not_just_the_status(tmp_path, monkeypatch):
    """A bare status code is unactionable: the reasoning-item rejection that broke the
    first live run only became fixable once the API's own message surfaced."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class Failing:
        async def post(self, url, headers=None, json=None):
            return _FakeResponse(
                {"error": {"message": "was provided without its required 'reasoning' item"}}, status_code=400
            )

    investigator = Investigator(Settings(), tmp_path, client=Failing())

    try:
        await investigator.investigate("q", "t")
    except RuntimeError as exc:
        assert "HTTP 400" in str(exc)
        assert "reasoning" in str(exc)
    else:
        raise AssertionError("the failure was swallowed")


async def test_every_output_item_is_replayed_not_only_the_calls(tmp_path, monkeypatch):
    """A reasoning model binds a reasoning item to each function_call and the API rejects
    the call when its partner is missing."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    first = _call("git_history", {})
    first["output"].insert(0, {"type": "reasoning", "id": "rs_1", "summary": []})
    client = _FakeClient([first, _text("done")])
    investigator = Investigator(Settings(), tmp_path, client=client)

    await investigator.investigate("q", "t")

    replayed = client.sent[1]["input"]
    assert any(item.get("type") == "reasoning" for item in replayed)
    assert any(item.get("type") == "function_call" for item in replayed)
