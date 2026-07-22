import os
import re
from datetime import datetime


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