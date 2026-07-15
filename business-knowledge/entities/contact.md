---
type: Entity
title: Contact
description: A person associated with a company in ConnectWise PSA. "User" in business questions maps to a contact.
resource: https://connect.verveit.com/v4_6_release/apis/3.0/company/contacts
aliases: [user, users, person, people, employee, employees]
tags: [contacts, users]
timestamp: 2026-06-22
---
# Contact

When someone asks about "users" — "how many users does ACME have", "list the
users" — they almost always mean **contacts** (`/company/contacts`), not
ConnectWise system *members* (`/system/members`, which are CW logins/agents).
Use contacts unless the question is explicitly about CW logins or agents.

## Fields that matter for activity
- `inactiveFlag` (bool) — `false` means the contact is active.
- `types` (collection) — contact type assignments. A real, active user has at
  least one type, expressed as a child filter: `childconditions=types/id!=null`.
- `company` — the company the contact belongs to; filter with `conditions=company/id=<id>`.

## Related
- Metric: [Active Users](../metrics/active_users.md)