---
type: Playbook
title: Account Manager
description: Find the account manager (a.k.a. TAM) for a company.
aliases:
  - account manager
  - technical account manager
  - tam
  - who is the account manager
  - whos our account manager
  - account manager for
  - am
entity: company teams
resource: https://connect.verveit.com/v4_6_release/apis/3.0/company/companies
tags: [account-manager, tam, company-team]
timestamp: 2026-06-22
recipe:
  notes: >
    "Account manager", "TAM", and "technical account manager" all map to the
    company team member flagged accountManagerFlag=true. Resolve the company,
    read its team, return that member. Prefer the company's _info.teams_href when
    your resolver surfaces it; otherwise use the id-based endpoint. The returned
    person is a ConnectWise system member, not a contact.
  step_1:
    tool: resolve_company
    args:
      query: "{company}"
    take: id
  step_2:
    description: Fetch the company team and keep the account-manager row(s).
    href: "{teams_href}"
    path: /company/companies/{company_id}/teams
    method: GET
    query:
      conditions: accountManagerFlag=true
      fields: id,member,teamRole,accountManagerFlag
      pageSize: 50
  step_3:
    return: member
    description: >
      Return the `member` of the matching row. If more than one row has
      accountManagerFlag=true, list them all. If none, say the company has no
      account manager assigned.
---
# Account Manager

## Definition
The **account manager** for a company is the entry on that company's team with
`accountManagerFlag=true`. "TAM" and "technical account manager" are treated as
the same role here. The result is a ConnectWise **system member** (a CW login),
not a [contact](../entities/contact.md).

## How to answer
1. Resolve the company: `resolve_company("ACME")` → take its `id` (and its
   `_info.teams_href` if available).
2. Read the company team and keep the account-manager row. Preferred, if you have
   the href:
   ```
   GET <company._info.teams_href>?conditions=accountManagerFlag=true&fields=id,member,teamRole,accountManagerFlag
   ```
   Reliable fallback using the resolved id:
   ```
   GET /company/companies/<id>/teams?conditions=accountManagerFlag=true&fields=id,member,teamRole,accountManagerFlag
   ```
3. Return the `member` of the matching row. If `resolve_company` returns more than
   one company, confirm which before looking up the team.

## Gotchas
- **teams_href may not be present.** `resolve_company` currently requests only
  `fields=id,identifier,name`, so the company's `_info` block (and `teams_href`)
  isn't returned. Either add `_info` to that resolver's fields, or just use the
  id-based path `/company/companies/<id>/teams` — it always works from the id you
  already have.
- If the teams endpoint rejects the `conditions` filter, fetch the team without
  it and select the row(s) where `accountManagerFlag == true` client-side.
- ConnectWise also has a separate `techContactFlag` (technical contact). This
  concept maps TAM to `accountManagerFlag` per the agreed definition; split it out
  later if you need to distinguish the two roles.
- Zero matches → no account manager assigned; multiple → list them, don't guess.

## Related
- Entity: [Contact](../entities/contact.md) — clarifies member vs contact.