---
type: Metric
title: Active Users
description: Count and list of active users (contacts) in ConnectWise PSA.
aliases:
  - active users
  - how many users
  - user count
  - number of users
  - headcount
  - list users
  - active user count
entity: contacts
resource: https://connect.verveit.com/v4_6_release/apis/3.0/company/contacts
tags: [users, contacts, headcount]
timestamp: 2026-06-22
recipe:
  notes: >
    "Users" means contacts (see entities/contact), not ConnectWise system
    members. Active = not inactive AND has at least one contact type. Use the
    /count endpoint for "how many"; use the list endpoint when the user wants the
    actual people. types/id!=null and types/name!='Non-User' is a CHILD condition, kept separate from
    conditions.
  count:
    method: GET
    path: /company/contacts/count
    query:
      conditions: inactiveFlag=false
      childconditions: types/id!=null and types/name!='Non-User'
  list:
    method: GET
    path: /company/contacts
    query:
      conditions: inactiveFlag=false
      childconditions: types/id!=null and types/name!='Non-User'
      fields: id,firstName,lastName,company,inactiveFlag
      pageSize: 100
      orderBy: lastName asc
  company_scope:
    when: A specific company is named, e.g. how many users does ACME have.
    step_1:
      tool: resolve_company
      args:
        query: "{company}"
      take: id
    step_2:
      apply: Prepend the company filter to conditions; childconditions is unchanged.
      conditions: company/id={company_id} and inactiveFlag=false
      childconditions: types/id!=null and types/name!='Non-User'
---
# Active Users

## Definition
An **active user** is a [contact](../entities/contact.md) that is:
1. not flagged inactive (`inactiveFlag=false`), and
2. assigned at least one contact type (`types/id!=null`, expressed as a *child* condition).

"User" maps to a contact, not a ConnectWise system member.

## How to answer

**"How many users…"** — call the count endpoint; never page the full list to tally:
```
GET /company/contacts/count?conditions=inactiveFlag=false&childconditions=types/id!=null
```

**"List / who are the users…"** — call the list endpoint with a bounded page:
```
GET /company/contacts?conditions=inactiveFlag=false&childconditions=types/id!=null&fields=id,firstName,lastName,company&pageSize=100&orderBy=lastName asc
```

## Scoping to a company
If the question names a company ("how many users does **ACME** have"):
1. Resolve the company first: `resolve_company("ACME")` → take its `id`.
2. Prepend the company filter to `conditions` (leave `childconditions` as-is):
```
conditions = company/id=<id> and inactiveFlag=false
childconditions = types/id!=null
```
If `resolve_company` returns more than one match, ask which company before counting.

## Gotchas
- `types/id!=null` belongs in **childconditions** (it targets the `types` child
  collection), not in top-level `conditions`. Keep them separate.
- Counting by listing and tallying rows is wrong for large tenants — always use
  `/count` for a number.