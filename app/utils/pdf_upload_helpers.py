# app/utils/pdf_upload_helpers.py

import os
import math
import uuid
import logging
from pathlib import Path
from collections import defaultdict

import pandas as pd
from flask import current_app
from werkzeug.utils import secure_filename

from app.models.models import models
from app.services.groceries import ImageScanner
from app.utils.ReadPdf import pdf_to_jpg, extract_text_from_pdf
from app.utils.dataHelpers import convert_date
from app.utils.dataHelpers import next_available_bill_id
from app.utils.dataHelpers import build_items_signature
from app.utils.dataHelpers import parse_receipt_time

from datetime import datetime, date


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
    db_session,
    bill_date_str=None,
    force_insert=False
):
    """
    Bill identity (date -> billId) is derived from the bill's own parsed
    content, not from the uploaded filename -- filenames are only used for
    basic type/size validation here. Duplicate detection compares parsed
    item content (via build_items_signature), not raw file bytes, so a
    re-upload is caught even if it was re-exported/re-scanned into a
    byte-different PDF.
    """
    safe_pdf_filename, _ = validate_pdf_file(file_storage)

    model_shop = models.get("groceries")
    if not model_shop:
        raise RuntimeError("groceries table is not available.")

    # Save to a staging path first -- the real bill_id depends on the date
    # parsed from the bill's own content, which isn't known yet.
    staging_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], "_staging")
    os.makedirs(staging_dir, exist_ok=True)
    staging_path = os.path.join(staging_dir, f"{uuid.uuid4().hex}.pdf")
    file_storage.save(staging_path)

    try:
        # Parse the bill content first. month/year passed here are just
        # placeholders -- corrected below from the date actually found on
        # the bill (ImageScanner.choose_bill_date already prioritizes the
        # OCR/text date over anything else).
        df = create_dataframe(
            staging_path,
            os.path.basename(staging_path),
            "",
            "",
            shopname,
            userid,
            bill_date_str
        )

        if df.empty:
            raise ValueError("Failed to extract valid data from the PDF.")

        parsed_date = df["date"].iloc[0]
        if isinstance(parsed_date, datetime):
            bill_date = parsed_date.date()
        elif isinstance(parsed_date, date):
            bill_date = parsed_date
        else:
            bill_date = datetime.strptime(str(parsed_date)[:10], "%Y-%m-%d").date()

        month = bill_date.strftime("%B").lower()
        year = bill_date.year
        df["month"] = month
        df["year"] = year

        # Standard bill ID format: R_08_05_2026 / K_08_05_2026 -- built from
        # the content-derived date, not the filename.
        shop_prefix = shopname[0].upper() if shopname else "R"
        base_bill_id = (
            f"{shop_prefix}_{bill_date.day:02d}_"
            f"{bill_date.month:02d}_"
            f"{year}"
        )

        # Find existing bills already stored for this same content-derived date
        existing_bill_ids = {
            row.billId
            for row in db_session.query(model_shop.billId)
            .filter(
                model_shop.userid == userid,
                model_shop.billId.like(f"{base_bill_id}%")
            )
            .all()
        }

        existing_signatures = {}
        if existing_bill_ids:
            existing_rows = (
                db_session.query(model_shop)
                .filter(
                    model_shop.userid == userid,
                    model_shop.billId.in_(existing_bill_ids)
                )
                .all()
            )
            rows_by_bill_id = defaultdict(list)
            for row in existing_rows:
                rows_by_bill_id[row.billId].append({
                    "item": row.item,
                    "quantity": row.quantity,
                    "price": row.price,
                    "pricePerKg": getattr(row, "pricePerKg", None),
                })
            existing_signatures = {
                bid: build_items_signature(rows)
                for bid, rows in rows_by_bill_id.items()
            }

        incoming_signature = build_items_signature(
            df[["item", "quantity", "price", "pricePerKg"]].to_dict("records")
        )

        # Same date + identical contents -> true duplicate, always skip
        # (not gated by force_insert -- an exact content match is never a
        # "different bill the user confirmed").
        if incoming_signature in existing_signatures.values():
            duplicate_bill_id = next(
                (bid for bid, sig in existing_signatures.items() if sig == incoming_signature),
                None
            )
            return {
                "status": "skipped",
                "billId": duplicate_bill_id,
                "filename": safe_pdf_filename,
                "reason": "duplicate content"
            }

        # Same date, different contents -> could be a genuine second bill
        # that day, or the same bill re-scanned with slightly different OCR
        # noise. Use the receipt's printed time and total amount (whenever
        # they can be read) to tell the two cases apart automatically, and
        # only fall back to asking a human when neither signal can rule out
        # a match against something already on file for that date.
        if existing_bill_ids and not force_insert:
            incoming_total = pd.to_numeric(df["totalSum"].iloc[0], errors="coerce") if not df.empty else None
            incoming_total = float(incoming_total) if incoming_total is not None and not pd.isna(incoming_total) else None

            try:
                incoming_raw_text = extract_text_from_pdf(staging_path, os.path.basename(staging_path))
            except Exception:
                incoming_raw_text = ""
            incoming_time = parse_receipt_time(incoming_raw_text)

            existing_totals = {}
            for row in db_session.query(model_shop.billId, model_shop.totalSum).filter(
                model_shop.userid == userid,
                model_shop.billId.in_(existing_bill_ids)
            ).all():
                if row.billId not in existing_totals and row.totalSum is not None:
                    existing_totals[row.billId] = float(row.totalSum)

            existing_times = {}
            for existing_id in existing_bill_ids:
                existing_pdf_path = os.path.join(
                    current_app.config["UPLOAD_FOLDER"], str(year), month, f"{existing_id}.pdf"
                )
                existing_time = None
                if os.path.exists(existing_pdf_path):
                    try:
                        existing_time = parse_receipt_time(
                            extract_text_from_pdf(existing_pdf_path, os.path.basename(existing_pdf_path))
                        )
                    except Exception:
                        existing_time = None
                existing_times[existing_id] = existing_time

            def _cannot_rule_out(existing_id):
                # True unless time or total clearly proves this is a
                # different bill than existing_id -- unknown values (None)
                # can never rule a match out, only a genuine mismatch can.
                same_time_or_unknown = (
                    incoming_time is None
                    or existing_times.get(existing_id) is None
                    or incoming_time == existing_times[existing_id]
                )
                same_total_or_unknown = (
                    incoming_total is None
                    or existing_totals.get(existing_id) is None
                    or abs(incoming_total - existing_totals[existing_id]) < 0.01
                )
                return same_time_or_unknown and same_total_or_unknown

            ambiguous_bill_ids = [bid for bid in existing_bill_ids if _cannot_rule_out(bid)]

            if ambiguous_bill_ids:
                conflicting_id = sorted(ambiguous_bill_ids)[0]
                return {
                    "status": "needs_confirmation",
                    "conflicting_billId": conflicting_id,
                    "filename": safe_pdf_filename,
                    "total": incoming_total,
                    "time": incoming_time.isoformat() if incoming_time else None,
                    "proposed_billId": next_available_bill_id(existing_bill_ids, base_bill_id),
                    "reason": f"A bill already exists for {bill_date.isoformat()} ({conflicting_id}) with different contents, and its time/total couldn't be told apart from this one; confirm this is a separate bill."
                }
            # Otherwise the receipt time and/or total clearly separates this
            # from every bill already on file for the date -- fall through
            # and insert it as a genuine second bill, no human needed.

        # Generate next available bill ID (base, or base_1/base_2/... if a
        # confirmed different bill already exists for this same date)
        bill_id = next_available_bill_id(existing_bill_ids, base_bill_id)

        # Now that the real bill_id is known, move the file out of staging
        # and into its final home.
        year_month_directory = os.path.join(
            current_app.config["UPLOAD_FOLDER"],
            str(year),
            month
        )
        os.makedirs(year_month_directory, exist_ok=True)
        pdf_path = os.path.join(year_month_directory, bill_id + ".pdf")
        os.replace(staging_path, pdf_path)
        staging_path = None  # moved -- nothing left to clean up in `finally`

        df["date"] = df["date"].apply(convert_date)

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

    finally:
        if staging_path and os.path.exists(staging_path):
            os.remove(staging_path)