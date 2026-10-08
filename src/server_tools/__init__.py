"""
========================================
server_tools/ — the faces of the seven MCP tools
========================================

One file per tool: breath, grow, recall, fold, muse, regrow, trace. Each holds what a
client sees — the signature with its parameter descriptions, the tool description, and
the one call that forwards to the implementation under tools/<tool>/ through
server_call._with_notice — plus adapt(mcp), the strict-argument adapter server.py runs
right after mounting it.

server.py mounts every face (mcp.tool()) in its mount table; nothing here registers itself.

Exports: none (package marker only)
========================================
"""
