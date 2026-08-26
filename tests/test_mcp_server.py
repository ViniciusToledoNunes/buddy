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
