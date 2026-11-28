"""
Steps that run after any bill or card statement is imported. Each step is
idempotent and never raises, so a failure here never fails the import.

Order matters: categorization uses card<->bill links as a fallback, so
matching runs first.
"""
from app.services.card_reconciliation import safe_auto_match
from app.services.spend_categories import safe_categorize


def run_post_import(userid):
    safe_auto_match(userid)
    safe_categorize(userid)
