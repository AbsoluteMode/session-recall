"""Cross-host packaging must not drift: Cursor support is more than reading its
database; the native plugin also wires MCP, skills, commands, agent, and hook."""

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _json(path: str):
    return json.loads((ROOT / path).read_text())


def test_plugin_versions_and_descriptions_cover_all_three_hosts():
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "version"]
    manifests = [
        _json(".claude-plugin/plugin.json"),
        _json(".codex-plugin/plugin.json"),
        _json(".cursor-plugin/plugin.json"),
    ]
    assert {manifest["version"] for manifest in manifests} == {project_version}
    for manifest in manifests:
        description = manifest["description"].casefold()
        assert all(host in description for host in ("claude", "codex", "cursor"))


def test_cursor_plugin_components_and_marketplace_are_resolvable():
    manifest = _json(".cursor-plugin/plugin.json")
    assert manifest["minClientVersions"]["cursor"] == "2.5.0"
    for field in ("commands", "agents", "skills", "hooks", "mcpServers"):
        value = manifest[field]
        assert isinstance(value, str) and value.startswith("./")
        assert (ROOT / value).exists(), f"Cursor manifest {field} path is stale"

    marketplace = _json(".cursor-plugin/marketplace.json")
    assert marketplace["plugins"] == [{
        "name": "session-recall",
        "source": ".",
        "description": (
            "Search and navigate one local semantic index of Claude Code, "
            "Codex, and Cursor history."),
    }]
    assert manifest["name"] == marketplace["plugins"][0]["name"]


def test_cursor_mcp_and_hook_use_native_shapes():
    mcp = _json("mcp.json")
    server = mcp["mcpServers"]["session-recall"]
    assert server["command"] == "/bin/sh"
    assert "session-recall-mcp" in server["args"][-1]

    hooks = _json("hooks/hooks-cursor.json")
    assert hooks["version"] == 1
    entries = hooks["hooks"]["sessionStart"]
    assert len(entries) == 1 and set(entries[0]) == {"command"}
    assert "session-recall" in entries[0]["command"]
    assert " index" in entries[0]["command"]
