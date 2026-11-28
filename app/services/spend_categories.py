"""
Purchase (spend) categorization: assigns every card transaction and grocery
bill a SpendCategory, using merchant keyword rules.

Precedence for one purchase:
  1. manual    -- the user picked the category for this purchase
  2. rule      -- best keyword rule on the card description / bill shop name
                  (user rules before global rules, then the longest keyword)
  3. linked    -- no rule matched, but the linked bill / card charge
                  (CardBillMatches) has a category
  4. uncategorized

categorize_user() is idempotent and writes only rows that changed, so it can
run after every import, after rule changes, and on page loads.

What counts as "spending" for a month (spending_report): every card
transaction (refunds are negative and reduce their category), plus bills not
linked to a card transaction (paid some other way). Linked bills are not
counted again. Categories with counts_as_spending = False (transfers) are
listed but excluded from the total.
"""
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import or_, text

from app.models.models import CategoryRule, CreditCardTransaction, PurchaseCategory, SpendCategory
from app.services import card_reconciliation as recon
from app.services.spend_category_seed import CATEGORIES, GLOBAL_RULES, UNCATEGORIZED_SLUG

logger = logging.getLogger(__name__)

CARD = "card_transaction"
BILL = "bill"
PURCHASE_TYPES = (CARD, BILL)

SOURCE_MANUAL = "manual"
SOURCE_RULE = "rule"
SOURCE_LINKED = "linked"
SOURCE_DEFAULT = "default"  # nothing matched -> uncategorized

MAX_KEYWORD_LENGTH = 100
_SHORT_KEYWORD = 3  # keywords this short must match a whole word


# ------------------------------------------------------------ pure logic

def normalize(value):
    """Lowercase, German umlauts spelled out, other accents stripped, whitespace collapsed."""
    value = (value or "").lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(src, dst)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class Rule:
    id: int
    keyword: str          # normalized
    category_id: int
    is_user_rule: bool


class RuleMatcher:
    """Finds the best rule for a merchant text. Build once per batch."""

    def __init__(self, rules):
        compiled = []
        for rule in rules:
            if not rule.keyword:
                continue
            pattern = r"(?<![a-z0-9])" + re.escape(rule.keyword)
            if len(rule.keyword) <= _SHORT_KEYWORD:
                pattern += r"(?![a-z0-9])"
            compiled.append((rule, re.compile(pattern)))
        # User rules first, then longer (more specific) keywords.
        compiled.sort(key=lambda rc: (not rc[0].is_user_rule, -len(rc[0].keyword), rc[0].id))
        self._compiled = compiled

    def match(self, merchant_text):
        normalized = normalize(merchant_text)
        if not normalized:
            return None
        for rule, pattern in self._compiled:
            if pattern.search(normalized):
                return rule
        return None


_NOISE_WORDS = {"sumup", "paypal", "www", "sp", "ls", "if", "hfb", "pay", "gmbh", "fil", "the"}


def suggest_keyword(merchant_text):
    """
    Keyword proposed when the user applies a category to "all purchases from
    this merchant": the first meaningful word of the description, e.g.
    "SumUp Nepal Momo" -> "nepal", "Kik Fil 8192" -> "kik".
    """
    for word in re.findall(r"[a-z][a-z&.\-]*", normalize(merchant_text)):
        word = word.strip(".-")
        if len(word) >= 2 and word not in _NOISE_WORDS:
            return word
    return normalize(merchant_text)[:MAX_KEYWORD_LENGTH]


# ---------------------------------------------------------------- schema

def ensure_schema(engine):
    """Creates the tables if missing and inserts missing seed categories / global rules."""
    from sqlalchemy.orm import Session
    for model in (SpendCategory, CategoryRule, PurchaseCategory):
        model.__table__.create(engine, checkfirst=True)

    with Session(engine) as s:
        by_slug = {c.slug: c for c in s.query(SpendCategory).all()}
        for order, (slug, name, parent_slug, counts) in enumerate(CATEGORIES):
            if slug not in by_slug:
                cat = SpendCategory(slug=slug, name=name, counts_as_spending=counts, sort_order=order)
                s.add(cat)
                by_slug[slug] = cat
        s.flush()
        for slug, _, parent_slug, _ in CATEGORIES:
            if parent_slug and by_slug[slug].parent_id is None:
                by_slug[slug].parent_id = by_slug[parent_slug].id

        existing = {r.keyword for r in s.query(CategoryRule).filter(CategoryRule.userid.is_(None))}
        added = 0
        for keyword, slug in GLOBAL_RULES.items():
            keyword = normalize(keyword)
            if keyword not in existing:
                s.add(CategoryRule(userid=None, keyword=keyword, category_id=by_slug[slug].id))
                existing.add(keyword)
                added += 1
        s.commit()
        if added:
            logger.info("Seeded %d global category rules", added)


