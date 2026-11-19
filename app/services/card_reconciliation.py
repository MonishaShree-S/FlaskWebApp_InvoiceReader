"""
Reconciles grocery bills (groceries table) against Amazon Visa card
transactions (CreditCardTransactions).

All grocery purchases are paid with the Amazon Visa, so every bill should
have a card charge and every grocery card charge should have a bill. Links
are stored in CardBillMatches, one row per (card transaction, bill). A card
transaction can be linked to several bills (one payment covering more than
one receipt); a bill is linked to at most one transaction.

match_type:
  auto      -- found by auto_match(): one bill with the same amount (to the
               cent), or 2-3 bills from the same shop whose totals add up to
               the charge. Card date within MATCH_DAYS_BEFORE..
               MATCH_DAYS_AFTER of every bill date.
  manual    -- linked by the user.
  ignored   -- user marked the charge as not needing a receipt (billId NULL,
               the only row for that transaction).
  rejected  -- user unlinked an auto match; billId remembers the rejected
               bill so auto_match() never proposes that pair again.

auto_match() is idempotent and only touches transactions and bills with no
links, so it is safe to call after every import and on page loads.
"""
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import combinations

from sqlalchemy import text

from app.models.models import CardBillMatch, CreditCardTransaction

logger = logging.getLogger(__name__)

BILL_LINK_TYPES = ("auto", "manual")

# Card posting date relative to the receipt date (in days).
MATCH_DAYS_BEFORE = 2
MATCH_DAYS_AFTER = 5
AMOUNT_TOLERANCE = 0.011
# Largest number of receipts auto_match() combines into one card charge.
MAX_SPLIT_BILLS = 3

# Bills dated in the last few days of the newest statement may only show up
# on the next statement, so they are reported as "awaiting statement".
STATEMENT_TAIL_DAYS = 3
# Statements are consecutive; gaps up to this many days are still covered.
STATEMENT_GAP_DAYS = 7
# Manual-link pickers list candidates up to this many days apart (bill dates
# are sometimes misread, so this is wider than the auto-match window).
SUGGEST_DAYS = 21

# Extra words that identify a shop in card descriptions, beyond the words of
# the shop name itself (groceries.shopName, lowercased).
SHOP_ALIASES = {
    "rewe": ["rewe"],
    "kaufland": ["kaufland"],
    "goindia": ["india"],
    "tkmaxx": ["maxx"],
    "rossman": ["rossmann"],
    "rossmann": ["rossmann"],
    "dm": ["drogerie"],
    "afghan shop": ["watan"],
    "afghan": ["watan"],
    "woolworth": ["woolworth"],
    "amazon": ["amazon", "amzn"],
}

_STOPWORDS = {"shop", "markt", "market", "supermarket", "supermarkt", "gmbh", "store"}


def _words(value):
    return set(re.findall(r"[a-z0-9äöüß]+", (value or "").lower()))


def shop_keywords(shop_name):
    name = (shop_name or "").strip().lower()
    keywords = {w for w in _words(name) if len(w) >= 3 and w not in _STOPWORDS}
    if len(name) >= 2 and name.isalnum():
        keywords.add(name)  # short names like "dm"
    keywords.update(SHOP_ALIASES.get(name, []))
    return keywords


def description_matches_shop(description, shop_name):
    desc_words = _words(description)
    desc_compact = re.sub(r"[^a-z0-9äöüß]", "", (description or "").lower())
    for kw in shop_keywords(shop_name):
        if kw in desc_words or (len(kw) >= 4 and kw in desc_compact):
            return True
    return False


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def _in_window(tx_day, bill_day):
    return -MATCH_DAYS_BEFORE <= (tx_day - bill_day).days <= MATCH_DAYS_AFTER


# ----------------------------------------------------------------- schema

