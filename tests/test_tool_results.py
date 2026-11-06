"""A tool's reply reaches the client as its text and nothing else.

Every tool returns JSON as a `str`. Left to infer it, `mcp` also declares an
output schema and sends the same reply again as `structuredContent`, wrapped
in `{"result": ...}` and escaped. Some clients show the model that copy in
place of the text, longer than it by the wrapper and by one character for
every quotation mark and backslash. Driven through `mcp.list_tools` and
`mcp.call_tool`, the layer that builds both: the schema test covers every
registered tool, one added later included, and the reply test calls one.
"""

import asyncio
import json

import pytest

import google_health_mcp.cli  # noqa: F401 - registers every tool module
from google_health_mcp import config, db, helpers
from google_health_mcp.mcp_instance import mcp


class _Exists:
    def exists(self):
        return True


def test_no_tool_declares_an_output_schema():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    # Otherwise an empty registry passes.
    assert {"health_sync", "health_trends", "health_get_lifetime_stats"} <= names
    declaring = sorted(t.name for t in tools if t.output_schema is not None)
    assert not declaring, f"these tools declare an output schema: {declaring}"


@pytest.mark.parametrize("offline", [False, True], ids=["online", "offline"])
def test_a_tool_reply_is_its_text_alone(monkeypatch, offline):
    """Also through `require_auth`'s re-serialising, which adds both notes."""
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    # Otherwise an empty registry fails only on the call below, for another reason.
    assert "health_get_lifetime_stats" in names
    monkeypatch.setattr(config, "OFFLINE_MODE", offline)
    monkeypatch.setattr(helpers, "GOOGLE_CLIENT_PATH", _Exists())
    monkeypatch.setattr(helpers, "GOOGLE_TOKENS_PATH", _Exists())
    conn = db.get_db()
    try:
        db.record_missing_scopes(conn, ["sleep"])
    finally:
        conn.close()

    result = asyncio.run(mcp.call_tool("health_get_lifetime_stats", {}))

    assert result.structured_content is None
    assert [c.type for c in result.content] == ["text"]
    reply = json.loads(result.content[0].text)
    # The text is the reply as format_response writes it, nothing added.
    assert result.content[0].text == json.dumps(reply)
    assert "totals" in reply
    assert "authorisation" in reply
    assert reply.get("offline_mode") is (True if offline else None)
