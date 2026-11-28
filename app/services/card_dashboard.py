"""
Credit card analysis dashboard.

A "month" here is a billing cycle: CreditCardTransactions.statement_month
('YYYY-MM'), so the numbers match the statements, not calendar months.

Amounts follow spending_report(): refunds are negative and reduce their
category; categories with counts_as_spending = False (card payments,
transfers) are left out. Counts, "biggest charges" and the weekday chart
only look at charges (amount > 0).

build_dashboard() is pure (plain inputs, no database) so it can be unit
tested; load_dashboard() gathers those inputs for one user.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

from app.models.models import CreditCardStatement, PurchaseCategory
from app.services import card_reconciliation as recon
from app.services import spend_categories as sc
from app.services.spend_category_seed import UNCATEGORIZED_SLUG

TREND_MONTHS = 12      # bars in the monthly trend
AVERAGE_MONTHS = 6     # window of the "6-month average" column
REWARDS_MONTHS = 12    # window of the points redemption ring
TOP_N = 5
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Charge:
    id: int
    month: str                 # statement month, 'YYYY-MM'
    day: date
    description: str
    amount: float              # negative = refund
    category_slug: str         # top-level category
    category_name: str
    counts_as_spending: bool
    linked: bool = False       # a receipt is linked to it
    ignored: bool = False      # marked as needing no receipt
    expects_receipt: bool = False  # unlinked, but looks like a shop you keep receipts for
    points: int = 0            # reward points booked on this transaction


@dataclass(frozen=True)
class Statement:
    provider: str
    month: str
    points_earned: int = 0
    points_redeemed: int = 0
    points_earned_eur: float = 0.0
    current_points: int | None = None
    current_points_eur: float | None = None


# ------------------------------------------------------------ month helpers

def shift_month(month, delta):
    """'2026-01', -1 -> '2025-12'."""
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_label(month):
    return date(int(month[:4]), int(month[5:7]), 1).strftime("%b %y")


def _pct_change(current, previous):
    """None when there is nothing to compare with."""
    if previous is None or abs(previous) < 0.005:
        return None
    return round(100 * (current - previous) / abs(previous), 1)


def _round(value):
    return round(float(value), 2)


# ------------------------------------------------------------ dashboard

def build_dashboard(charges, statements, month=None):
    """
    charges: [Charge], statements: [Statement], month: 'YYYY-MM' or None
    (newest month). Returns a dict ready for the template / JSON.
    """
    months = sorted({c.month for c in charges} | {s.month for s in statements}, reverse=True)
    if not months:
        return {"months": [], "month": None, "empty": True}
    if month not in months:
        month = months[0]
    prev = shift_month(month, -1)
    has_prev = prev in months

    spending = [c for c in charges if c.counts_as_spending]
    by_month = defaultdict(list)
    for c in spending:
        by_month[c.month].append(c)
    current = by_month[month]

    def total(rows):
        return _round(sum(c.amount for c in rows))

    def category_totals(rows):
        out = defaultdict(float)
        for c in rows:
            out[(c.category_slug, c.category_name)] += c.amount
        return out

    spend_total = total(current)
    prev_total = total(by_month[prev]) if has_prev else None
    purchases = [c for c in current if c.amount > 0]
    prev_purchases = [c for c in by_month[prev] if c.amount > 0] if has_prev else None

    # Spend by category (donut).
    cat_now = category_totals(current)
    by_category = [
        {"slug": slug, "name": name, "total": _round(amount),
         "share": round(100 * amount / spend_total, 1) if spend_total > 0 else None}
        for (slug, name), amount in sorted(cat_now.items(), key=lambda kv: -kv[1]) if amount > 0.005
    ]

    # Points: the statement summary when there is one, else the points booked on the transactions.
    st_by_month = defaultdict(list)
    for s in statements:
        st_by_month[s.month].append(s)
    tx_points = defaultdict(int)
    for c in charges:
        tx_points[c.month] += c.points or 0

    def points_earned(m):
        if any(s.points_earned for s in st_by_month[m]):
            return sum(s.points_earned or 0 for s in st_by_month[m])
        return tx_points[m]

    points_now = points_earned(month)
    points_eur_now = _round(sum(s.points_earned_eur or 0 for s in st_by_month[month])) if st_by_month[month] else None
    points_prev = points_earned(prev) if has_prev else None

    # Receipt coverage.
    receipt_charges = [c for c in purchases if not c.ignored]
    matched = sum(1 for c in receipt_charges if c.linked)
    expected = matched + sum(1 for c in receipt_charges if not c.linked and c.expects_receipt)

    kpis = {
        "spend": {"value": spend_total, "previous": prev_total, "change": _pct_change(spend_total, prev_total)},
        "transactions": {
            "value": len(purchases),
            "previous": len(prev_purchases) if prev_purchases is not None else None,
            "change": _pct_change(len(purchases), len(prev_purchases)) if prev_purchases is not None else None,
            "average": _round(sum(c.amount for c in purchases) / len(purchases)) if purchases else None,
        },
        "top_category": by_category[0] if by_category else None,
        "points": {"value": points_now, "eur": points_eur_now, "previous": points_prev,
                   "change": _pct_change(points_now, points_prev)},
        "receipts": {"matched": matched, "expected": expected,
                     "pct": round(100 * matched / expected) if expected else None},
    }

    # Monthly trend: calendar-consecutive billing months, starting no earlier than the data.
    oldest = months[-1]
    trend = []
    for i in range(TREND_MONTHS - 1, -1, -1):
        m = shift_month(month, -i)
        if m >= oldest:
            trend.append({"month": m, "label": month_label(m), "total": total(by_month[m])})

    # Top merchants (grouped by the merchant keyword, labelled by its most common description).
    merchants = defaultdict(lambda: {"total": 0.0, "count": 0, "names": Counter()})
    for c in current:
        entry = merchants[sc.suggest_keyword(c.description) or c.description]
        entry["total"] += c.amount
        entry["count"] += 1 if c.amount > 0 else 0
        entry["names"][c.description.strip()] += 1
    top_merchants = [
        {"name": e["names"].most_common(1)[0][0], "total": _round(e["total"]), "count": e["count"]}
        for e in sorted(merchants.values(), key=lambda e: -e["total"]) if e["total"] > 0.005
    ][:TOP_N]

    # Category table: this month vs last month vs the average of earlier months with data.
    window = [m for m in months if m < month][:AVERAGE_MONTHS]
    cat_prev = category_totals(by_month[prev]) if has_prev else {}
    cat_window = [category_totals(by_month[m]) for m in window]
    counts_now = Counter((c.category_slug, c.category_name) for c in purchases)
    keys = set(cat_now) | set(cat_prev) | {k for totals in cat_window for k in totals}
    category_table = []
    for key in keys:
        this, last = cat_now.get(key, 0.0), cat_prev.get(key, 0.0)
        avg = sum(t.get(key, 0.0) for t in cat_window) / len(cat_window) if cat_window else None
        category_table.append({
            "slug": key[0], "name": key[1],
            "this_month": _round(this),
            "last_month": _round(last) if has_prev else None,
            "change": _pct_change(this, last) if has_prev else None,
            "average": _round(avg) if avg is not None else None,
            "count": counts_now.get(key, 0),
        })
    category_table.sort(key=lambda r: (-r["this_month"], -(r["average"] or 0), r["name"]))

    # Rewards ring: balance from each provider's latest statement up to this month,
    # redemption rate over the last REWARDS_MONTHS.
    latest = {}
    for s in sorted(statements, key=lambda s: s.month):
        if s.month <= month and s.current_points is not None:
            latest[s.provider] = s
    since = shift_month(month, -(REWARDS_MONTHS - 1))
    window_months = [m for m in months if since <= m <= month]
    recent = [s for s in statements if since <= s.month <= month]
    earned = sum(points_earned(m) for m in window_months)
    redeemed = sum(s.points_redeemed or 0 for s in recent) if recent else None
    rewards = {
        "balance": sum(s.current_points for s in latest.values()) if latest else None,
        "balance_eur": _round(sum(s.current_points_eur or 0 for s in latest.values())) if latest else None,
        "earned": earned,
        "redeemed": redeemed,
        "redemption_pct": min(100, round(100 * redeemed / earned)) if earned and redeemed is not None else None,
        "months": REWARDS_MONTHS,
    }

    biggest = [
        {"date": c.day.isoformat(), "description": c.description, "amount": _round(c.amount), "category": c.category_name}
        for c in sorted(purchases, key=lambda c: (-c.amount, c.day))[:TOP_N]
    ]

    weekday_totals = [0.0] * 7
    weekday_counts = [0] * 7
    for c in purchases:
        weekday_totals[c.day.weekday()] += c.amount
        weekday_counts[c.day.weekday()] += 1
    weekdays = [{"day": d, "total": _round(t), "count": n} for d, t, n in zip(WEEKDAYS, weekday_totals, weekday_counts)]

    uncategorized = [c for c in charges if c.month == month and c.category_slug == UNCATEGORIZED_SLUG]

    return {
        "empty": False,
        "months": months,
        "month": month,
        "month_label": month_label(month),
        "previous_month": prev if has_prev else None,
        "kpis": kpis,
        "by_category": by_category,
        "trend": trend,
        "top_merchants": top_merchants,
        "category_table": category_table,
        "rewards": rewards,
        "biggest": biggest,
        "weekdays": weekdays,
        "uncategorized": {"count": len(uncategorized), "total": _round(sum(c.amount for c in uncategorized))},
    }


# ------------------------------------------------------------ loading

def load_charges(db_session, userid):
    """Every card transaction of the user as a Charge with its top-level category."""
    userid = int(userid)
    cats = sc.load_categories(db_session)
    uncategorized = next(c for c in cats.values() if c.slug == UNCATEGORIZED_SLUG)
    assigned = {
        row.purchase_ref: row.category_id
        for row in db_session.query(PurchaseCategory).filter(
            PurchaseCategory.userid == userid, PurchaseCategory.purchase_type == sc.CARD
        )
    }
    links = recon.load_matches(db_session, userid)
    known_shops = {b["shopName"] for b in recon.load_bills(db_session, userid) if b["shopName"]}

    charges = []
    for tx in recon.load_transactions(db_session, userid):
        cat = cats.get(assigned.get(str(tx.id))) or uncategorized
        top = cats[cat.parent_id] if cat.parent_id else cat
        rows = links.get(tx.id, [])
        linked = any(m.match_type in recon.BILL_LINK_TYPES and m.billId for m in rows)
        ignored = any(m.match_type == "ignored" for m in rows)
        expects = (not linked and not ignored and tx.amount > 0
                   and any(recon.description_matches_shop(tx.description, s) for s in known_shops))
        charges.append(Charge(
            id=tx.id, month=tx.statement_month, day=recon._as_date(tx.transaction_date),
            description=tx.description or "", amount=float(tx.amount),
            category_slug=top.slug, category_name=top.name,
            counts_as_spending=bool(cat.counts_as_spending and top.counts_as_spending),
            linked=linked, ignored=ignored, expects_receipt=expects, points=tx.points or 0,
        ))
    return charges


def load_statements(db_session, userid):
    rows = db_session.query(CreditCardStatement).filter(CreditCardStatement.userid == int(userid)).all()
    return [
        Statement(
            provider=r.provider, month=r.statement_month,
            points_earned=r.points_earned or 0, points_redeemed=r.points_redeemed or 0,
            points_earned_eur=r.points_earned_eur or 0.0,
            current_points=r.current_points, current_points_eur=r.current_points_eur,
        )
        for r in rows
    ]


def load_dashboard(db_session, userid, month=None):
    return build_dashboard(load_charges(db_session, userid), load_statements(db_session, userid), month)