def ensure_schema(engine):
    """
    Creates CardBillMatches if missing. The first version of the table had a
    UNIQUE constraint on transaction_id (one bill per charge); drop it so a
    charge can be linked to several bills, and add a plain index instead.
    """
    CardBillMatch.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        uniques = conn.execute(text("""
            SELECT i.name, i.is_unique_constraint
            FROM sys.indexes i
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.object_id = OBJECT_ID('CardBillMatches') AND i.is_unique = 1
              AND i.is_primary_key = 0 AND c.name = 'transaction_id'
        """)).all()
        for name, is_constraint in uniques:
            if is_constraint:
                conn.execute(text(f"ALTER TABLE CardBillMatches DROP CONSTRAINT [{name}]"))
            else:
                conn.execute(text(f"DROP INDEX [{name}] ON CardBillMatches"))
            logger.info("Dropped unique %s on CardBillMatches.transaction_id", name)
        conn.execute(text("""
            IF NOT EXISTS (SELECT 1 FROM sys.indexes
                           WHERE object_id = OBJECT_ID('CardBillMatches')
                             AND name = 'ix_CardBillMatches_transaction_id')
                CREATE INDEX ix_CardBillMatches_transaction_id ON CardBillMatches (transaction_id)
        """))


# ---------------------------------------------------------------- loading

def load_bills(db_session, userid):
    """One row per grocery bill: billId, shop, date, total paid."""
    rows = db_session.execute(text("""
        SELECT billId,
               MAX(shopName) AS shopName,
               MIN([date]) AS bill_date,
               COALESCE(MAX(totalSum), SUM(total_price), SUM(price)) AS total
        FROM groceries
        WHERE userid = :userid AND billId IS NOT NULL
        GROUP BY billId
    """), {"userid": int(userid)}).all()
    bills = []
    for r in rows:
        if r.bill_date is None or r.total is None:
            continue
        bills.append({
            "billId": r.billId,
            "shopName": r.shopName,
            "date": _as_date(r.bill_date),
            "total": round(float(r.total), 2),
        })
    return bills


def load_transactions(db_session, userid):
    return (
        db_session.query(CreditCardTransaction)
        .filter(CreditCardTransaction.userid == int(userid))
        .order_by(CreditCardTransaction.transaction_date.asc())
        .all()
    )


def load_matches(db_session, userid):
    """transaction_id -> [CardBillMatch, ...]"""
    out = defaultdict(list)
    for m in db_session.query(CardBillMatch).filter(CardBillMatch.userid == int(userid)).all():
        out[m.transaction_id].append(m)
    return out


def _bill_links(rows):
    return [m for m in rows if m.match_type in BILL_LINK_TYPES and m.billId]


def _is_ignored(rows):
    return any(m.match_type == "ignored" for m in rows)


def statement_coverage(transactions):
    """Merged [start, end] date ranges covered by the imported statements."""
    by_statement = {}
    for tx in transactions:
        d = _as_date(tx.transaction_date)
        lo, hi = by_statement.get(tx.statement_id, (d, d))
        by_statement[tx.statement_id] = (min(lo, d), max(hi, d))

    ranges = []
    for lo, hi in sorted(by_statement.values()):
        if ranges and lo <= ranges[-1][1] + timedelta(days=STATEMENT_GAP_DAYS):
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))
    return ranges


def _in_coverage(day, ranges):
    return any(lo <= day <= hi for lo, hi in ranges)


# --------------------------------------------------------------- matching

def _single_candidates(transactions, bills, rejected):
    pairs = []
    for tx in transactions:
        tx_day = _as_date(tx.transaction_date)
        for bill in bills:
            if abs(float(tx.amount) - bill["total"]) > AMOUNT_TOLERANCE:
                continue
            if not _in_window(tx_day, bill["date"]) or (tx.id, bill["billId"]) in rejected:
                continue
            name_match = description_matches_shop(tx.description, bill["shopName"])
            delta = abs((tx_day - bill["date"]).days)
            pairs.append((0 if name_match else 1, delta, tx.id, (bill["billId"],)))
    return pairs


