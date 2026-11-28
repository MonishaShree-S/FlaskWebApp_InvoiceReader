"""
Sidebar navigation for pages built on templates/base.html.

The menu is data: add a page by adding an Item. An Item with endpoint=None
is a planned feature and is shown disabled with a "soon" badge.
"""
from dataclasses import dataclass, field

from flask import request


@dataclass(frozen=True)
class Item:
    label: str
    icon: str                  # Bootstrap Icons name, without the "bi-" prefix
    endpoint: str | None = None

    @property
    def soon(self):
        return self.endpoint is None


@dataclass(frozen=True)
class Section:
    title: str
    icon: str
    items: tuple = field(default_factory=tuple)


NAVIGATION = (
    Section("Home", "house", (
        Item("Upload & invoices", "house-door", "main.index"),
    )),
    Section("Credit card", "credit-card", (
        Item("Analysis dashboard", "speedometer2", "credit_card.dashboard"),
        Item("Forecast expense", "graph-up-arrow"),
        Item("Planner", "calendar-check"),
        Item("Statements", "file-earmark-text", "main.credit_card_statements"),
        Item("Reconciliation", "check2-square", "main.card_reconciliation_page"),
    )),
    Section("Groceries", "basket", (
        Item("Analysis dashboard", "bar-chart", "main.analytics_page"),
        Item("Pantry", "box-seam"),
        Item("Recipes & meal prep", "journal-bookmark"),
        Item("Meal planner", "calendar-week"),
    )),
    Section("Money", "wallet2", (
        Item("Spending by category", "tags", "spending.spending_page"),
        Item("Budgeting", "piggy-bank", "budgeting.budgeting_dashboard"),
        Item("Balances", "people", "main.view_balances"),
    )),
)


def inject_navigation():
    """Context processor: the menu and the current endpoint for highlighting."""
    return {"navigation": NAVIGATION, "active_endpoint": request.endpoint}