# ------------------------------------------------------------ loading

def load_categories(db_session):
    """id -> SpendCategory, ordered for display."""
    return {c.id: c for c in db_session.query(SpendCategory).order_by(SpendCategory.sort_order).all()}


def category_tree(db_session):
    """[{id, slug, name, children: [...]}] for pickers and the API."""
    cats = load_categories(db_session)
    tops = [c for c in cats.values() if c.parent_id is None]
    children = defaultdict(list)
    for c in cats.values():
        if c.parent_id is not None:
            children[c.parent_id].append(c)

    def node(c):
        return {"id": c.id, "slug": c.slug, "name": c.name, "counts_as_spending": c.counts_as_spending}

    return [dict(node(t), children=[node(ch) for ch in children[t.id]]) for t in tops]


def load_rules(db_session, userid):
    rows = db_session.query(CategoryRule).filter(
        or_(CategoryRule.userid.is_(None), CategoryRule.userid == int(userid))
    ).all()
    return [Rule(r.id, normalize(r.keyword), r.category_id, r.userid is not None) for r in rows]


def _category_by_slug(db_session, slug):
    cat = db_session.query(SpendCategory).filter_by(slug=slug).first()
    if cat is None:
        raise ValueError(f"Unknown category: {slug}")
    return cat


# -------------------------------------------------------- categorization

def categorize_user(db_session, userid):
    """
    (Re)computes the category of every purchase of this user except manual
    ones. Commits. Returns the number of rows inserted or changed.
    """
    userid = int(userid)
    matcher = RuleMatcher(load_rules(db_session, userid))
    uncategorized_id = _category_by_slug(db_session, UNCATEGORIZED_SLUG).id

    transactions = recon.load_transactions(db_session, userid)
    bills = recon.load_bills(db_session, userid)
    links = recon.load_matches(db_session, userid)
    existing = {
        (row.purchase_type, row.purchase_ref): row
        for row in db_session.query(PurchaseCategory).filter(PurchaseCategory.userid == userid)
    }

    # Pass 1: manual or rule.
    assigned = {}  # (type, ref) -> (category_id, source, rule_id)
    texts = [((CARD, str(tx.id)), tx.description) for tx in transactions]
    texts += [((BILL, b["billId"]), b["shopName"]) for b in bills]
    for key, merchant in texts:
        row = existing.get(key)
        if row is not None and row.source == SOURCE_MANUAL:
            assigned[key] = (row.category_id, SOURCE_MANUAL, None)
            continue
        rule = matcher.match(merchant)
        if rule is not None:
            assigned[key] = (rule.category_id, SOURCE_RULE, rule.id)

    # Pass 2: fill gaps from the linked purchase.
    bill_to_tx = {}
    tx_to_bills = defaultdict(list)
    for tx_id, rows in links.items():
        for m in rows:
            if m.match_type in recon.BILL_LINK_TYPES and m.billId:
                bill_to_tx[m.billId] = str(tx_id)
                tx_to_bills[str(tx_id)].append(m.billId)
    for key, _ in texts:
        if key in assigned:
            continue
        ptype, ref = key
        if ptype == CARD:
            linked = [assigned.get((BILL, b)) for b in tx_to_bills.get(ref, [])]
        else:
            linked = [assigned.get((CARD, bill_to_tx[ref]))] if ref in bill_to_tx else []
        linked = [a for a in linked if a is not None]
        assigned[key] = (linked[0][0], SOURCE_LINKED, None) if linked else (uncategorized_id, SOURCE_DEFAULT, None)

    # Write the differences.
    changed = 0
    for (ptype, ref), (category_id, source, rule_id) in assigned.items():
        row = existing.get((ptype, ref))
        if row is None:
            db_session.add(PurchaseCategory(
                userid=userid, purchase_type=ptype, purchase_ref=ref,
                category_id=category_id, source=source, rule_id=rule_id,
            ))
            changed += 1
        elif (row.category_id, row.source, row.rule_id) != (category_id, source, rule_id):
            row.category_id, row.source, row.rule_id = category_id, source, rule_id
            changed += 1

    if changed:
        db_session.commit()
        logger.info("Categorized %d purchases for user %s", changed, userid)
    return changed


