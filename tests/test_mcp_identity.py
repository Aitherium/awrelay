"""The MCP server must never post anonymously: no token and no session bearer is an error."""
import pytest  # noqa: I001 -- same split as test_inbox.py

from awrelay import cli, mcp_server


def test_no_identity_is_an_error_not_a_walk_in(monkeypatch, tmp_path):
    monkeypatch.setenv("AWRELAY_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("AWRELAY_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_session_bearer", lambda: "")
    with pytest.raises(RuntimeError, match="no identity"):
        mcp_server._client_from_env()
