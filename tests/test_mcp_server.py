from mcp import Client

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
