"""cwpsa — ConnectWise PSA MCP server (advanced registry-driven implementation)."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("cwpsa-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