def _split_candidates(transactions, bills, rejected):
    """
    2..MAX_SPLIT_BILLS bills from one shop, all within the date window, whose
    totals add up to the charge. The shop must appear in the card
    description, so unrelated receipts that happen to sum up are not paired.
    """
    out = []
    for tx in transactions:
        tx_day = _as_date(tx.transaction_date)
        amount = float(tx.amount)
        by_shop = defaultdict(list)
        for bill in bills:
            if (bill["total"] < amount - AMOUNT_TOLERANCE
                    and _in_window(tx_day, bill["date"])
                    and (tx.id, bill["billId"]) not in rejected
                    and description_matches_shop(tx.description, bill["shopName"])):
                by_shop[(bill["shopName"] or "").lower()].append(bill)
        for shop_bills in by_shop.values():
            for size in range(2, min(MAX_SPLIT_BILLS, len(shop_bills)) + 1):
                for combo in combinations(shop_bills, size):
                    if abs(sum(b["total"] for b in combo) - amount) <= AMOUNT_TOLERANCE:
                        spread = max(abs((tx_day - b["date"]).days) for b in combo)
                        out.append((size, spread, tx.id, tuple(b["billId"] for b in combo)))
    return out


def _add_link(db_session, rows_by_tx, userid, tx_id, bill_id, match_type):
    """Adds a bill link, reusing a 'rejected' row for the same pair if present."""
    for row in rows_by_tx.get(tx_id, []):
        if row.match_type == "rejected" and row.billId == bill_id:
            row.match_type = match_type
            row.created_at = datetime.utcnow()
            return row
    row = CardBillMatch(userid=int(userid), transaction_id=int(tx_id), billId=bill_id, match_type=match_type)
    db_session.add(row)
    rows_by_tx.setdefault(tx_id, []).append(row)
    return row


def auto_match(db_session, userid):
    """
    Links transactions with no links to bills with no links: exact single
    matches first, then split payments (several receipts, one charge).
    Commits. Returns the number of new links.
    """
    transactions = load_transactions(db_session, userid)
    rows_by_tx = load_matches(db_session, userid)
    bills = load_bills(db_session, userid)

    linked_bills = {m.billId for rows in rows_by_tx.values() for m in _bill_links(rows)}
    rejected = {(m.transaction_id, m.billId) for rows in rows_by_tx.values() for m in rows if m.match_type == "rejected"}

    open_tx = [
        tx for tx in transactions
        if tx.amount > 0 and not _bill_links(rows_by_tx.get(tx.id, [])) and not _is_ignored(rows_by_tx.get(tx.id, []))
    ]
    open_bills = [b for b in bills if b["billId"] not in linked_bills and b["total"] > 0]

    used_tx, used_bills, created = set(), set(), 0

    def assign(candidates):
        nonlocal created
        for *_, tx_id, bill_ids in sorted(candidates):
            if tx_id in used_tx or used_bills.intersection(bill_ids):
                continue
            used_tx.add(tx_id)
            used_bills.update(bill_ids)
            for bill_id in bill_ids:
                _add_link(db_session, rows_by_tx, userid, tx_id, bill_id, "auto")
                created += 1

    assign(_single_candidates(open_tx, open_bills, rejected))
    remaining_tx = [tx for tx in open_tx if tx.id not in used_tx]
    remaining_bills = [b for b in open_bills if b["billId"] not in used_bills]
    assign(_split_candidates(remaining_tx, remaining_bills, rejected))

    if created:
        db_session.commit()
        logger.info("Card reconciliation: %d new bill links for user %s", created, userid)
    return created


def safe_auto_match(userid):
    """auto_match() in its own session; never raises (for import hooks)."""
    from app.models.models import get_session
    try:
        with get_session() as db_session:
            return auto_match(db_session, userid)
    except Exception:
        logger.exception("Card reconciliation failed for user %s", userid)
        return 0


# --------------------------------------------------------- manual actions

