import json

from meeting_agent.config import Settings
from meeting_agent.deep import DeepAnalyst
from meeting_agent.memory import MeetingMemoryIndex


def _project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "storage.py").write_text(
        "def append_transcript(event):\n    return event\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret\n", encoding="utf-8")
    return DeepAnalyst(Settings(), tmp_path)


def test_search_project_reports_ranked_paths(tmp_path):
    analyst = _project(tmp_path)

    result = analyst.search_project("where do we append the transcript")

    assert "src/storage.py" in result
    assert "append_transcript" in result


def test_reading_a_file_outside_the_project_is_refused(tmp_path):
    analyst = _project(tmp_path)

    for attempt in ("../outside.py", "/etc/passwd", "src/../../escape.py"):
        assert "refused" in analyst.read_project_file(attempt).lower()


def test_reading_credentials_is_refused_even_inside_the_project(tmp_path):
    analyst = _project(tmp_path)

    result = analyst.read_project_file(".env")

    assert "refused" in result.lower()
    assert "sk-ant-secret" not in result


def test_reading_a_source_file_is_bounded(tmp_path):
    analyst = _project(tmp_path)
    (tmp_path / "src" / "big.py").write_text("\n".join(f"line {i}" for i in range(1000)), encoding="utf-8")

    result = analyst.read_project_file("src/big.py", max_lines=25)

    assert len(result.splitlines()) <= 30
    assert "truncated" in result.lower()


def test_reading_a_missing_file_says_so(tmp_path):
    assert "not found" in _project(tmp_path).read_project_file("src/nope.py").lower()


def test_related_meetings_tool_uses_structured_memory(tmp_path):
    meetings = tmp_path / "meetings"
    prior = meetings / "2026-08-20_billing"
    prior.mkdir(parents=True)
    (prior / "copilot.json").write_text(
        json.dumps({"memory": "Invoice rollback agreed.", "topics": ["rollback", "invoice"]}),
        encoding="utf-8",
    )
    (prior / "transcript.jsonl").write_text("raw private transcript", encoding="utf-8")
    analyst = DeepAnalyst(Settings(), tmp_path, memory_index=MeetingMemoryIndex(meetings))

    result = analyst.find_related_meetings("invoice rollback")

    assert "2026-08-20_billing" in result
    assert "raw private transcript" not in result


def test_git_history_survives_a_directory_without_git(tmp_path):
    result = _project(tmp_path).git_history()

    assert isinstance(result, str) and result


class _FakeRunner:
    def __init__(self, message):
        self.message = message

    async def until_done(self):
        return self.message


class _FakeMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"type": "text", "text": text})()]


class _FakeClient:
    def __init__(self, text="storage.py:12 already appends every final event."):
        self.calls = []
        self.text = text
        self.beta = type("Beta", (), {"messages": self})()

    def tool_runner(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeRunner(_FakeMessage(self.text))


async def test_analysis_runs_a_bounded_tool_loop_on_the_deep_model(tmp_path):
    client = _FakeClient()
    settings = Settings.model_validate({"copilot": {"deep_max_iterations": 4}})
    analyst = DeepAnalyst(settings, tmp_path, client=client)

    answer = await analyst.analyze("REMOTE: do we already store every event?", "What should I say?")

    assert "storage.py:12" in answer
    call = client.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["max_iterations"] == 4
    tool_names = {tool.name for tool in call["tools"]}
    assert {"search_project", "read_project_file", "git_history"} <= tool_names


async def test_the_system_block_is_marked_for_caching(tmp_path):
    client = _FakeClient()
    analyst = DeepAnalyst(Settings(), tmp_path, client=client)

    await analyst.analyze("REMOTE: anything", "What should I say?")

    system = client.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
