import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime

from flask import current_app
from sqlalchemy import or_

from app.models.models import CreditCardTransaction
from app.utils.ReadPdf import extract_text_from_pdf
from app.utils.pdf_upload_helpers import validate_pdf_file

AMAZON_VISA_PROVIDER = "amazon_visa"


def _parse_german_date(date_str):
    return datetime.strptime(date_str, "%d.%m.%Y").date()


def _parse_euro(value):
    if value is None:
        return None
    return float(value.replace(".", "").replace(",", "."))


def parse_amazon_visa_statement(pdf_path, extract_text_from_pdf):
    text = extract_text_from_pdf(pdf_path, os.path.basename(pdf_path))

    transactions = []

    # ---------- Summary fields ----------
    summary = {
        "statement_start_date": None,
        "statement_end_date": None,
        "closing_date": None,
        "booking_date": None,
        "current_points": None,
        "current_points_eur": None,
        "points_earned": None,
        "points_earned_eur": None,
        "points_redeemed": None,
        "points_redeemed_eur": None,
    }

    period_match = re.search(
        r"Abrechnungszeitraum\s+(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})",
        text
    )
    if period_match:
        summary["statement_start_date"] = _parse_german_date(period_match.group(1))
        summary["statement_end_date"] = _parse_german_date(period_match.group(2))

    closing_match = re.search(r"Abschlussdatum\s+(\d{2}\.\d{2}\.\d{4})", text)
    if closing_match:
        summary["closing_date"] = _parse_german_date(closing_match.group(1))

    booking_match = re.search(r"Buchungsdatum\s+(\d{2}\.\d{2}\.\d{4})", text)
    if booking_match:
        summary["booking_date"] = _parse_german_date(booking_match.group(1))

    current_points_match = re.search(
        r"Aktuelle Punkte.*?(-?\d+)\s*\((-?[\d,]+)\s*€\)",
        text,
        re.DOTALL
    )
    if current_points_match:
        summary["current_points"] = int(current_points_match.group(1))
        summary["current_points_eur"] = _parse_euro(current_points_match.group(2))

    earned_match = re.search(
        r"Gesammelte Punkte.*?(-?\d+)\s*\((-?[\d,]+)\s*€\)",
        text,
        re.DOTALL
    )
    if earned_match:
        summary["points_earned"] = int(earned_match.group(1))
        summary["points_earned_eur"] = _parse_euro(earned_match.group(2))

    redeemed_match = re.search(
        r"Eingelöste Punkte.*?(-?\d+)\s*\((-?[\d,]+)\s*€\)",
        text,
        re.DOTALL
    )
    if redeemed_match:
        summary["points_redeemed"] = int(redeemed_match.group(1))
        summary["points_redeemed_eur"] = _parse_euro(redeemed_match.group(2))

    # ---------- Transactions ----------

    pattern = re.compile(
        r"(\d{2}\.\d{2}\.\d{4})\s+"
        r"(\*+\d{4})\s+"
        r"(.+?)\s+"
        r"([+-])\s*([\d.,]+)\s*€\s+"
        r"([+-]?)\s*(\d+)",
        re.MULTILINE
    )

    for match in pattern.finditer(text):
        date_str, card_number, description, amount_sign, amount_str, points_sign, points_str = match.groups()

        transaction_date = datetime.strptime(date_str, "%d.%m.%Y")

        amount = float(amount_str.replace(".", "").replace(",", "."))
        if amount_sign == "+":
            amount = -amount

        points = int(points_str)
        if points_sign == "-":
            points = -points

        transactions.append({
            "date": transaction_date,
            "card": card_number,
            "description": description.strip(),
            "amount": amount,
            "points": points
        })

    return {
        "summary": summary,
        "transactions": transactions
    }


def _transaction_key(tx_date, card, description, amount):
    if isinstance(tx_date, datetime):
        tx_date = tx_date.date()
    return (tx_date, (card or "").strip(), (description or "").strip(), round(float(amount), 2))


