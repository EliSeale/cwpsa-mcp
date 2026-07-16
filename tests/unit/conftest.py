"""
Unit test configuration — set environment variables before any module imports.

All unit tests run with CW_LOCAL_SECRETS=1 so the config module uses env var
fallbacks instead of hitting Azure Key Vault.  The values here are stubs;
they are never used for live ConnectWise calls in unit tests.
"""

import os

# Must be set before cwpsa.config is imported by any test module.
os.environ.setdefault("CW_LOCAL_SECRETS", "1")

# Secret names as stored in Key Vault → env var fallback mapping:
# get_secret(name) → os.getenv(name.upper().replace("-", "_"))

# cw-companyId-01-mcp  → CW_COMPANYID_01_MCP
os.environ.setdefault("CW_COMPANYID_01_MCP", "mettle")
# cw-clientid-01-mcp   → CW_CLIENTID_01_MCP
os.environ.setdefault("CW_CLIENTID_01_MCP", "test-client-id")
# cw-publickey-01-mcp  → CW_PUBLICKEY_01_MCP
os.environ.setdefault("CW_PUBLICKEY_01_MCP", "test-pub-key")
# cw-privatekey-01-mcp → CW_PRIVATEKEY_01_MCP
os.environ.setdefault("CW_PRIVATEKEY_01_MCP", "test-priv-key")
# cw-integratorusername-01-mcp → CW_INTEGRATORUSERNAME_01_MCP
os.environ.setdefault("CW_INTEGRATORUSERNAME_01_MCP", "test_integrator")
# cw-integratorpassword-01-mcp → CW_INTEGRATORPASSWORD_01_MCP
os.environ.setdefault("CW_INTEGRATORPASSWORD_01_MCP", "test_integrator_pass")

# Optional — override the base URL for validation tests
os.environ.setdefault("CW_BASE_URL", "https://connect.verveit.com")

# Disable vocabulary fetch in any server-assembly tests
os.environ.setdefault("CW_LOAD_VOCABULARY", "0")
