# app/utils/pdf_upload_helpers.py

import os
import math
import hashlib
import logging
from pathlib import Path

import pandas as pd
from flask import current_app
from werkzeug.utils import secure_filename

from app.models.models import models
from app.services.groceries import ImageScanner
from app.utils.ReadPdf import pdf_to_jpg, extract_text_from_pdf
from app.utils.dataHelpers import convert_date
from app.utils.dataHelpers import next_available_bill_id

import re
from datetime import datetime


logger = logging.getLogger(__name__)

MAX_PDF_SIZE_BYTES = 10 * 1024 * 1024


def derive_bill_id_from_filename(uploaded_filename: str):
    safe_name = secure_filename(uploaded_filename or "")

    if not safe_name:
        raise ValueError("Invalid or empty filename.")

    if Path(safe_name).suffix.lower() != ".pdf":
        raise ValueError("Only PDF files are accepted.")

    bill_id = Path(safe_name).stem.strip()

    if not bill_id:
        raise ValueError("Could not derive bill ID from filename.")

    return safe_name, bill_id


def validate_pdf_file(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("No selected file.")

    safe_name, bill_id = derive_bill_id_from_filename(file_storage.filename)

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    if size <= 0:
        raise ValueError(f"{safe_name} is empty.")

    if size > MAX_PDF_SIZE_BYTES:
        raise ValueError(f"{safe_name} is too large. Max size is 10 MB.")

    return safe_name, bill_id


def create_dataframe(pdf_path, filename, month, year, shopname, userid, bill_date_str):
    try:
        if shopname in ('rewe', 'kaufland'):
            extracted_text = extract_text_from_pdf(pdf_path, filename)
            scanned_object = ImageScanner('', filename, extracted_text, month, year, bill_date_str)

            text = scanned_object.get_text()
            if not text or len(text) < 10:
                logger.warning("Insufficient text detected. Attempting secondary processing.")
                scanned_object.get_groceries_bill()

            df = scanned_object.create_data_frame()

        else:
            image_path = pdf_to_jpg(pdf_path, filename, current_app.config['IMAGE_FOLDER'])
            scanned_object = ImageScanner(image_path, filename, '', month, year, bill_date_str)

            text = scanned_object.get_text()
            if not text or len(text) < 10:
                logger.warning("Insufficient text detected. Attempting secondary processing.")
                scanned_object.get_groceries_bill()
            else:
                scanned_object.get_groceries_bill()

            df = scanned_object.create_data_frame()

        if df.empty:
            raise ValueError("Failed to extract valid data from the PDF.")

        df['userid'] = userid
        pd.set_option("display.max_columns", None)

        logger.info(f"DataFrame created successfully for user ID {userid}.")
        return df

    except Exception as e:
        logger.error(f"Error creating DataFrame from PDF: {str(e)}")
        raise


def process_one_pdf_upload(
    *,
    file_storage,
    userid,
    shopname,
    month,
    year,
    bill_date_str,
    db_session,
    force_insert=False
):
    safe_pdf_filename, _ = validate_pdf_file(file_storage)

    model_shop = models.get("groceries")
    if not model_shop:
        raise RuntimeError("groceries table is not available.")

    # Extract date from filename
    date_info = extract_date_from_bill_filename(file_storage.filename)

    # Standard bill ID format: R_08_05_2026 / K_08_05_2026
    shop_prefix = shopname[0].upper() if shopname else "R"
    base_bill_id = (
        f"{shop_prefix}_{date_info['day']:02d}_"
        f"{date_info['month_num']:02d}_"
        f"{date_info['year']}"
    )

    # Find existing bill IDs for the same day
    existing_bill_ids = {
        row.billId
        for row in db_session.query(model_shop.billId)
        .filter(
            model_shop.userid == userid,
            model_shop.billId.like(f"{base_bill_id}%")
        )
        .all()
    }

    # File-hash duplicate check: catches re-uploads of the exact same file
    uploaded_bytes = file_storage.stream.read()
    file_storage.stream.seek(0)
    upload_hash = hashlib.md5(uploaded_bytes).hexdigest()

    year_month_dir_check = os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        str(year),
        str(month).lower()
    )
    for existing_id in existing_bill_ids:
        existing_path = os.path.join(year_month_dir_check, existing_id + ".pdf")
        if os.path.exists(existing_path):
            with open(existing_path, "rb") as f:
                if hashlib.md5(f.read()).hexdigest() == upload_hash:
                    return {"status": "skipped", "billId": existing_id, "filename": secure_filename(file_storage.filename), "reason": "duplicate content"}

    # Generate next available bill ID
    bill_id = next_available_bill_id(existing_bill_ids, base_bill_id)

    year_month_directory = os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        str(year),
        str(month).lower()
    )
    os.makedirs(year_month_directory, exist_ok=True)

    # Save using bill_id as filename so the PDF URL always matches
    stored_filename = bill_id + ".pdf"
    pdf_path = os.path.join(year_month_directory, stored_filename)
    file_storage.save(pdf_path)

    df = create_dataframe(
        pdf_path,
        stored_filename,
        month,
        year,
        shopname,
        userid,
        bill_date_str
    )

    df["date"] = df["date"].apply(convert_date)

    # totalSum duplicate check: if any existing bill for this date already has
    # the same total, this is a re-upload — skip it even if the filename changed
    if existing_bill_ids:
        incoming_total = pd.to_numeric(df["totalSum"].iloc[0], errors="coerce") if not df.empty else None
        if incoming_total is not None:
            existing_totals = {
                row.billId: row.totalSum
                for row in db_session.query(model_shop.billId, model_shop.totalSum)
                .filter(
                    model_shop.userid == userid,
                    model_shop.billId.like(f"{base_bill_id}%"),
                    model_shop.totalSum != None
                )
                .distinct(model_shop.billId)
                .all()
            }
            for eid, etotal in existing_totals.items():
                if etotal is not None and abs(float(etotal) - float(incoming_total)) < 0.01:
                    if not force_insert:
                        os.remove(pdf_path)
                        return {
                            "status": "needs_confirmation",
                            "conflicting_billId": eid,
                            "filename": safe_pdf_filename,
                            "total": float(incoming_total),
                            "proposed_billId": bill_id,
                            "reason": f"A bill with the same date and total €{incoming_total} already exists ({eid})"
                        }
                    # force_insert=True: user confirmed this is a different bill — proceed

    def _clean_float(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except Exception:
            pass
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except Exception:
            return None

    def _clean_str(v):
        return None if v in (None, "", "None") else str(v)

    for _, row in df.iterrows():
        address_val = _clean_str(row.get("address"))

        if address_val:
            address_val = address_val[:120]

        new_entry = model_shop(
            userid=int(userid),
            billId=_clean_str(bill_id),
            month=_clean_str(row["month"]),
            year=int(row["year"]),
            item=_clean_str(row["item"]),

            quantity=_clean_float(row.get("quantity")),
            weight_quantity=_clean_float(row.get("weight_quantity")),
            price=_clean_float(row.get("price")),

            unit_price=_clean_float(row.get("unit_price")),
            unit_type=_clean_str(row.get("unit_type")),
            total_price=_clean_float(row.get("total_price")),
            unit_price_pricePerKg=_clean_float(row.get("unit_price_pricePerKg")),

            shopName=_clean_str(shopname),
            address=address_val,
            invoiceNumber=_clean_str(row.get("invoiceNumber")),
            totalSum=_clean_float(row.get("totalSum")),
            date=row["date"]
        )

        db_session.add(new_entry)

    return {
        "status": "inserted",
        "billId": bill_id,
        "filename": safe_pdf_filename
    }


def extract_date_from_bill_filename(filename):
    """
    Extract the bill date from supported filename formats.

    Supported examples:
        R_02_05_2026.pdf
        K_05_03_2026.pdf
        REWE 29.05.2026.pdf
        Dein REWE eBon vom 15.05.2026.pdf
        Dein REWE eBon vom 15.05.2026 (1).pdf
        K_01.06.26.pdf  (DD.MM.YY with 2-digit year)

    Returns:
        {
            "date": "2026-05-15",
            "day": 15,
            "month_num": 5,
            "month": "may",
            "year": 2026
        }
    """

    # -------- Pattern 1 --------
    # R_02_05_2026.pdf
    match = re.search(r'(\d{2})_(\d{2})_(\d{4})', filename)

    # -------- Pattern 2 --------
    # REWE 29.05.2026.pdf
    # Dein REWE eBon vom 15.05.2026.pdf
    # Dein REWE eBon vom 15.05.2026 (1).pdf
    if not match:
        match = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', filename)

    # -------- Pattern 3 --------
    # K_01.06.26.pdf  (DD.MM.YY with 2-digit year)
    two_digit_year_match = None
    if not match:
        two_digit_year_match = re.search(r'(\d{2})\.(\d{2})\.(\d{2})', filename)

    if not match and not two_digit_year_match:
        raise ValueError(
            f"Could not extract a valid date from filename: {filename}"
        )

    if two_digit_year_match:
        day, month, yy = map(int, two_digit_year_match.groups())
        year = 2000 + yy
    else:
        day, month, year = map(int, match.groups())

    bill_date = datetime(year, month, day)

    return {
        "date": bill_date.strftime("%Y-%m-%d"),
        "day": day,
        "month_num": month,
        "month": bill_date.strftime("%B").lower(),
        "year": year,
    }