def process_credit_card_statement_upload(*, file_storage, userid, provider, db_session):
    """
    Credit card counterpart of process_one_pdf_upload(), used by the
    incoming/ watcher. Statement identity (statement month -> statement_id)
    is derived from the statement's own parsed content, not from the
    filename, and duplicate detection compares the parsed transactions, so a
    statement already uploaded through the web UI (whose statement_id embeds
    the uploaded filename) is still recognised.

    Returns the same status dict shape as process_one_pdf_upload(), with the
    statement_id in the "billId" slot. Rows are only added to db_session --
    committing is left to the caller.
    """
    if provider != AMAZON_VISA_PROVIDER:
        raise ValueError(f"Unsupported credit card provider: {provider}")

    safe_pdf_filename, _ = validate_pdf_file(file_storage)

    staging_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], "_staging")
    os.makedirs(staging_dir, exist_ok=True)
    staging_path = os.path.join(staging_dir, f"{uuid.uuid4().hex}.pdf")
    file_storage.save(staging_path)

    try:
        parsed = parse_amazon_visa_statement(staging_path, extract_text_from_pdf)
        summary = parsed["summary"]
        transactions = parsed["transactions"]

        if not transactions:
            raise ValueError("No transactions found in PDF")

        tx_dates = [tx["date"] for tx in transactions]
        month_source = summary.get("statement_end_date") or summary.get("closing_date") or max(tx_dates)
        statement_month = month_source.strftime("%Y-%m")
        statement_id = f"{provider}_{statement_month}"

        incoming = Counter(
            _transaction_key(tx["date"], tx.get("card"), tx["description"], tx["amount"])
            for tx in transactions
        )

        # Statements already on file that could be this one: same month, or
        # any transaction inside this statement's date range (the month on a
        # manual upload is whatever the user picked, so it can't be trusted
        # on its own).
        candidate_ids = {
            row.statement_id
            for row in db_session.query(CreditCardTransaction.statement_id)
            .filter(
                CreditCardTransaction.userid == userid,
                CreditCardTransaction.provider == provider,
                or_(
                    CreditCardTransaction.statement_month == statement_month,
                    CreditCardTransaction.transaction_date.between(min(tx_dates), max(tx_dates)),
                ),
            )
            .distinct()
            .all()
        }

        existing = defaultdict(Counter)
        existing_months = {}
        if candidate_ids:
            for row in db_session.query(CreditCardTransaction).filter(
                CreditCardTransaction.userid == userid,
                CreditCardTransaction.statement_id.in_(candidate_ids),
            ).all():
                existing[row.statement_id][
                    _transaction_key(row.transaction_date, row.card_number, row.description, row.amount)
                ] += 1
                existing_months[row.statement_id] = row.statement_month

        # Identical transactions -> true duplicate, always skip.
        for existing_id in sorted(existing):
            if existing[existing_id] == incoming:
                return {
                    "status": "skipped",
                    "billId": existing_id,
                    "filename": safe_pdf_filename,
                    "reason": "duplicate content"
                }

        # Same statement month, or mostly the same transactions, but not an
        # exact match -- a human has to decide which version is right.
        for existing_id in sorted(existing):
            shared = sum((existing[existing_id] & incoming).values())
            if existing_months[existing_id] == statement_month or shared * 2 >= len(transactions):
                return {
                    "status": "needs_confirmation",
                    "conflicting_billId": existing_id,
                    "filename": safe_pdf_filename,
                    "proposed_billId": statement_id,
                    "reason": f"A statement already exists ({existing_id}) sharing {shared} of {len(transactions)} transactions with this one, but the contents differ."
                }

        upload_dir = os.path.join(
            current_app.config["UPLOAD_FOLDER"], "credit_cards", provider, statement_month
        )
        os.makedirs(upload_dir, exist_ok=True)
        os.replace(staging_path, os.path.join(upload_dir, statement_id + ".pdf"))
        staging_path = None  # moved -- nothing left to clean up in `finally`

        for tx in transactions:
            db_session.add(CreditCardTransaction(
                userid=int(userid),
                statement_id=statement_id,
                provider=provider,
                statement_month=statement_month,
                transaction_date=tx["date"],
                card_number=tx.get("card"),
                description=tx["description"],
                amount=float(tx["amount"]),
                points=tx.get("points")
            ))

        return {
            "status": "inserted",
            "billId": statement_id,
            "filename": safe_pdf_filename,
            "inserted": len(transactions)
        }

    finally:
        if staging_path and os.path.exists(staging_path):
            os.remove(staging_path)