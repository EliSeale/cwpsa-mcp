"""
Pagination helpers for ConnectWise API responses (§8).

Two modes:
  Navigable:    Standard page/pageSize with RFC 5988 Link header (next/prev/first/last).
  Forward-only: pageId keyset pagination (2018.5+).  Faster for bulk sweeps.
                Ignores `page`, forbids `orderBy`.  Returns `pagination-type: forward-only`
                and a `pageId` in the response Link header.

Usage:
    from cwpsa.integration.pagination import parse_next_page_id, PageResult

    result = await cw_get("/service/tickets", pageSize=25)
    page = PageResult(data=result, link_header=resp.headers.get("Link"))
    if page.next_page_id:
        # forward-only — use pageId for next request
        next_result = await cw_get("/service/tickets", pageId=page.next_page_id, pageSize=25)
    elif page.next_page:
        # navigable — use page number for next request
        next_result = await cw_get("/service/tickets", page=page.next_page, pageSize=25)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# RFC 5988 Link header parser — matches <url>; rel="next" etc.
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
_PAGE_RE = re.compile(r'[?&]page=(\d+)')
_PAGE_ID_RE = re.compile(r'[?&]pageId=([^&]+)')


@dataclass
class PageResult:
    """Parsed pagination state from a ConnectWise list response."""

    data: list[dict]
    link_header: str | None = None

    @property
    def links(self) -> dict[str, str]:
        if not self.link_header:
            return {}
        return {rel: url for url, rel in _LINK_RE.findall(self.link_header)}

    @property
    def next_url(self) -> str | None:
        return self.links.get("next")

    @property
    def next_page(self) -> int | None:
        """For navigable pagination — page number from the next Link."""
        url = self.next_url
        if not url:
            return None
        m = _PAGE_RE.search(url)
        return int(m.group(1)) if m else None

    @property
    def next_page_id(self) -> str | None:
        """For forward-only pagination — pageId from the next Link."""
        url = self.next_url
        if not url:
            return None
        m = _PAGE_ID_RE.search(url)
        return m.group(1) if m else None

    @property
    def has_more(self) -> bool:
        return self.next_url is not None

    @property
    def total_count(self) -> int | None:
        """ConnectWise does not return total count in the response body.
        Use cw_count() before a query to get the total count."""
        return None