def _own_transaction(db_session, userid, transaction_id):
    tx = db_session.get(CreditCardTransaction, int(transaction_id))
    if tx is None or tx.userid != int(userid):
        raise ValueError("Card transaction not found.")
    return tx


def _tx_rows(db_session, transaction_id):
    return db_session.query(CardBillMatch).filter_by(transaction_id=int(transaction_id)).all()


def link(db_session, userid, transaction_id, bill_id):
    """Adds bill_id to the bills paid by this transaction (keeps existing links)."""
    _own_transaction(db_session, userid, transaction_id)
    if not any(b["billId"] == bill_id for b in load_bills(db_session, userid)):
        raise ValueError("Bill not found.")

    taken = db_session.query(CardBillMatch).filter(
        CardBillMatch.userid == int(userid),
        CardBillMatch.billId == bill_id,
        CardBillMatch.match_type.in_(BILL_LINK_TYPES),
    ).first()
    if taken is not None:
        if taken.transaction_id == int(transaction_id):
            return
        raise ValueError(f"Bill {bill_id} is already linked to another card transaction.")

    rows = _tx_rows(db_session, transaction_id)
    for row in rows:
        if row.match_type == "ignored":
            db_session.delete(row)
    _add_link(db_session, {int(transaction_id): rows}, userid, int(transaction_id), bill_id, "manual")
    db_session.commit()


def unlink(db_session, userid, transaction_id, bill_id=None):
    """
    Removes one bill link (bill_id given) or all links / the 'ignored' mark
    of a transaction. Unlinked auto matches are kept as 'rejected' so they are
    not proposed again.
    """
    _own_transaction(db_session, userid, transaction_id)
    for row in _tx_rows(db_session, transaction_id):
        if bill_id and row.billId != bill_id:
            continue
        if row.match_type == "auto" and row.billId:
            row.match_type = "rejected"
        elif row.match_type in ("manual", "ignored"):
            db_session.delete(row)
    db_session.commit()


def ignore(db_session, userid, transaction_id):
    _own_transaction(db_session, userid, transaction_id)
    rows = _tx_rows(db_session, transaction_id)
    if any(r.match_type in BILL_LINK_TYPES for r in rows):
        raise ValueError("Unlink the bills from this charge first.")
    if not any(r.match_type == "ignored" for r in rows):
        db_session.add(CardBillMatch(
            userid=int(userid), transaction_id=int(transaction_id), billId=None, match_type="ignored"
        ))
    db_session.commit()


# ---------------------------------------------------------------- report

