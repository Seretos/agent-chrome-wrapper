from mcp.server.fastmcp import FastMCP

mcp = FastMCP("chrome-wrapper")


@mcp.tool()
def ping() -> str:
    """Health check tool. Replace with real tools as you build them out."""
    return "pong"


def main() -> None:
    mcp.run()
