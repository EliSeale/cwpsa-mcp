"""
MCP Prompts — parameterized workflow templates (§4.6).

Published common MSP workflows as MCP Prompts so hosts get reliable,
repeatable flows without hard-coding — the front-door complement to
the Tier 2 workflow tools.
"""

from __future__ import annotations

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Register MCP Prompts on the server."""

    @mcp.prompt()
    def triage_company_tickets(company: str) -> str:
        """Triage open tickets for a company — summarize by priority and status."""
        return (
            f"Triage open service tickets for '{company}':\n"
            "1. Use cw_find_company to confirm the company.\n"
            "2. Use cw_list_tickets(company=..., closed=False) to get open tickets.\n"
            "3. Group by priority and status. Highlight any overdue or high-priority items.\n"
            "4. Provide a concise summary suitable for an account manager review."
        )

    @mcp.prompt()
    def log_time_on_ticket(ticket_number: str, description: str) -> str:
        """Log time worked on a ticket — prompt for hours and notes."""
        return (
            f"Log time on ticket #{ticket_number}:\n"
            f"Work description: {description}\n"
            "1. Confirm the ticket exists with cw_get_ticket.\n"
            "2. Ask the user: how many hours were worked and should they be billable?\n"
            "3. Use cw_log_time to create the time entry.\n"
            "4. Confirm the time entry was created."
        )

    @mcp.prompt()
    def open_ticket_for_issue(company: str, issue: str) -> str:
        """Open a new service ticket for a reported issue."""
        return (
            f"Open a service ticket for '{company}' — issue: {issue}\n"
            "1. Use cw_find_company to confirm the company and ID.\n"
            "2. Ask the user: which board, what priority, and who to assign to (if known).\n"
            "3. Use cw_create_ticket with the resolved board/priority.\n"
            "4. Confirm the ticket number and URL back to the user."
        )
