"""World Clock MCP Server — returns current time in different timezones."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

mcp = FastMCP("World Clock")


@mcp.tool()
def india_time() -> str:
    """Get the current time in India (Asia/Kolkata, IST)."""
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    return now.strftime("%A, %B %d %Y — %I:%M:%S %p %Z")


@mcp.tool()
def stlouis_time() -> str:
    """Get the current time in St. Louis, Missouri (America/Chicago, CST/CDT)."""
    now = datetime.now(ZoneInfo("America/Chicago"))
    return now.strftime("%A, %B %d %Y — %I:%M:%S %p %Z")


@mcp.tool()
def london_time() -> str:
    """Get the current time in London, UK (Europe/London, GMT/BST)."""
    now = datetime.now(ZoneInfo("Europe/London"))
    return now.strftime("%A, %B %d %Y — %I:%M:%S %p %Z")


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)
