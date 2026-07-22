import pandas as pd
from flask import current_app
from app.utils.ReadPdf import pdf_to_jpg
from app.services.groceries import ImageScanner
import logging
from sqlalchemy import inspect, text
import pandas as pd
from app.models.models import get_session
import re
from app.services.rewe  import extract_text_from_pdf
#import datetime
from datetime import datetime

logger = logging.getLogger(__name__)
from datetime import date
from sqlalchemy import and_, or_, func, cast, Integer, Date
import calendar

MONTH_NUM_TO_NAME = {i: calendar.month_name[i].lower() for i in range(1, 13)}

def split_ym(ym: str) -> tuple[int, int]:
    y, m = ym.split("-")
    return int(y), int(m)

def ym_iter(y1: int, m1: int, y2: int, m2: int):
    y, m = y1, m1
    while (y < y2) or (y == y2 and m <= m2):
        yield y, m
        m += 1
        if m == 13:
            m = 1
            y += 1

def parse_month_str(ym: str) -> tuple[date, date]:
    y, m = split_ym(ym)
    start = date(y, m, 1)
    # first day of next month
    end = date(y + (m // 12), (m % 12) + 1, 1)
    return start, end

def year_eq(col, year_int: int):
    # robust: works if YEAR column is INT or stored as TEXT
    return or_(cast(col, Integer) == year_int, col == str(year_int))

def create_dataframe(pdf_path, filename, month,year, shopname,userid):
    """
    Processes a PDF file, extracts data into a DataFrame, and associates it with a user ID.
    """
    try:
        # Convert PDF to an image
        if shopname == 'rewe':
            extracted_text = extract_text_from_pdf(filename)
            scanned_object = ImageScanner('', filename, extracted_text, month, year)
        else:
            image_path = pdf_to_jpg(pdf_path, filename, current_app.config['IMAGE_FOLDER'])
            scanned_object = ImageScanner(image_path, filename, '', month, year)

        # Extract text from the image
        text = scanned_object.get_text()
        if not text or len(text) < 10:
            logger.warning("Insufficient text detected. Attempting secondary processing.")
            try:
                try:
                    scanned_object.get_grocery_bill()
                except Exception as e:
                    logger.error(f"Error creating DataFrame from get_grocery_bill(): {str(e)}")
                df = scanned_object.create_data_frame()
            except Exception as e:
                logger.error(f"Error creating DataFrame from create_data_frame(): {str(e)}")
            raise
        else:
            try:
                try:
                    scanned_object.get_grocery_bill()
                except Exception as e:
                    logger.error(f"Error creating DataFrame from get_grocery_bill(): {str(e)}")
                df = scanned_object.create_data_frame()
            except Exception as e:
                logger.error(f"Error creating DataFrame from create_data_frame(): {str(e)}")
            raise
        # Validate DataFrame
        if df.empty:
            logger.error("Extracted DataFrame is empty. No data found in the PDF.")
            raise ValueError("Failed to extract valid data from the PDF.")

        # Add the user ID to the DataFrame
        df['userid'] = userid
        pd.set_option("display.max_columns", None)

        # Log the result
        logger.info(f"DataFrame created successfully for user ID {userid}.")
        logger.debug(f"Extracted DataFrame:\n{df}")

        return df

    except Exception as e:
        logger.error(f"Error creating DataFrame from PDF: {str(e)}")
        raise

def convert_date_old(date_str):
    try:
        # Normalize the input by removing spaces around dots
        normalized_date = re.sub(r'\s*\.\s*', '.', date_str)

        # Parse the date using a flexible format (handling both two-digit and full years)
        if len(normalized_date.split('.')[-1]) == 2:  # Two-digit year
            date_obj = datetime.strptime(normalized_date, '%d.%m.%y')
        else:  # Full year
            date_obj = datetime.strptime(normalized_date, '%d.%m.%Y')

        # Return the formatted date as 'YYYY-MM-DD'
        return date_obj.strftime('%Y-%m-%d')

    except ValueError:
        # Handle cases where the date cannot be parsed
        print(f"Error converting date: {date_str}")
        return None


def convert_date_old(date_str):
    if not date_str:
        return None
    s = date_str.strip()

    # 1) Already ISO → keep it
    if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s

    # 2) Normalize "23 . 08 . 25" → "23.08.25"
    s = re.sub(r'\s*\.\s*', '.', s)

    # 3) Try common formats in order
    fmts = [
        '%d.%m.%Y',  # 23.08.2025
        '%d.%m.%y',  # 23.08.25
        '%Y.%m.%d',  # 2025.08.23
        '%Y/%m/%d',  # 2025/08/23
        '%Y-%m-%d',  # 2025-08-23 (fallback if reached)
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass

    # 4) Last resort: pull a date-like token out and retry (handles noise around date)
    m = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{2,4})', s)
    if m:
        return convert_date(m.group(1))

    # Couldn’t parse
    print(f"Error converting date: {date_str}")
    return None

import re
from datetime import datetime, date

def convert_date(date_str):
    """Accepts str, datetime, or date — always returns an ISO 'YYYY-MM-DD' string or None."""
    if not date_str:
        return None

    # 🧩 Handle date/datetime objects directly
    if isinstance(date_str, (date, datetime)):
        return date_str.isoformat()[:10]  # 'YYYY-MM-DD'

    s = str(date_str).strip()

    # 1) Already ISO → keep it
    if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s

    # 2) Normalize "23 . 08 . 25" → "23.08.25"
    s = re.sub(r'\s*\.\s*', '.', s)

    # 3) Try common formats
    fmts = [
        '%d.%m.%Y',  # 23.08.2025
        '%d.%m.%y',  # 23.08.25
        '%Y.%m.%d',  # 2025.08.23
        '%Y/%m/%d',  # 2025/08/23
        '%Y-%m-%d',  # 2025-08-23
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).strftime('%d-%m-%Y')
        except ValueError:
            continue

    # 4) Try to pull a date-like token out of noisy text
    m = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{2,4})', s)
    if m:
        return convert_date(m.group(1))

    print(f"Error converting date: {date_str!r}")
    return None

def receipt_json_to_df(
    receipt_json: dict,
    *,
    month: str,
    year: str,
    shopname: str,
    userid: int,
    bill_date_str: str,
    bill_id: str
) -> pd.DataFrame:
    """
    Convert receipt JSON (from OpenAI vision) into a DataFrame compatible with
    your groceries DB insert logic.

    Key rule:
    - If BOTH quantity and weight_quantity are missing -> default quantity to 1.
    """

    items = receipt_json.get("items") or []
    if not items:
        return pd.DataFrame()

    address = receipt_json.get("address")
    invoice_number = receipt_json.get("invoiceNumber")
    total_sum = receipt_json.get("totalSum")

    rows = []
    for it in items:
        qty = it.get("quantity")
        weight_qty = it.get("weight_quantity")

        # ✅ Default quantity to 1 if nothing specified (common on receipts)
        if qty is None and weight_qty is None:
            qty = 1

        # line total (legacy "price" column in your DB)
        line_total = it.get("total_price")

        rows.append({
            # metadata
            "month": (month or "").lower(),
            "year": int(year),
            "userid": int(userid),
            "billId": bill_id,
            "date": bill_date_str,

            # item fields
            "item": it.get("item"),
            "quantity": qty,
            "weight_quantity": weight_qty,

            # legacy meaning: line total
            "price": line_total,

            # unified fields you already insert
            "unit_price": it.get("unit_price"),
            "unit_type": it.get("unit_type"),
            "total_price": line_total,
            "unit_price_pricePerKg": None,  # compute later if you want

            # receipt fields
            "shopName": (shopname or "").lower(),
            "address": address,
            "invoiceNumber": invoice_number,
            "totalSum": total_sum,
        })

    return pd.DataFrame(rows)


# ---------- CONSTANTS ----------
SHEET_ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls', 'tsv'}


# ---------- HELPERS ----------
def is_allowed_sheet(filename: str) -> bool:
    """Check if the uploaded file extension is one of the allowed sheet types."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in SHEET_ALLOWED_EXTENSIONS


def normalize_shop_name(raw_shop: str) -> str:
    """Trim + lowercase the shop name for consistent grouping and billId."""
    return (raw_shop or "").strip().lower()


def normalize_date_string(raw_date: str) -> str:
    """
    Normalize dates like '06.08.2025', '14.08-2025', '6/8/2025' into ISO 'YYYY-MM-DD'.
    """
    if not raw_date:
        raise ValueError("Missing date")
    cleaned = str(raw_date).strip()
    cleaned = cleaned.replace('-', '.').replace('/', '.')
    parts = [p for p in cleaned.split('.') if p]
    if len(parts) != 3:
        raise ValueError(f"Unrecognized date: {raw_date}")
    day_str, month_str, year_str = parts
    day, month, year = int(day_str), int(month_str), int(year_str)
    return f"{year:04d}-{month:02d}-{day:02d}"


def get_month_name_from_iso(iso_date: str) -> str:
    """Return month name (lowercase) from ISO date string."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return dt.strftime("%B").lower()


def collapse_whitespace(text: str) -> str:
    """Collapse internal whitespace/newlines in strings like 'geschenk\\npapier' -> 'geschenk papier'."""
    return re.sub(r'\s+', ' ', (text or '')).strip()


def float_from_cell(value) -> float:
    """
    Convert common spreadsheet number strings to float (handles comma decimal).
    Returns 0.0 for blanks or unparsable values.
    """
    if value is None or value == '':
        return 0.0
    try:
        return float(str(value).replace(',', '.'))
    except Exception:
        return 0.0


def build_items_signature(rows_for_signature):
    """
    Build a stable signature string for duplicate detection within (shop, date) groups.
    Includes item, quantity, price and pricePerKg.
    """
    normalized_rows = []
    for row_dict in rows_for_signature:
        item_name = collapse_whitespace(row_dict.get('item'))
        quantity_val = float(row_dict.get('quantity') or 0)
        price_val = float(row_dict.get('price') or 0)
        price_per_kg_val = row_dict.get('pricePerKg')
        price_per_kg_val = float(price_per_kg_val) if price_per_kg_val not in (None, "", "None") else 0.0
        normalized_rows.append(f"{item_name.lower()}|{quantity_val:g}|{price_val:.2f}|{price_per_kg_val:.3f}")
    normalized_rows.sort()
    return ";".join(normalized_rows)

def next_available_bill_id(existing_bill_ids_for_base, base_bill_id: str) -> str:
    """Return base bill id or suffixed version base_1, base_2, ... if conflicts exist."""
    if base_bill_id not in existing_bill_ids_for_base:
        return base_bill_id
    suffix_index = 1
    while f"{base_bill_id}_{suffix_index}" in existing_bill_ids_for_base:
        suffix_index += 1
    return f"{base_bill_id}_{suffix_index}"