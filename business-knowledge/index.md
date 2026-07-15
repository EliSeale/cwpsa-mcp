---
okf_version: "0.1"
type: Bundle Index
title: ConnectWise PSA Business Knowledge
description: Tenant business concepts, metrics, and rules consumed by the ConnectWise PSA MCP server.
timestamp: 2026-06-22
---
# ConnectWise PSA — Business Knowledge Bundle

Curated, tenant-specific business concepts and rules for this ConnectWise
instance. The MCP server loads this bundle at startup, injects a compact concept
index into its instructions, and exposes each concept through the
`get_business_concept` tool so the agent can fetch the exact recipe on demand.

The agent MUST consult the matching concept here before constructing any
`conditions` / `childconditions` filter for a metric, count, or "how many / list"
question. Do not guess filter values.

## Contents
- [metrics](metrics/index.md) — named business metrics and how to compute them
- [playbooks](playbooks/index.md) — multi-step lookups and procedures
- [entities](entities/index.md) — what core ConnectWise objects mean in business terms