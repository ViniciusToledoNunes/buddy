from mcp import Client
import json

from meeting_agent.context import MeetingRepository
from meeting_agent.mcp_server import create_server


async def test_mcp_surface_and_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("BUDDY_ALLOW_MCP_START", raising=False)
    monkeypatch.delenv("MEETING_COPILOT_ALLOW_MCP_START", raising=False)
    server = create_server(MeetingRepository(tmp_path / "meetings", tmp_path / "runtime"))
    async with Client(server) as client:
        discovered = await client.list_tools()
        names = {tool.name for tool in discovered.tools}
        assert {"meeting_status", "get_live_transcript", "search_meetings", "start_meeting"} <= names
        status = await client.call_tool("meeting_status", {})
        assert status.structured_content == {"active": False, "recording": False}
        start = await client.call_tool("start_meeting", {"confirmed": True})
        assert start.structured_content["accepted"] is False
        assert "disabled" in start.structured_content["reason"]
        resources = await client.list_resources()
        assert "meeting://status" in {str(resource.uri) for resource in resources.resources}


async def test_mcp_finds_related_structured_meeting_memory(tmp_path):
    meetings = tmp_path / "meetings"
    prior = meetings / "2026-08-20_billing"
    prior.mkdir(parents=True)
    (prior / "copilot.json").write_text(
        json.dumps(
            {
                "memory": "Invoice migration needs reconciliation and rollback.",
                "topics": ["invoice migration", "reconciliation", "rollback"],
                "decisions": [],
                "action_items": [],
                "open_questions": [],
            }
        ),
        encoding="utf-8",
    )
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        discovered = await client.list_tools()
        assert "find_related_meetings" in {tool.name for tool in discovered.tools}
        result = await client.call_tool(
            "find_related_meetings", {"query": "production invoice rollback", "limit": 3}
        )
        assert result.structured_content["results"][0]["meeting_id"] == prior.name


async def _meeting(tmp_path):
    meetings = tmp_path / "meetings"
    directory = meetings / "2026-08-25_100000"
    directory.mkdir(parents=True)
    (directory / "metadata.json").write_text(json.dumps({"started_at": "2026-08-25T10:00:00+00:00"}), encoding="utf-8")
    (directory / "summary.md").write_text("# Summary\nRollback review", encoding="utf-8")
    (directory / "copilot.json").write_text(
        json.dumps({"memory": "Rollback review", "topics": ["rollback"], "decisions": ["ship on Friday"]}),
        encoding="utf-8",
    )
    (directory / "transcript.jsonl").write_text(
        json.dumps({"speaker": "ME", "text": "We agreed on the rollback", "final": True, "timestamp": "2026-08-25T10:01:00+00:00"}),
        encoding="utf-8",
    )
    return meetings, directory


async def test_every_read_tool_returns_bounded_context(tmp_path):
    meetings, directory = await _meeting(tmp_path)
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        listed = await client.call_tool("list_meetings", {"limit": 5})
        assert listed.structured_content["result"][0]["meeting_id"] == directory.name

        meeting = await client.call_tool("get_meeting", {"meeting_id": directory.name})
        assert meeting.structured_content["summary"].startswith("# Summary")
        assert meeting.structured_content["transcript"][0]["speaker"] == "ME"

        context = await client.call_tool("get_copilot_context", {"meeting_id": directory.name})
        assert context.structured_content["decisions"] == ["ship on Friday"]

        found = await client.call_tool("search_meetings", {"query": "rollback"})
        assert found.structured_content["results"][0]["meeting_id"] == directory.name

        live = await client.call_tool("get_live_transcript", {})
        assert live.structured_content["active"] is False


async def test_read_tools_fail_closed_on_bad_input(tmp_path):
    meetings, _ = await _meeting(tmp_path)
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        missing = await client.call_tool("get_meeting", {"meeting_id": "does-not-exist"})
        assert missing.structured_content["error"]["code"] == "meeting_not_found"

        traversal = await client.call_tool("get_copilot_context", {"meeting_id": "../secrets"})
        assert traversal.structured_content["error"]["code"] == "meeting_not_found"

        short = await client.call_tool("search_meetings", {"query": "a"})
        assert short.structured_content["error"]["code"] == "invalid_query"


async def test_control_tools_refuse_without_an_active_meeting(tmp_path):
    meetings, _ = await _meeting(tmp_path)
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        assert (await client.call_tool("request_suggestion", {})).structured_content["accepted"] is False
        assert (await client.call_tool("stop_meeting", {})).structured_content["accepted"] is False
        unconfirmed = await client.call_tool("start_meeting", {})
        assert "confirmation" in unconfirmed.structured_content["reason"]


async def test_resources_expose_status_and_summary(tmp_path):
    meetings, directory = await _meeting(tmp_path)
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        status = await client.read_resource("meeting://status")
        assert json.loads(status.contents[0].text)["active"] is False

        recent = await client.read_resource("meeting://recent")
        assert json.loads(recent.contents[0].text)[0]["meeting_id"] == directory.name

        summary = await client.read_resource("meeting://latest/summary")
        assert "Rollback review" in summary.contents[0].text


async def test_related_meetings_ignores_noise_only_queries(tmp_path):
    meetings, _ = await _meeting(tmp_path)
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        empty = await client.call_tool("find_related_meetings", {"query": "the and or"})
        assert empty.structured_content["results"] == []


async def test_prompt_keeps_identity_and_secret_guardrails(tmp_path):
    meetings, _ = await _meeting(tmp_path)
    server = create_server(MeetingRepository(meetings, tmp_path / "runtime"))
    async with Client(server) as client:
        prompt = await client.get_prompt("relate_meeting_to_project", {"project_name": "buddy"})
        text = prompt.messages[0].content.text
        assert "buddy" in text
        assert "secrets" in text
