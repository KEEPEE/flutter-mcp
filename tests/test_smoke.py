from flutter_docs_mcp import __version__


def test_version():
    assert __version__ == "0.3.0"


def test_server_imports_and_has_health_tool():
    from flutter_docs_mcp.server import mcp
    tools = mcp._tool_manager.list_tools()
    names = [t.name for t in tools]
    assert "health_check" in names


def test_server_exposes_all_six_tools():
    from flutter_docs_mcp.server import mcp
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert names == {
        "flutter_docs",
        "flutter_search",
        "flutter_mentions",
        "pub_package",
        "flutter_status",
        "health_check",
    }