def build_report(db_session, userid):
    """
    Everything the reconciliation page shows. Lists hold plain dicts.
      matched          -- charges with linked bills (one or more)
      bills_no_charge  -- bills inside statement coverage with no linked charge
      bills_awaiting   -- bills newer than the latest statement (not flagged)
      charges_missing  -- unlinked debits whose description looks like a shop you have bills for
      charges_other    -- other unlinked debits
      ignored          -- debits marked as needing no receipt
    """
    transactions = load_transactions(db_session, userid)
    rows_by_tx = load_matches(db_session, userid)
    bills = load_bills(db_session, userid)
    bills_by_id = {b["billId"]: b for b in bills}
    coverage = statement_coverage(transactions)
    known_shops = {b["shopName"] for b in bills if b["shopName"]}

    def tx_dict(tx):
        return {
            "id": tx.id,
            "date": _as_date(tx.transaction_date),
            "description": tx.description,
            "amount": round(float(tx.amount), 2),
            "statement_id": tx.statement_id,
        }

    matched, ignored, charges_missing, charges_other = [], [], [], []
    linked_bills = set()
    for tx in transactions:
        rows = rows_by_tx.get(tx.id, [])
        links = [m for m in _bill_links(rows) if m.billId in bills_by_id]
        if links:
            linked = []
            for m in sorted(links, key=lambda m: (bills_by_id[m.billId]["date"], m.billId)):
                bill = dict(bills_by_id[m.billId])
                bill["match_type"] = m.match_type
                bill["name_match"] = description_matches_shop(tx.description, bill["shopName"])
                linked.append(bill)
                linked_bills.add(m.billId)
            entry = tx_dict(tx)
            entry["remaining"] = round(entry["amount"] - sum(b["total"] for b in linked), 2)
            matched.append({"tx": entry, "bills": linked})
        elif _is_ignored(rows):
            ignored.append(tx_dict(tx))
        elif tx.amount > 0:
            entry = tx_dict(tx)
            entry["remaining"] = entry["amount"]
            shop = next((s for s in known_shops if description_matches_shop(tx.description, s)), None)
            entry["shop_guess"] = shop
            (charges_missing if shop else charges_other).append(entry)

    if coverage:
        flag_until = coverage[-1][1] - timedelta(days=STATEMENT_TAIL_DAYS)
        flag_ranges = coverage[:-1] + [(coverage[-1][0], flag_until)]
    else:
        flag_ranges = []

    bills_no_charge, bills_awaiting = [], []
    for bill in sorted(bills, key=lambda b: b["date"]):
        if bill["billId"] in linked_bills or bill["total"] <= 0:
            continue
        if _in_coverage(bill["date"], flag_ranges):
            bills_no_charge.append(bill)
        elif coverage and bill["date"] > flag_ranges[-1][1]:
            bills_awaiting.append(bill)

    # Charges a bill could still be added to: no links yet, or linked bills
    # that don't add up to the charge.
    open_charges = charges_missing + charges_other + [
        m["tx"] for m in matched if m["tx"]["remaining"] > AMOUNT_TOLERANCE
    ]
    for bill in bills_no_charge + bills_awaiting:
        bill["suggestions"] = sorted(
            (c for c in open_charges if abs((c["date"] - bill["date"]).days) <= SUGGEST_DAYS),
            key=lambda c: (abs(c["remaining"] - bill["total"]), abs((c["date"] - bill["date"]).days)),
        )[:5]

    open_bills = bills_no_charge + bills_awaiting
    for charge in open_charges:
        charge["suggestions"] = sorted(
            (b for b in open_bills if abs((charge["date"] - b["date"]).days) <= SUGGEST_DAYS),
            key=lambda b: (abs(charge["remaining"] - b["total"]), abs((charge["date"] - b["date"]).days)),
        )[:5]

    matched.sort(key=lambda m: m["tx"]["date"], reverse=True)
    for lst in (charges_missing, charges_other, ignored):
        lst.sort(key=lambda t: t["date"], reverse=True)
    bills_no_charge.sort(key=lambda b: b["date"], reverse=True)

    return {
        "coverage": coverage,
        "matched": matched,
        "partial": [m for m in matched if abs(m["tx"]["remaining"]) > AMOUNT_TOLERANCE],
        "bills_no_charge": bills_no_charge,
        "bills_awaiting": bills_awaiting,
        "charges_missing": charges_missing,
        "charges_other": charges_other,
        "ignored": ignored,
    }


def statement_links(db_session, userid, statement_id):
    """
    transaction_id -> {"billIds": [...], "ignored": bool, "remaining": float}
    for one statement's detail page.
    """
    bills_by_id = {b["billId"]: b for b in load_bills(db_session, userid)}
    amounts = {
        tx.id: float(tx.amount)
        for tx in db_session.query(CreditCardTransaction).filter(
            CreditCardTransaction.userid == int(userid),
            CreditCardTransaction.statement_id == statement_id,
        )
    }
    out = {}
    for tx_id, rows in load_matches(db_session, userid).items():
        if tx_id not in amounts:
            continue
        links = [m.billId for m in _bill_links(rows) if m.billId in bills_by_id]
        if links or _is_ignored(rows):
            out[tx_id] = {
                "billIds": sorted(links),
                "ignored": not links,
                "remaining": round(amounts[tx_id] - sum(bills_by_id[b]["total"] for b in links), 2),
            }
    return out