def safe_categorize(userid):
    """categorize_user() in its own session; never raises (for import hooks)."""
    from app.models.models import get_session
    try:
        with get_session() as db_session:
            return categorize_user(db_session, userid)
    except Exception:
        logger.exception("Categorization failed for user %s", userid)
        return 0


# ------------------------------------------------------- user actions

def _purchase_text(db_session, userid, purchase_type, ref):
    """Merchant text of a purchase owned by userid; raises ValueError if not found."""
    if purchase_type == CARD:
        tx = db_session.get(CreditCardTransaction, int(ref)) if str(ref).isdigit() else None
        if tx is None or tx.userid != int(userid):
            raise ValueError("Card transaction not found.")
        return tx.description
    if purchase_type == BILL:
        shop = db_session.execute(
            text("SELECT TOP 1 shopName FROM groceries WHERE userid = :userid AND billId = :bill_id"),
            {"userid": int(userid), "bill_id": ref},
        ).first()
        if shop is None:
            raise ValueError("Bill not found.")
        return shop.shopName
    raise ValueError(f"Unknown purchase type: {purchase_type}")


def _linked_refs(db_session, userid, purchase_type, ref):
    """The other side(s) of the same purchase, so a manual change applies to both."""
    links = recon.load_matches(db_session, userid)
    if purchase_type == CARD:
        rows = links.get(int(ref), [])
        return [(BILL, m.billId) for m in rows if m.match_type in recon.BILL_LINK_TYPES and m.billId]
    for tx_id, rows in links.items():
        bill_ids = [m.billId for m in rows if m.match_type in recon.BILL_LINK_TYPES and m.billId]
        if ref in bill_ids and len(bill_ids) == 1:
            return [(CARD, str(tx_id))]
    return []


def set_purchase_category(db_session, userid, purchase_type, ref, slug):
    """Manually categorizes one purchase (and its linked counterpart). Commits."""
    userid = int(userid)
    _purchase_text(db_session, userid, purchase_type, ref)
    category = _category_by_slug(db_session, slug)
    for ptype, pref in [(purchase_type, str(ref))] + _linked_refs(db_session, userid, purchase_type, ref):
        row = db_session.query(PurchaseCategory).filter_by(
            userid=userid, purchase_type=ptype, purchase_ref=pref
        ).first()
        if row is None:
            row = PurchaseCategory(userid=userid, purchase_type=ptype, purchase_ref=pref)
            db_session.add(row)
        row.category_id, row.source, row.rule_id = category.id, SOURCE_MANUAL, None
    db_session.commit()


def reset_purchase_category(db_session, userid, purchase_type, ref):
    """Drops a manual category so rules apply again (to the purchase and its linked side)."""
    userid = int(userid)
    refs = [(purchase_type, str(ref))] + _linked_refs(db_session, userid, purchase_type, ref)
    for ptype, pref in refs:
        db_session.query(PurchaseCategory).filter_by(
            userid=userid, purchase_type=ptype, purchase_ref=pref, source=SOURCE_MANUAL
        ).delete()
    db_session.commit()
    categorize_user(db_session, userid)


def list_user_rules(db_session, userid):
    cats = load_categories(db_session)
    rows = db_session.query(CategoryRule).filter(CategoryRule.userid == int(userid)).order_by(CategoryRule.keyword).all()
    return [{"id": r.id, "keyword": r.keyword, "category_slug": cats[r.category_id].slug,
             "category_name": cats[r.category_id].name} for r in rows]


def save_user_rule(db_session, userid, keyword, slug):
    """Creates or updates the user's rule for keyword, then re-categorizes. Returns the rule id."""
    keyword = normalize(keyword)
    if len(keyword) < 2 or len(keyword) > MAX_KEYWORD_LENGTH:
        raise ValueError(f"Keyword must be 2-{MAX_KEYWORD_LENGTH} characters.")
    category = _category_by_slug(db_session, slug)
    rule = db_session.query(CategoryRule).filter_by(userid=int(userid), keyword=keyword).first()
    if rule is None:
        rule = CategoryRule(userid=int(userid), keyword=keyword, category_id=category.id)
        db_session.add(rule)
    else:
        rule.category_id = category.id
    db_session.commit()
    categorize_user(db_session, userid)
    return rule.id


