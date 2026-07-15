"""
Unit test configuration — set environment variables before any module imports.

All unit tests run with CW_LOCAL_SECRETS=1 so the config module uses env var
fallbacks instead of hitting Azure Key Vault.  The values here are stubs;
they are never used for live ConnectWise calls in unit tests.
"""

import os

# Must be set before cwpsa.config is imported by any test module.
os.environ.setdefault("CW_LOCAL_SECRETS", "1")

# New canonical secret names (CWPSA-* scheme, §12.1)
os.environ.setdefault("CWPSA_COMPANY_ID", "mettle")
os.environ.setdefault("CWPSA_CLIENT_ID", "test-client-id")
os.environ.setdefault("CWPSA_INTEGRATOR_USERNAME", "test_integrator")
os.environ.setdefault("CWPSA_INTEGRATOR_PASSWORD", "test_integrator_pass")

# API member lookup keys (Hop 1 — cw-publickey-01-mcp / cw-privatekey-01-mcp)
os.environ.setdefault("CW_PUBLICKEY_01_MCP", "test-pub-key")
os.environ.setdefault("CW_PRIVATEKEY_01_MCP", "test-priv-key")

# Optional — override the base URL for validation tests
os.environ.setdefault("CW_BASE_URL", "https://connect.verveit.com")

# Disable vocabulary fetch in any server-assembly tests
os.environ.setdefault("CW_LOAD_VOCABULARY", "0")
