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