def delete_user_rule(db_session, userid, rule_id):
    rule = db_session.get(CategoryRule, int(rule_id))
    if rule is None or rule.userid != int(userid):
        raise ValueError("Rule not found.")
    db_session.query(PurchaseCategory).filter_by(rule_id=rule.id).update({"rule_id": None})
    db_session.delete(rule)
    db_session.commit()
    categorize_user(db_session, userid)


# --------------------------------------------------------------- report

def _month_bounds(month):
    """'YYYY-MM' -> (first day, first day of next month)."""
    start = datetime.strptime(month, "%Y-%m").date()
    end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def spending_report(db_session, userid, month):
    """
    Purchases of one month with their categories, plus totals per top-level
    category (with per-subcategory breakdown). month: 'YYYY-MM'.
    """
    userid = int(userid)
    start, end = _month_bounds(month)
    cats = load_categories(db_session)
    assigned = {
        (row.purchase_type, row.purchase_ref): row
        for row in db_session.query(PurchaseCategory).filter(PurchaseCategory.userid == userid)
    }
    links = recon.load_matches(db_session, userid)
    linked_bill_ids = {
        m.billId for rows in links.values() for m in rows
        if m.match_type in recon.BILL_LINK_TYPES and m.billId
    }
    uncategorized = next(c for c in cats.values() if c.slug == UNCATEGORIZED_SLUG)

    purchases = []

    def add(ptype, ref, day, merchant, amount, linked_to):
        row = assigned.get((ptype, ref))
        cat = cats.get(row.category_id) if row else uncategorized
        top = cats[cat.parent_id] if cat.parent_id else cat
        purchases.append({
            "type": ptype, "ref": ref, "date": day, "merchant": merchant,
            "amount": round(float(amount), 2), "linked_to": linked_to,
            "category_slug": cat.slug, "category_name": cat.name,
            "top_slug": top.slug, "top_name": top.name,
            "source": row.source if row else None,
            "counts_as_spending": cat.counts_as_spending and top.counts_as_spending,
            "suggested_keyword": suggest_keyword(merchant),
        })

    for tx in recon.load_transactions(db_session, userid):
        day = recon._as_date(tx.transaction_date)
        if start <= day < end:
            bill_ids = [m.billId for m in links.get(tx.id, []) if m.match_type in recon.BILL_LINK_TYPES and m.billId]
            add(CARD, str(tx.id), day, tx.description, tx.amount, bill_ids)
    for bill in recon.load_bills(db_session, userid):
        if start <= bill["date"] < end and bill["billId"] not in linked_bill_ids:
            add(BILL, bill["billId"], bill["date"], bill["shopName"], bill["total"], [])

    purchases.sort(key=lambda p: (p["date"], p["merchant"] or ""), reverse=True)

    totals = {}
    for p in purchases:
        top = totals.setdefault(p["top_slug"], {
            "slug": p["top_slug"], "name": p["top_name"], "total": 0.0, "count": 0,
            "counts_as_spending": p["counts_as_spending"], "children": {},
        })
        top["total"] += p["amount"]
        top["count"] += 1
        child = top["children"].setdefault(p["category_slug"], {"slug": p["category_slug"], "name": p["category_name"], "total": 0.0})
        child["total"] += p["amount"]

    spending_total = round(sum(t["total"] for t in totals.values() if t["counts_as_spending"]), 2)
    breakdown = []
    for t in sorted(totals.values(), key=lambda t: (not t["counts_as_spending"], -t["total"])):
        t["total"] = round(t["total"], 2)
        t["share"] = round(100 * t["total"] / spending_total, 1) if spending_total > 0 and t["counts_as_spending"] else None
        t["children"] = sorted(
            ({**c, "total": round(c["total"], 2)} for c in t["children"].values()),
            key=lambda c: -c["total"],
        )
        breakdown.append(t)

    return {
        "month": month,
        "spending_total": spending_total,
        "breakdown": breakdown,
        "purchases": purchases,
        "uncategorized_count": sum(1 for p in purchases if p["category_slug"] == UNCATEGORIZED_SLUG),
    }


def available_months(db_session, userid):
    """'YYYY-MM' strings with any purchases, newest first."""
    months = {recon._as_date(t.transaction_date).strftime("%Y-%m") for t in recon.load_transactions(db_session, userid)}
    months |= {b["date"].strftime("%Y-%m") for b in recon.load_bills(db_session, userid)}
    return sorted(months, reverse=True)
