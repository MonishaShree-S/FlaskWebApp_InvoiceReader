#Flaskapp/main_routes.py
from io import BytesIO
from collections import defaultdict
#from sqlalchemy import func, and_
# NEW imports for sheet upload#
from flask import Blueprint, request, current_app, jsonify, render_template, redirect, url_for, abort, send_file, \
    Response
from sqlalchemy import bindparam, String
from flask_login import login_required
import os
#import logging
import pandas as pd
#from app.services.groceries import ImageScanner
from app.models.models import Users, models, SplitInvoiceUser, get_session, SplitInvoiceDetail, Settlements, \
    DebtSettlements, DebtSettlementsBills,DebtSummary,SettlementLog, CreditCardTransaction, ShoppingListItem
#from app.utils.dataHelpers import convert_date, create_dataframe, receipt_json_to_df,is_allowed_sheet,collapse_whitespace,normalize_date_string,normalize_shop_name,float_from_cell,build_items_signature,get_month_name_from_iso,_split_ym,
from app.utils.dataHelpers import *
from app.utils.ReadPdf import pdf_to_jpg, extract_text_from_pdf
from app.utils.openai_receipt_parser import parse_receipt_image_to_json
from app.utils.pdf_upload_helpers import *
from datetime import datetime, timezone
#from decimal import Decimal
from app.utils.route_helpers import sanitize_input, fetch_user_id_from_session,allowed_file,allowed_image
import math
from werkzeug.utils import secure_filename
from app.services.credit_cards import parse_amazon_visa_statement
from app.services import card_reconciliation
import sys
sys.stdout.reconfigure(encoding='utf-8')

# Blueprint setup
main_bp = Blueprint('main', __name__)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'pdf'}
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_BATCH_FILES = 20
MAX_PDF_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per PDF


@main_bp.route('/index')
def index():
    return render_template('index.html')


@main_bp.route('/upload', methods=['POST'])
@login_required
def upload_file():
    """
    Handles file uploads by saving the file and recording metadata in the database.
    """
    try:
        userid = fetch_user_id_from_session()
        if not userid:
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        if 'file' not in request.files:
            return jsonify({'error': 'No file part in the request.'}), 400
        # Fetch and validate inputs
        #filename = request.form.get('filename')
        #filename_for_billid=filename
        file = request.files['file']

        safe_filename = secure_filename(file.filename)

        if not safe_filename.lower().endswith('.pdf'):
            return jsonify({'error': 'Only PDF files are accepted.'}), 400

        filename = safe_filename
        filename_for_billid = os.path.splitext(safe_filename)[0]

        # Ensure the filename ends with .pdf
        if not filename.lower().endswith('.pdf'):
            filename += '.pdf'

        session['billid'] = filename_for_billid
        #print("session['billid']", session['billid'])

        NameOfTheShop = request.form.get('NameOfTheShop')
        session['NameOfTheShop'] = NameOfTheShop.lower()

        month = request.form.get('month').lower()
        year = request.form.get('year')
        bill_date_str = request.form.get('datePicker')
        # Debugging log to verify values are received correctly
        logger.info(f"Received Upload: filename={filename_for_billid}, shop={NameOfTheShop}, month={month}, year={year}")

        if  filename is None:
            logger.error("Missing filename  in the request.")
            return jsonify({'error': 'Missing filename'}), 400
        if bill_date_str is None or '':
            logger.error("Missing  year in the request.")
            return jsonify({'error': 'Missing bill_date'}), 40
        else:
            bill_date = datetime.strptime(bill_date_str, "%Y-%m-%d").date()
        if month is None or '':
            logger.error("Missing  month in the request.")
            return jsonify({'error': 'Missing month'}), 400
        if year is None or '':
            logger.error("Missing  year in the request.")
            return jsonify({'error': 'Missing year'}), 400
        if NameOfTheShop is None:
            logger.error("Missing  ShopNmae in the request.")
            return jsonify({'error': 'Missing ShopNmae'}), 400

        if not sanitize_input(filename) or not sanitize_input(month) or not sanitize_input(year) or not sanitize_input(NameOfTheShop):
            logger.error("Invalid characters detected in input.")
            return jsonify({'error': 'Input contains invalid characters.'}), 400

        # Check if the uploaded file is present
        if 'file' not in request.files:
            logger.error("No file part in the request.")
            return jsonify({'error': 'No file part in the request.'}), 400

        file = request.files['file']
        if file.filename == '':
            logger.error("No selected file.")
            return jsonify({'error': 'No selected file.'}), 400

        if not allowed_file(file.filename):
            logger.error(f"File type not allowed: {file.filename}")
            return jsonify({'error': 'File type not allowed. Only PDF files are accepted.'}), 400

        # Save the uploaded file
        year_month_directory = os.path.join(current_app.config['UPLOAD_FOLDER'], year, month)
        os.makedirs(year_month_directory, exist_ok=True)
        print(f"Directory created: {year_month_directory}")
        pdf_path = os.path.join(year_month_directory, filename)
        file.save(pdf_path)

        # Fetch the user ID from the session

        userid = request.form.get('userid') or fetch_user_id_from_session()
        if not userid or not str(userid).isdigit():
            logger.error(f"Invalid or missing user ID during upload: {userid}")
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        shopname = session.get('NameOfTheShop').lower()
        modelShop = models.get("groceries")
        if not modelShop:
            logger.error("groceries model not initialized.")
            return jsonify({'error': 'groceries table is not available.'}), 500

        with get_session() as db_session:
            # Check for duplicates
            existing_count = db_session.query(modelShop).filter_by(userid=userid, billId=filename_for_billid).count()

            if existing_count > 0:
                logger.info(f"Bill ID {filename} already exists in the database. Skipping insertion.")
                #return redirect(url_for('main.view_data'))
                logger.warning(f"Bill {filename_for_billid} is already split.")
                return jsonify({
                    "warning": f"Bill ID {filename_for_billid} already exists",
                    "Bill_exists": True
                }), 200

            else:
                # Generate the DataFrame using the create_dataframe function

                df = create_dataframe(pdf_path, filename, month, year, shopname, userid,bill_date_str)
                if df.empty:
                    logger.info("No data extracted from the PDF.")
                    # return render_template('view_bk30jan.html', message="No records found.")

                df['date'] = df['date'].apply(convert_date)
                # Insert metadata
                def _clean_float(v):
                    # Turn NaN-like values into None; keep valid floats
                    if v is None:
                        return None
                    try:
                        # pandas NA/NaN
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

                try:

                    logger.info(f"shopname from form/session: {shopname}")
                    #logger.info(f"row shopName from parser: {row.get('shopName')}")

                    for index, row in df.iterrows():

                        address_val = _clean_str(row.get('address'))

                        if address_val:
                            address_val = address_val[:120]
                        new_entry = modelShop(
                                userid=int(userid),
                                billId=_clean_str(filename_for_billid),
                                month=_clean_str(row['month']),
                                year=int(row['year']),
                                item=_clean_str(row['item']),

                                # quantities
                                quantity=_clean_float(row.get('quantity')),
                                weight_quantity=_clean_float(row.get('weight_quantity')),

                                # legacy line price (what you already had)
                                price=_clean_float(row.get('price')),

                                # old price-per-kg still in the DF (optional now)
                                #pricePerKg=_clean_float(row.get('pricePerKg')),

                                # 🔹 NEW unified fields
                                unit_price=_clean_float(row.get('unit_price')),  # €/piece or €/kg
                                unit_type=_clean_str(row.get('unit_type')),  # "piece", "kg", ...
                                total_price=_clean_float(row.get('total_price')),  # line total
                                unit_price_pricePerKg=_clean_float(row.get('unit_price_pricePerKg')),
                                # renamed column in DB

                                # meta
                                #shopName=_clean_str(row['shopName']),
                                shopName=_clean_str(shopname),
                                address=address_val,
                                #address=_clean_str(row['address']),
                                invoiceNumber=_clean_str(row['invoiceNumber']),
                                totalSum=_clean_float(row.get('totalSum')),
                                date=row['date']  # already a date object or iso string
                            )
                        db_session.add(new_entry)

                    db_session.commit()
                    logger.info(f"File metadata for {filename_for_billid} recorded successfully.")
                    return redirect(url_for('main.view_data'))
                except Exception as e:
                    db_session.rollback()  # Rollback on error
                    logger.error(f"Error inserting data: {str(e)}")
                    return jsonify({'error': 'Database error.', 'details': str(e)}), 500


    except Exception as e:
        logger.error(f"Error during file upload: {str(e)}")
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500
@main_bp.route("/upload_batch", methods=["POST"])
@login_required
def upload_batch_files():
    """
    Batch PDF upload.
    Each billId is derived from the date parsed off the bill's own content
    (not the uploaded filename, which can be anything).
    """
    try:
        userid = fetch_user_id_from_session()
        if not userid:
            return jsonify({"error": "User not authenticated or invalid session."}), 403

        files = request.files.getlist("files")
        if not files:
            return jsonify({"error": "No PDF files provided."}), 400

        if len(files) > MAX_BATCH_FILES:
            return jsonify({
                "error": f"Too many files. Maximum allowed is {MAX_BATCH_FILES} PDFs per batch."
            }), 400

        name_of_shop = request.form.get("NameOfTheShop")


        if not name_of_shop:
            return jsonify({"error": "Missing shop name."}), 400


        shopname = name_of_shop.lower().strip()



        if not  sanitize_input(shopname):
            return jsonify({"error": "Input Shop contains invalid characters."}), 400

        inserted = []
        skipped = []
        errors = []
        needs_confirmation = []

        # filenames the user explicitly confirmed as different bills
        force_filenames = set(request.form.getlist("force_insert_filenames"))

        with get_session() as db_session:
            for file in files:
                try:
                    is_forced = secure_filename(file.filename) in force_filenames

                    result = process_one_pdf_upload(
                        file_storage=file,
                        userid=int(userid),
                        shopname=shopname,
                        db_session=db_session,
                        force_insert=is_forced
                    )

                    if result["status"] == "inserted":
                        inserted.append(result)
                    elif result["status"] == "skipped":
                        skipped.append(result)
                    elif result["status"] == "needs_confirmation":
                        needs_confirmation.append(result)
                    else:
                        errors.append(result)

                except Exception as file_exc:
                    errors.append({
                        "filename": secure_filename(file.filename or "unknown"),
                        "reason": str(file_exc)
                    })

            if inserted:
                db_session.commit()
                session["billid"] = inserted[-1]["billId"]
                session["NameOfTheShop"] = shopname
            else:
                db_session.rollback()

        return jsonify({
            "success": True,
            "inserted_bills": inserted,
            "skipped_duplicates": skipped,
            "needs_confirmation": needs_confirmation,
            "errors": errors,
            "redirect": url_for("main.view_data", billId=inserted[-1]["billId"]) if inserted else None
        }), 200

    except Exception as exc:
        logger.exception("Error during batch PDF upload")
        return jsonify({
            "error": "An internal error occurred.",
            "details": str(exc)
        }), 500

@main_bp.route('/upload_image_bill', methods=['POST'])
@login_required
def upload_image_bill():
    try:
        filename = request.form.get("filename")
        filename_for_billid = filename

        NameOfTheShop = request.form.get("NameOfTheShop")
        month = (request.form.get("month") or "").lower()
        year = request.form.get("year")
        bill_date_str = request.form.get("datePicker")

        if not filename_for_billid:
            return jsonify({"error": "Missing filename"}), 400
        if not bill_date_str:
            return jsonify({"error": "Missing bill_date"}), 400
        if not month:
            return jsonify({"error": "Missing month"}), 400
        if not year:
            return jsonify({"error": "Missing year"}), 400
        if not NameOfTheShop:
            return jsonify({"error": "Missing ShopName"}), 400

        # file present?
        if "file" not in request.files:
            return jsonify({"error": "No file part in the request."}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No selected file."}), 400

        if not allowed_image(file.filename):
            return jsonify({"error": "File type not allowed. Only PNG/JPG/JPEG/WEBP."}), 400

        # Save to uploads/<year>/<month> just like PDFs
        year_month_directory = os.path.join(current_app.config["UPLOAD_FOLDER"], year, month)
        os.makedirs(year_month_directory, exist_ok=True)

        # preserve image extension
        ext = file.filename.rsplit(".", 1)[1].lower()
        stored_filename = secure_filename(f"{filename_for_billid}.{ext}")
        image_path = os.path.join(year_month_directory, stored_filename)
        file.save(image_path)

        userid = request.form.get("userid") or fetch_user_id_from_session()
        if not userid or not str(userid).isdigit():
            return jsonify({"error": "User not authenticated or invalid session."}), 403

        shopname = NameOfTheShop.lower()

        modelShop = models.get("groceries")
        if not modelShop:
            return jsonify({"error": "groceries table is not available."}), 500

        with get_session() as db_session:
            # duplicate check: same billId like your PDF logic
            existing_count = db_session.query(modelShop).filter_by(userid=userid, billId=filename_for_billid).count()
            if existing_count > 0:
                return jsonify({
                    "warning": f"Bill ID {filename_for_billid} already exists",
                    "Bill_exists": True
                }), 200

            # ---- 1) Vision → JSON
            receipt_json = parse_receipt_image_to_json(image_path)

            # ---- 2) JSON → DataFrame
            df = receipt_json_to_df(
                receipt_json,
                month=month,
                year=year,
                shopname=shopname,
                userid=int(userid),
                bill_date_str=bill_date_str,
                bill_id=filename_for_billid
            )

            if df.empty:
                return jsonify({"error": "No items extracted from image."}), 400

            # (reuse your cleaning helpers pattern)
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

            # Insert like your PDF route insert-loop
            for _, row in df.iterrows():
                new_entry = modelShop(
                    userid=int(userid),
                    billId=_clean_str(filename_for_billid),
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
                    shopName=_clean_str(row["shopName"]),
                    address=_clean_str(row.get("address")),
                    invoiceNumber=_clean_str(row.get("invoiceNumber")),
                    totalSum=_clean_float(row.get("totalSum")),
                    date=row.get("date"),
                )
                db_session.add(new_entry)

            db_session.commit()
            session["billid"] = filename_for_billid
            return redirect(url_for("main.view_data"))

    except Exception as e:
        current_app.logger.exception("Error during image upload")
        return jsonify({"error": "An internal error occurred.", "details": str(e)}), 500
#--------------------------------------------------------------------------------------------------------------
#                                                                CREDITCARDS  SECTION
#--------------------------------------------------------------------------------------------------------------
@main_bp.route("/upload_credit_card_bill", methods=["POST"])
@login_required
def upload_credit_card_bill():
    try:
        userid = fetch_user_id_from_session()
        if not userid:
            return jsonify({"error": "User not authenticated"}), 403

        provider = request.form.get("creditCardProvider")
        statement_month = request.form.get("statementMonth")
        file = request.files.get("file")

        if not provider:
            return jsonify({"error": "Missing provider"}), 400

        if not statement_month:
            return jsonify({"error": "Missing statement month"}), 400

        if not file:
            return jsonify({"error": "Missing PDF"}), 400

        if provider != "amazon_visa":
            return jsonify({"error": "Only Amazon Visa is supported for now"}), 400

        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400

        if not allowed_file(file.filename):
            return jsonify({"error": "Only PDF files are accepted"}), 400

        upload_dir = os.path.join(
            current_app.config["UPLOAD_FOLDER"],
            "credit_cards",
            provider,
            statement_month
        )
        os.makedirs(upload_dir, exist_ok=True)

        safe_filename = secure_filename(file.filename)
        pdf_path = os.path.join(upload_dir, safe_filename)
        file.save(pdf_path)

        #transactions = parse_amazon_visa_statement(pdf_path)
        parsed = parse_amazon_visa_statement(pdf_path, extract_text_from_pdf)

        summary = parsed["summary"]
        transactions = parsed["transactions"]

        if not transactions:
            return jsonify({"error": "No transactions found in PDF"}), 400

        statement_id = f"{provider}_{statement_month}_{safe_filename}"

        with get_session() as db_session:
            existing_count = db_session.query(CreditCardTransaction).filter_by(
                userid=userid,
                statement_id=statement_id
            ).count()

            if existing_count > 0:
                return jsonify({
                    "error": "This credit card statement was already uploaded"
                }), 400

            for tx in transactions:
                new_transaction = CreditCardTransaction(
                    userid=int(userid),
                    statement_id=statement_id,
                    provider=provider,
                    statement_month=statement_month,
                    transaction_date=tx["date"],
                    card_number=tx.get("card"),
                    description=tx["description"],
                    amount=float(tx["amount"]),
                    points=tx.get("points")
                )
                db_session.add(new_transaction)

            db_session.commit()

        card_reconciliation.safe_auto_match(userid)

        return jsonify({
            "success": True,
            "provider": provider,
            "month": statement_month,
            "inserted": len(transactions)
        }), 200

    except Exception as e:
        logger.error(f"Credit card upload failed: {str(e)}")
        return jsonify({
            "error": "An internal error occurred",
            "details": str(e)
        }), 500
from types import SimpleNamespace
from sqlalchemy import func
from flask import render_template, jsonify
from flask_login import login_required

@main_bp.route("/credit_card_statements", methods=["GET"])
@login_required
def credit_card_statements():
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated or invalid session."}), 403

    #CreditCardTransactions = models.get("CreditCardTransaction") or models.get("credit_card_transaction")
    CreditCardTransactions = CreditCardTransaction
    if not CreditCardTransactions:
        return jsonify({"error": "CreditCardTransactions model not found."}), 500

    with get_session() as db_session:
        rows = (
            db_session.query(
                CreditCardTransactions.statement_id.label("statement_id"),
                CreditCardTransactions.provider.label("provider"),
                CreditCardTransactions.statement_month.label("statement_month"),
                func.count(CreditCardTransactions.id).label("transaction_count"),
                func.coalesce(func.sum(CreditCardTransactions.amount), 0).label("total_amount"),
            )
            .filter(CreditCardTransactions.userid == userid)
            .group_by(
                CreditCardTransactions.statement_id,
                CreditCardTransactions.provider,
                CreditCardTransactions.statement_month,
            )
            .order_by(func.max(CreditCardTransactions.transaction_date).desc())
            .all()
        )

        statements = [
            SimpleNamespace(
                statement_id=row.statement_id,
                provider=row.provider,
                statement_month=row.statement_month,
                transaction_count=row.transaction_count,
                total_amount=float(row.total_amount or 0),
            )
            for row in rows
        ]

    return render_template("credit_card_statements.html", statements=statements)


@main_bp.route("/credit_card_statements/<path:statement_id>", methods=["GET"])
@login_required
def credit_card_statement_detail(statement_id):
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated or invalid session."}), 403

    #CreditCardTransactions = models.get("CreditCardTransaction") or models.get("credit_card_transaction")
    CreditCardTransactions = CreditCardTransaction
    if not CreditCardTransactions:
        return jsonify({"error": "CreditCardTransactions model not found."}), 500

    with get_session() as db_session:
        transactions = (
            db_session.query(CreditCardTransactions)
            .filter(
                CreditCardTransactions.userid == userid,
                CreditCardTransactions.statement_id == statement_id,
            )
            .order_by(CreditCardTransactions.transaction_date.asc())
            .all()
        )

    with get_session() as db_session:
        split_details = (
            db_session.query(DebtSettlementsBills)
            .filter(
                DebtSettlementsBills.billId == statement_id,
                DebtSettlementsBills.created_by == userid,
            )
            .all()
        )
        split_details = [
            SimpleNamespace(
                paid_by_email=d.paid_by_email,
                owed_by_email=d.owed_by_email,
                amount=d.amount,
            )
            for d in split_details
        ]

    with get_session() as db_session:
        card_reconciliation.auto_match(db_session, userid)
        bill_links = card_reconciliation.statement_links(db_session, userid, statement_id)

    return render_template(
        "credit_card_statement_detail.html",
        statement_id=statement_id,
        transactions=transactions,
        split_details=split_details,
        bill_links=bill_links,
    )


@main_bp.route("/card_reconciliation", methods=["GET"])
@login_required
def card_reconciliation_page():
    """Grocery bills checked against Amazon Visa transactions."""
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated or invalid session."}), 403

    with get_session() as db_session:
        card_reconciliation.auto_match(db_session, userid)
        report = card_reconciliation.build_report(db_session, userid)

    return render_template("card_reconciliation.html", report=report)


@main_bp.route("/card_reconciliation/<action>", methods=["POST"])
@login_required
def card_reconciliation_action(action):
    """
    action: link (transaction_id + billId; adds a bill to the charge),
    unlink (transaction_id, optional billId -- without it all links go),
    ignore (transaction_id).
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated or invalid session."}), 403

    data = request.get_json(silent=True) or {}
    transaction_id = data.get("transaction_id")
    if not transaction_id:
        return jsonify({"error": "Missing transaction_id"}), 400

    try:
        with get_session() as db_session:
            if action == "link":
                bill_id = (data.get("billId") or "").strip()
                if not bill_id:
                    return jsonify({"error": "Missing billId"}), 400
                card_reconciliation.link(db_session, userid, transaction_id, bill_id)
            elif action == "unlink":
                card_reconciliation.unlink(db_session, userid, transaction_id, (data.get("billId") or "").strip() or None)
            elif action == "ignore":
                card_reconciliation.ignore(db_session, userid, transaction_id)
            else:
                return jsonify({"error": f"Unknown action {action}"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"success": True})


@main_bp.route("/split_credit_card_statement/<path:statement_id>", methods=["GET", "POST"])
@login_required
def split_credit_card_statement(statement_id):
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated or invalid session."}), 403

    if request.method == "GET":
        with get_session() as db_session:
            transactions = (
                db_session.query(CreditCardTransaction)
                .filter(
                    CreditCardTransaction.userid == userid,
                    CreditCardTransaction.statement_id == statement_id,
                )
                .order_by(CreditCardTransaction.transaction_date.asc())
                .all()
            )

            logged_in_user = db_session.query(Users).filter_by(userid=userid).first()
            if not logged_in_user:
                return jsonify({"error": "Logged-in user not found."}), 400

            logged_in_user_email = logged_in_user.username
            logged_in_user_name = logged_in_user.firstname

            # Ensure logged-in user exists in SplitInvoiceUser
            existing = db_session.query(SplitInvoiceUser).filter_by(email=logged_in_user_email).first()
            if not existing:
                db_session.add(SplitInvoiceUser(email=logged_in_user_email, name=logged_in_user_name, userid=userid))
                db_session.commit()

            split_invoice_users = db_session.query(SplitInvoiceUser).filter(
                SplitInvoiceUser.userid == userid,
                SplitInvoiceUser.email != logged_in_user_email,
            ).all()

            total_amount = sum(tx.amount for tx in transactions)

        return render_template(
            "split_credit_card.html",
            statement_id=statement_id,
            transactions=transactions,
            total_amount=total_amount,
            split_invoice_users=split_invoice_users,
            logged_in_user={"email": logged_in_user_email, "name": logged_in_user_name},
        )

    # POST — process the split
    try:
        data = request.get_json()
        paid_by_email = data.get("paid_by")
        tx_data = data.get("transactions", [])
        overwrite = bool(data.get("overwrite", False))

        if not paid_by_email or not tx_data:
            return jsonify({"error": "Required fields are missing."}), 400

        with get_session() as db_session:
            logged_in_user = db_session.query(Users).filter_by(userid=userid).first()
            if not logged_in_user:
                return jsonify({"error": "Logged-in user not found."}), 400

            payer = db_session.query(SplitInvoiceUser).filter_by(email=paid_by_email).first()
            if not payer:
                return jsonify({"error": "Payer not found."}), 400

            existing_splits = db_session.query(DebtSettlementsBills).filter_by(
                billId=statement_id, created_by=userid
            ).count()

            if existing_splits > 0 and not overwrite:
                return jsonify({"split_exists": True}), 200

            if overwrite:
                db_session.query(DebtSettlementsBills).filter_by(
                    billId=statement_id, created_by=userid
                ).delete()
                db_session.commit()

            # Aggregate amount owed per person
            debt_map = {}  # owed_by_email -> total amount
            for tx in tx_data:
                shared_with = tx.get("shared_with", [])
                amount = float(tx.get("amount", 0))
                if not shared_with:
                    continue
                amount_per_user = amount / len(shared_with)
                for user_email in shared_with:
                    if user_email == paid_by_email:
                        continue  # payer's own share — no debt
                    debt_map[user_email] = debt_map.get(user_email, 0.0) + amount_per_user

            for owed_by_email, total in debt_map.items():
                db_session.add(DebtSettlementsBills(
                    billId=statement_id,
                    paid_by_email=paid_by_email,
                    owed_by_email=owed_by_email,
                    amount=round(total, 2),
                    settled=False,
                    created_by=userid,
                ))
            db_session.commit()

            calculate_overall_debts_and_insert(db_session, current_user_id=userid)

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Error splitting CC statement {statement_id}: {e}")
        return jsonify({"error": "An internal error occurred.", "details": str(e)}), 500
















@main_bp.route('/check_filename', methods=['POST'])
@login_required
def check_filename():
    """
    Check if a filename already exists in the database.
    """
    try:
        filename = request.form.get('filename')
        if not filename:
            logger.error("Missing filename in the request.")
            return jsonify({'error': 'Missing filename.'}), 400

        userid = fetch_user_id_from_session()
        if not userid:
            logger.error("User ID not found in session or database.")
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        modelShop = models.get("groceries")
        if not modelShop:
            logger.error("groceries model not initialized.")
            return jsonify({'error': 'groceries table is not available.'}), 500

        with get_session() as db_session:
            existing_count = db_session.query(modelShop).filter_by(userid=userid, billId=filename).count()
            logger.info(f"Check Filename: {filename}, Existing Count {existing_count} ")
            if existing_count > 0:
                return jsonify({'exists': True}), 200
            else:
                return jsonify({'exists': False}), 200

    except Exception as e:
        logger.error(f"Error during filename check: {str(e)}")
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500

@main_bp.route('/view', methods=['GET'])
@login_required
def view_data():
    """
    Fetches data for the authenticated user and displays item details
    associated with the Bill ID from the session.
    """
    try:
        # Fetch user ID from the session
        userid = fetch_user_id_from_session()
        if not userid:
            logger.error("User ID not found in session.")
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        # Fetch the Bill ID from the session
        #billid = session.get('billid')
        billid = request.args.get('billId') or session.get('billid')
        logger.info(f"billid for user {userid} with Bill ID {billid}.")
        if not billid:
            logger.error("Bill ID not found in session.")
            return jsonify({'error': 'Bill ID not found in session.'}), 400
        session['billid'] = billid
        # Query the database for the user's uploaded file records based on userid and billId

        modelShop = models.get("groceries")
        if not modelShop:
            logger.error(f"{modelShop} model not initialized.")
            return jsonify({'error': 'groceries table is not available.'}), 500

        with get_session() as db_session:
            # Query for records that match the user ID and Bill ID
            records = db_session.query(modelShop).filter_by(userid=userid, billId=billid).all()

            if not records:
                logger.info(f"No uploaded files found for user {userid} with Bill ID {billid}.")
                return render_template('view.html', message="No records found.")

            else:
                # Process the records to extract details
                logger.info(f"files found for user {userid} with Bill ID {billid}.")
                bill_info = records[0].billId  # Assuming all records belong to the same Bill ID
                totalSum_info = records[0].totalSum
                address_info = records[0].address  # Assuming the same address for all records
                date_info = records[0].date  # Assuming the same date for all records
                ShopName = records[0].shopName
                # Fetch debt settlement details for the bill ID
                debt_settlement_details = fetch_debt_settlement_details(billid, userid)
                # Prepare the item data to display in the table


                split_details = db_session.query(SplitInvoiceDetail).filter_by(billId=billid, userid=userid).all()
                logger.info(f"Split details fetched for user {userid}, bill {billid}: {split_details}")
                # Fetch split invoice users (InvoiceUsers) to build an email-to-name mapping.
                split_invoice_users = db_session.query(SplitInvoiceUser).filter_by(userid=userid).all()

                # Build mapping from email to name.
                email_to_name = {user.email: user.name for user in split_invoice_users}
                # Build the initial items_data from groceries records.
                items_data = [
                    {
                        'item': record.item,
                        'quantity': record.quantity,
                        'weight_quantity': record.weight_quantity,
                        'price': record.price,
                        'unit_price_pricePerKg': record.unit_price_pricePerKg,
                        'shared_with': ''  # default empty if not split
                    }
                    for record in records
                ]

                # If split details exist, merge the shared_with info.
                if split_details:
                    # Create a mapping from item to list of shared_with emails.
                    shared_map = {}
                    for detail in split_details:
                        # Use the item as key; append the shared user (you might choose to display email or name)
                        name = email_to_name.get(detail.shared_with, detail.shared_with)
                        shared_map.setdefault(detail.item, []).append(name)

                    # Update items_data with the shared_with information.
                    for item_data in items_data:
                        if item_data['item'] in shared_map:
                            item_data['shared_with'] = ', '.join(shared_map[item_data['item']])

                # Also fetch split_invoice_users and logged in user info as before.
                split_invoice_users = db_session.query(SplitInvoiceUser).filter_by(userid=userid).all()
                logged_in_user = db_session.query(Users).filter_by(userid=userid).first()
                is_split = bool(split_details)
                return render_template(
                    'view.html',
                    bill_info=bill_info,
                    totalSum_info=totalSum_info,
                    address_info=address_info,
                    ShopName=ShopName,
                    date_info=date_info,
                    items_data=items_data,
                    selected_date=date_info,
                    debt_settlement_details=debt_settlement_details,
                    split_invoice_users = split_invoice_users,
                    logged_in_user = logged_in_user,
                    is_split=is_split
                )

    except Exception as e:
        logger.error(f"Error processing view data: {str(e)}")
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500


@main_bp.route('/view_data_by_date', methods=['GET'])
@login_required
def view_data_by_date():
    """
    Fetches and displays data from the groceries table for the authenticated user,
    filtered by a specific date. Then redirects to the main view page with the Bill ID.
    """
    try:
        # Get the selected date from the request arguments.
        selected_date = request.args.get('date') or request.args.get('datePicker')
        if not selected_date:
            logger.error("No date provided in the request.")
            return render_template('view_data.html', message="Please provide a valid date.")

        # Debugging log to check the received date
        logger.info(f"Received selected_date: {selected_date}")

        # Fetch the user ID from the session.
        userid = fetch_user_id_from_session()
        if not userid:
            logger.error("User ID not found in session.")
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        with get_session() as db_session:
            # Retrieve the groceries model.
            modelShop = models.get("groceries")
            if not modelShop:
                logger.error("groceries model not initialized.")
                return jsonify({'error': 'groceries table is not available.'}), 500

            # Query records for the given userid and selected date.
            records = db_session.query(modelShop).filter_by(userid=userid, date=selected_date).all()
            if not records:
                logger.info(f"No records found for user {userid} on {selected_date}.")
                return jsonify({'warning': f'No records found for {selected_date}'}), 200

            # **Fetch Bill ID from the first record since all records for the date should belong to the same bill**
            billid = records[0].billId

            # **Store the Bill ID in the session**
            session['billid'] = billid

            # **Redirect to `/view` so that it can use the Bill ID from the session**
            return redirect(url_for('main.view_data'))

    except Exception as e:
        logger.error(f"Error fetching data: {str(e)}")
        return render_template('view_data.html', message="An error occurred while fetching data.")


@main_bp.route('/view_data_by_month', methods=['GET'])
@login_required
def view_data_by_month():
    month = request.args.get('month')
    year = request.args.get('year')
    userid = fetch_user_id_from_session()

    if not (userid and month and year):
        return render_template('view_data.html', message="Please select both month and year.")

    with get_session() as db_session:
        modelShop = models.get("groceries")
        records = db_session.query(modelShop).filter_by(userid=userid, month=month, year=year).all()

        if not records:
            return render_template('view_data.html', message="No records found for selected month.")

        session['billid'] = records[0].billId
        return redirect(url_for('main.view_data'))

@main_bp.route('/api/search_items', methods=['GET'])
@login_required
def search_items():
    """
    Searches the logged-in user's purchase history by item name (substring, case-insensitive)
    and returns item, price, date purchased, shop, and a link to that bill's details.
    """
    query = (request.args.get('q') or '').strip()
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({'error': 'User not authenticated or invalid session.'}), 403

    if not query:
        return jsonify({'results': []})

    modelShop = models.get("groceries")
    if not modelShop:
        logger.error("groceries model not initialized.")
        return jsonify({'error': 'groceries table is not available.'}), 500

    with get_session() as db_session:
        records = (
            db_session.query(modelShop)
            .filter(modelShop.userid == userid)
            .filter(modelShop.item.ilike(f"%{query}%"))
            .order_by(modelShop.date.desc())
            .all()
        )

        results = [
            {
                'item': record.item,
                'price': float(record.price) if record.price is not None else None,
                'date_purchased': record.date.isoformat() if hasattr(record.date, 'isoformat') else record.date,
                'shop': record.shopName,
                'billId': record.billId,
                'bill_link': url_for('main.view_data', billId=record.billId)
            }
            for record in records
        ]

        return jsonify({'results': results})

@main_bp.route('/view_invoices_by_month', methods=['GET'])
@login_required
def view_invoices_by_month():
    month = request.args.get('month')
    year = request.args.get('year')

    # NEW -Sorting
    sort_by = request.args.get('sort_by', 'date_desc')
    userid = fetch_user_id_from_session()
    bill_id=""

    if not (userid and month and year):
        return render_template('view_data.html', message="Please select both month and year.")

    modelShop = models.get("groceries")
    modelSplitDetail=models.get("DebtSettlementsBills")
    with get_session() as db_session:
        records = db_session.query(modelShop).filter_by(userid=userid, month=month, year=year).all()

        if not records:
            return render_template('view_data.html', message="No records found for selected month.")
        # Group records by billId
        bill_map = {}
        for record in records:
            bill_id = record.billId
            # Add .pdf extension only if missing
            if not bill_id.endswith('.pdf'):
                filename_pdf = bill_id + '.pdf'
            else:
                filename_pdf = bill_id

            PaidByUser = aliased(SplitInvoiceUser)

            result = db_session.query(
                PaidByUser.email,
                PaidByUser.name
            ).join(
                DebtSettlementsBills, PaidByUser.email == DebtSettlementsBills.paid_by_email
            ).filter(
                DebtSettlementsBills.billId == bill_id
            ).distinct().all()

            if not result:
                paid_by = "Bill is not splitted yet"
            else:
                # If you just need the first payer
                paid_by = result[0].name

            pdf_url = url_for('static', filename=f"uploads/{record.year}/{record.month}/{filename_pdf}")

            bill_map[record.billId] = {
                'billId': record.billId,
                'shop': record.shopName,
                'date': record.date,
                'total': record.totalSum,
                'pdf_url': pdf_url,
                'paid_by': paid_by
            }
        bills = list(bill_map.values())

        # SORTING
        sort_by = request.args.get("sort_by", "date_desc")

        if sort_by == "date_asc":
            bills.sort(key=lambda x: x["date"] or "")

        elif sort_by == "date_desc":
            bills.sort(key=lambda x: x["date"] or "", reverse=True)

        elif sort_by == "shop_asc":
            bills.sort(key=lambda x: (x["shop"] or "").lower())

        elif sort_by == "price_asc":
            bills.sort(key=lambda x: float(x["total"] or 0))

        elif sort_by == "price_desc":
            bills.sort(key=lambda x: float(x["total"] or 0), reverse=True)

        return render_template('view_invoices_month.html', bills=bills, month=month, year=year,selected_sort=sort_by)

#def se@main_bp.route('/static/uploads/<path:filename>')
#rve_uploaded_file(filename):
   # return send_from_directory(current_app.config['UPLOAD_FOLDER'], filename)

@main_bp.route('/get_invoice_years', methods=['GET'])
@login_required
def get_invoice_years():
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({'error': 'User not authenticated'}), 403

    modelShop = models.get("groceries")
    with get_session() as db_session:
        years = db_session.query(modelShop.year).filter_by(userid=userid).distinct().all()
        years_list = [y[0] for y in years]
        return jsonify({'years': years_list})

@main_bp.route('/get_invoice_months/<int:year>', methods=['GET'])
@login_required
def get_invoice_months(year):
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({'error': 'User not authenticated'}), 403

    modelShop = models.get("groceries")
    with get_session() as db_session:
        months = db_session.query(modelShop.month).filter_by(userid=userid, year=year).distinct().all()
        months_list = [m[0] for m in months]
        return jsonify({'months': months_list})


@main_bp.route('/split_invoice', methods=['GET', 'POST'])
@login_required
def split_invoice():
    try:
        userid = fetch_user_id_from_session()
        if not userid:
            return jsonify({'error': 'User not authenticated'}), 403

        billid = session.get('billid')
        if not billid:
            logger.error("Bill ID  not found in session.")
            return jsonify({'error': 'Bill ID or User ID not found in session.'}), 400

        groceries = models.get("groceries")
        if not groceries:
            logger.error(f"{groceries} model not initialized.")
            return jsonify({'error': f'{groceries} table is not available.'}), 500

        if request.method == 'GET':

            with get_session() as db_session:

                    # Fetch the logged-in user from Users table
                    logged_in_user = db_session.query(Users).filter_by(userid=userid).first()

                    if logged_in_user:
                        logged_in_user_email = logged_in_user.username  # Convert ID to email
                        logged_in_user_name = logged_in_user.firstname
                    else:
                        logger.error("Logged-in user not found in Users table.")
                        return jsonify({'error': 'Logged-in user not found.'}), 400

                    # Ensure logged-in user is included in the list
                    split_invoice_user_for_loggedinUser = db_session.query(SplitInvoiceUser).filter_by(email=logged_in_user_email).first()

                    if not split_invoice_user_for_loggedinUser:
                        #   Insert logged-in user into SplitInvoiceUser with correct userid
                        new_split_user = SplitInvoiceUser(
                            email=logged_in_user_email,
                            name=logged_in_user_name,
                            userid=userid  #   Ensures `userid` matches Users table
                        )
                        db_session.add(new_split_user)
                        db_session.commit()
                        logger.info(f"Logged-in user {logged_in_user_email} added to SplitInvoiceUser.")

                    # Fetch all the items from the bill
                    records = db_session.query(groceries).filter_by(userid=userid, billId=billid).all()
                    if not records:
                        logger.info(f"No records found for Bill ID {billid}.")
                        return render_template('split_invoice.html', message="No records found.")

                    # Fetch all users from SplitInvoiceUser for this logged-in user EXCLUDING THE LOGGEDIN USER RECORD
                    split_invoice_users = db_session.query(SplitInvoiceUser).filter(
                        SplitInvoiceUser.userid == userid, SplitInvoiceUser.email != logged_in_user_email
                    ).all()

                    items_data = [
                        {
                        'item': record.item,
                        'quantity': record.quantity,
                        'price': record.price
                        }
                    for record in records
                    ]

                    return render_template(
                        'split_invoice.html',
                        bill_info=billid,
                        totalSum_info=records[0].totalSum if records else 0,
                        address_info=records[0].address if records else '',
                        date_info=records[0].date if records else '',
                        items_data=items_data,
                        split_invoice_users=split_invoice_users,
                        logged_in_user={'email': logged_in_user_email, 'name': logged_in_user_name}
                    )

        elif request.method == 'POST':
            # Handle form submission for splitting the bill
            try:
                userid = fetch_user_id_from_session()
                if not userid:
                    logger.error("User ID not found in session.")
                    return jsonify({'error': 'User not authenticated or invalid session.'}), 403

                logger.info("Incoming Headers: %s", request.headers)
                logger.info("Raw Data: %s", request.data)

                data = request.get_json()
                paid_by = data.get('paid_by')  # Email of the payer
                items = data.get('items', [])
                shared_with = data.get('shared_with', [])
                overwrite = bool(data.get('overwrite', False))  # Ensure boolean type

                if not paid_by or not items or not shared_with:
                    logger.error("Paid by, items, or shared_with data is missing.")
                    return jsonify({'error': 'Required fields are missing.'}), 400

                with get_session() as db_session:
                    billid = session.get('billid')
                    logged_in_user = db_session.query(Users).filter_by(userid=userid).first()
                    if logged_in_user:
                        logged_in_user_email = logged_in_user.username  # Convert ID to email
                        logged_in_user_name = logged_in_user.firstname
                        logged_in_user_id = logged_in_user.userid
                    else:
                        logger.error("Logged-in user not found in Users table.")
                        return jsonify({'error': 'Logged-in user not found.'}), 400

                    #  Ensure `paid_by` is used correctly (fetch by email, not ID)
                    payer = db_session.query(SplitInvoiceUser).filter(SplitInvoiceUser.email == paid_by).first()


                    #check if payer email id loggedin user email, if yes, then fetch the loggedin userid to payerid and
                    if payer:
                        logger.info(f"Payer found: ID={payer.id}, Email={payer.email}, Name={payer.name}")
                        if payer.email == logged_in_user_email:
                            logger.info(f"Payer is the logged-in user. Updating payer ID to match Users table.")
                            #payer.id = logged_in_user_id #  Ensure payer.id matches logged-in user ID
                            paid_by_user_id = logged_in_user_id
                            paid_by_email = payer.email
                        else:
                             # Ensure payer.id matches logged-in user email
                            paid_by_email = payer.email
                            paid_by_user_id = payer.id
                    else:
                            logger.error(f"Payer with email {paid_by} not found.")
                            return jsonify({'error': 'Payer not found.'}), 400

                    #  Use `payer.email` in further processing (not `payer.id`)
                    final_paid_by = payer.email

                    #  Check if bill is already split
                    #existing_splits = db_session.query(SplitInvoiceDetail).filter_by(billId=billid).count()
                    existing_splits = db_session.query(SplitInvoiceDetail).filter_by(billId=billid,
                                                                                     userid=userid).count()

                    if existing_splits > 0 and not overwrite:
                        logger.warning(f"Bill {billid} is already split.")
                        return jsonify({'warning': 'Bill is already split. Overwrite?', 'split_exists': True}), 200

                    #  If overwrite is confirmed, remove existing splits
                    if overwrite:
                        #db_session.query(SplitInvoiceDetail).filter_by(billId=billid).delete()
                        db_session.query(SplitInvoiceDetail).filter_by(billId=billid, userid=userid).delete()

                        db_session.commit()
                        logger.info(f"Previous split details for Bill ID {billid}  in SplitInvoiceDetail deleted.")
                        #db_session.query(Settlements).filter_by(billId=billid).delete()
                        db_session.query(Settlements).filter_by(billId=billid, userid=userid).delete()
                        db_session.commit()
                        logger.info(f"Previous split details for Bill ID {billid}  in Settlements deleted.")
                        #db_session.query(DebtSettlementsBills).filter_by(billId=billid).delete()
                        db_session.query(DebtSettlementsBills).filter_by(billId=billid, created_by=userid).delete()
                        db_session.commit()
                        logger.info(f"Previous split details for Bill ID {billid}  in DebtSettlementsBills deleted.")
                        db_session.commit()
                        logger.info(f"Previous split details for Bill ID {billid} deleted.")

                    #   Now process items and shared users
                    groceries = models.get("groceries")
                    if not groceries:
                        logger.error("groceries model not initialized.")
                        return jsonify({'error': 'groceries table is not available.'}), 500

                    for item, users in zip(items, shared_with):
                        item_record = db_session.query(groceries).filter_by(billId=billid, userid=userid, item=item).first()
                        if not item_record:
                            logger.error(f"Item {item} not found in the database.")
                            continue

                        price = float(item_record.price)
                        num_users = len(users)
                        amount_per_user = price / num_users

                        for user_email in users:
                            shared_user = db_session.query(SplitInvoiceUser).filter_by(email=user_email).first()
                            if shared_user:
                                if shared_user.email == logged_in_user_email:
                                    logger.info(f"Owed user is logged-in user.")
                                    owed_by_id = logged_in_user_id
                                    owed_by_email = shared_user.email
                                    logger.info(f"Using User's ID {logged_in_user_id} from Users table.")
                                else:
                                    owed_by_id = shared_user.id
                                    owed_by_email = shared_user.email
                            else:
                                logger.error(f"User with email {user_email} not found.")
                                continue

                            #  Store `email` instead of `id`
                            try:
                                new_split_detail = SplitInvoiceDetail(
                                    billId=billid,
                                    item=item,
                                    shared_with=user_email,
                                    amount=amount_per_user,
                                    userid=userid
                                )
                                db_session.add(new_split_detail)
                            except Exception as e:
                                logger.error(f"Error during new_split_detail: {str(e)}")
                                return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500
                            try:
                                logger.info(f"Inside new_settlement:")
                                new_settlement = Settlements(
                                    billId=billid,
                                    paid_by=paid_by_user_id,
                                    owed_by=owed_by_id,
                                    paid_by_email=paid_by_email,
                                    owed_by_email=owed_by_email,
                                    amount=amount_per_user,
                                    settled=False,
                                    userid=userid
                                )

                                db_session.add(new_settlement)
                                logger.info(f"successfully added new_settlement")
                            except Exception as e:
                                logger.error(f"Error during new_settlement: {str(e)}")
                                return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500
                    db_session.commit()
                    #   Final debt calculations
                    calculate_debts_basedON_billId(billid, db_session, current_user_id=userid)
                    calculate_overall_debts_and_insert(db_session, current_user_id=userid)


                    return jsonify({'success': True}), 200
            except Exception as e:
                logger.error(f"Error during split invoice: {str(e)}")
                return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500


    except Exception as e:
        logger.error(f"Error during split invoice: {str(e)}")
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500



@main_bp.route('/add_split_invoice_user', methods=['POST'])
@login_required
def add_split_invoice_user():
    """
    Adds a new user to the SplitInvoiceUser table.
    """
    try:
        userid = fetch_user_id_from_session()
        if not userid:
            logger.error("User ID not found in session.")
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        email = request.form.get('email')
        name = request.form.get('name')

        if not email or not name:
            logger.error("Email and name are required.")
            return jsonify({'error': 'Email and name are required.'}), 400

        with get_session() as db_session:
            # Check if the user already exists
            existing_user = db_session.query(SplitInvoiceUser).filter_by(email=email).first()
            if existing_user:
                logger.info(f"User with email {email} already exists.")
                return jsonify({'error': 'User already exists.'}), 400

            # Add new user
            new_user = SplitInvoiceUser(email=email, name=name, userid=userid)
            db_session.add(new_user)
            db_session.commit()

            logger.info(f"New user added: {email}")
            return jsonify({'success': True, 'message': 'User added successfully.'})

    except Exception as e:
        logger.error(f"Error adding user: {str(e)}")
        db_session.rollback()  # Rollback in case of error
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500



@main_bp.route('/add_split_invoice_user', methods=['POST'])
@login_required
def add_split_invoice_user_new():
    """
    Adds a new user to the SplitInvoiceUser table.
    """
    try:
        userid = fetch_user_id_from_session()
        if not userid:
            logger.error("User ID not found in session.")
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        email = request.form.get('email')
        name = request.form.get('name')

        if not email or not name:
            logger.error("Email and name are required.")
            return jsonify({'error': 'Email and name are required.'}), 400

        with get_session() as db_session:
            # Check if the user already exists
            existing_user = db_session.query(SplitInvoiceUser).filter_by(email=email).first()
            if existing_user:
                logger.info(f"User with email {email} already exists.")
                return jsonify({'error': 'User already exists.'}), 400

            # Add new user
            new_user = SplitInvoiceUser(email=email, name=name, userid=userid)
            db_session.add(new_user)
            db_session.commit()

            logger.info(f"New user added: {email}")
            return jsonify({'success': True, 'message': 'User added successfully.'})

    except Exception as e:
        logger.error(f"Error adding user: {str(e)}")
        db_session.rollback()  # Rollback in case of error
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500
'''
@main_bp.route('/balances', methods=['GET'])
@login_required
def view_balances():
    loggedin_user_email = session.get('username')
    if not loggedin_user_email:
        return jsonify({'error': 'User not authenticated or invalid session.'}), 403

    with get_session() as db_session:

        update_debt_summary(db_session)
        #   Total money owed to you (others owe you)
        total_money_owed = db_session.query(
            func.coalesce(func.sum(DebtSummary.total_amount), 0)
        ).filter(
            DebtSummary.paid_by_email == loggedin_user_email,
            DebtSummary.owed_by_email != loggedin_user_email,
            DebtSummary.settled == False
        ).scalar()

        #   Total money you owe (you owe others)
        total_money_borrowed = db_session.query(
            func.coalesce(func.sum(DebtSummary.total_amount), 0)
        ).filter(
            DebtSummary.owed_by_email == loggedin_user_email,
            DebtSummary.paid_by_email != loggedin_user_email,
            DebtSummary.settled == False
        ).scalar()

        #  Breakdown of who you owe
        owed_breakdown = db_session.query(
            DebtSummary.paid_by_email,
            func.sum(DebtSummary.total_amount).label('total')
        ).filter(
            DebtSummary.owed_by_email == loggedin_user_email,
            DebtSummary.paid_by_email != loggedin_user_email,
            DebtSummary.settled == False
        ).group_by(DebtSummary.paid_by_email).all()


        logger.info(f"owed_breakdown details: {owed_breakdown}")


        #   Breakdown of who owes you
        borrowed_breakdown = db_session.query(
            DebtSummary.owed_by_email,
            func.sum(DebtSummary.total_amount).label('total')
        ).filter(
            DebtSummary.paid_by_email == loggedin_user_email,
            DebtSummary.owed_by_email != loggedin_user_email,
            DebtSummary.settled == False
        ).group_by(DebtSummary.owed_by_email).all()

        logger.info(f"borrowed_breakdown details: {borrowed_breakdown}")


        # Format owed details
        owed_details = []
        for paid_by_email, total in owed_breakdown:
            user = db_session.query(SplitInvoiceUser).filter_by(email=paid_by_email).first()
            owed_details.append({
                'email': paid_by_email,
                'name': user.name if user else 'Unknown',
                'amount': round(total, 2),
                'paid_by_email': paid_by_email
            })
        logger.info(f"owed details: {owed_details}")

        # Format borrowed details
        borrowed_details = []
        for owed_by_email, total in borrowed_breakdown:
            user = db_session.query(SplitInvoiceUser).filter_by(email=owed_by_email).first()
            borrowed_details.append({
                'email': owed_by_email,
                'name': user.name if user else 'Unknown',
                'amount': round(total, 2)
            })
        logger.info(f"borrowed_details details: {borrowed_details}")
    return render_template(
        'balances.html',
        total_money_owed=round(total_money_owed, 2),
        total_money_borrowed=round(total_money_borrowed, 2),
        owed_details=owed_details,
        borrowed_details=borrowed_details
    )

'''

@main_bp.route('/balances', methods=['GET'])
@login_required
def view_balances():
    """
    Show balances, optionally filtered by:
      - shop: string
      - month: 'YYYY-MM'
      - start: 'YYYY-MM'
      - end: 'YYYY-MM'
    Query params can be combined (e.g., shop + month, shop + period).
    """
    loggedin_user_email = session.get('username')
    if not loggedin_user_email:
        return jsonify({'error': 'User not authenticated or invalid session.'}), 403

    # Read filters from query params
    shop = request.args.get('shop')
    month = request.args.get('month')            # format 'YYYY-MM'
    start = request.args.get('start')            # format 'YYYY-MM'
    end = request.args.get('end')                # format 'YYYY-MM'

    with get_session() as db_session:
        # Ensure summary reflects latest bill-level settlements
        update_debt_summary(db_session)

        # If filters exist, resolve allowed billIds, otherwise None means "no filter on bills".
        userid = fetch_user_id_from_session()
        #bill_filter_ids = get_filtered_bill_ids(db_session, userid, shop, month, start, end)
        raw_ids = get_filtered_bill_ids(db_session, userid, shop, month, start, end)
        #bill_filter_ids = list(bill_filter_ids)
        bill_filter_ids = _normalize_bill_ids_to_str(raw_ids)
        current_app.logger.debug("bill_filter_ids=%r", bill_filter_ids)

        apply_bill_filter = len(bill_filter_ids) > 0

        # Base query over DebtSettlementsBills (bill-level), then roll up to DebtSummary-like view
        dsb = DebtSettlementsBills

        base_q = db_session.query(
            dsb.paid_by_email,
            dsb.owed_by_email,
            func.coalesce(func.sum(dsb.amount), 0).label('total')
        ).filter(dsb.settled == False)

        # Narrow to selected bills if we have a bill filter
        if apply_bill_filter:
            base_q = base_q.filter(
                dsb.billId.in_(bindparam('bill_ids', expanding=True, type_=String()))
            ).params(bill_ids=bill_filter_ids)

        base_q = base_q.group_by(dsb.paid_by_email, dsb.owed_by_email).subquery()

        # Compute totals relative to the logged in user
        # total others owe you (you are the payer)
        total_money_owed = db_session.query(
            func.coalesce(func.sum(base_q.c.total), 0)
        ).filter(
            base_q.c.paid_by_email == loggedin_user_email,
            base_q.c.owed_by_email != loggedin_user_email
        ).scalar()

        # total you owe others (you are the ower)
        total_money_borrowed = db_session.query(
            func.coalesce(func.sum(base_q.c.total), 0)
        ).filter(
            base_q.c.owed_by_email == loggedin_user_email,
            base_q.c.paid_by_email != loggedin_user_email
        ).scalar()

        # Breakdown: who you owe
        owed_breakdown_rows = db_session.query(
            base_q.c.paid_by_email,
            base_q.c.total
        ).filter(
            base_q.c.owed_by_email == loggedin_user_email,
            base_q.c.paid_by_email != loggedin_user_email
        ).all()

        # Breakdown: who owes you
        borrowed_breakdown_rows = db_session.query(
            base_q.c.owed_by_email,
            base_q.c.total
        ).filter(
            base_q.c.paid_by_email == loggedin_user_email,
            base_q.c.owed_by_email != loggedin_user_email
        ).all()

        # Map to names via SplitInvoiceUser (as you do today)
        owed_details = []
        borrowed_details = []

        for paid_by_email, total in owed_breakdown_rows:
            user = db_session.query(SplitInvoiceUser).filter_by(email=paid_by_email).first()
            owed_details.append({
                'email': paid_by_email,
                'name': user.name if user else 'Unknown',
                'amount': round(float(total), 2),
                'paid_by_email': paid_by_email
            })

        for owed_by_email, total in borrowed_breakdown_rows:
            user = db_session.query(SplitInvoiceUser).filter_by(email=owed_by_email).first()
            borrowed_details.append({
                'email': owed_by_email,
                'name': user.name if user else 'Unknown',
                'amount': round(float(total), 2)
            })

        # Provide the filter values back to the template so the UI keeps state
        return render_template(
            'balances.html',
            total_money_owed=round(float(total_money_owed), 2),
            total_money_borrowed=round(float(total_money_borrowed), 2),
            owed_details=owed_details,
            borrowed_details=borrowed_details,
            current_filters={'shop': shop or '', 'month': month or '', 'start': start or '', 'end': end or ''}
        )

@main_bp.route('/reverse_settlement', methods=['POST'])
@login_required
def reverse_settlement():
    data = request.get_json()
    paid_by = data.get('paid_by')
    owed_by = data.get('owed_by')

    if not paid_by or not owed_by:
        return jsonify({'error': 'Missing required fields'}), 400

    with get_session() as db_session:
        summaries = db_session.query(DebtSummary).filter_by(
            paid_by_email=paid_by,
            owed_by_email=owed_by
        ).all()

        if not summaries:
            return jsonify({'error': 'No settlements found to reverse.'}), 404

        for summary in summaries:
            if summary.settled:
                # restore the total amount from original
                summary.total_amount = summary.original_amount
                summary.settled = False
                summary.settled_amount = 0

        # Also restore child records
        db_session.query(DebtSettlementsBills).filter_by(
            paid_by_email=paid_by, owed_by_email=owed_by
        ).update({"settled": False}, synchronize_session='fetch')

        db_session.query(DebtSettlements).filter_by(
            paid_by_email=paid_by, owed_by_email=owed_by
        ).update({"settled": False}, synchronize_session='fetch')

        db_session.query(Settlements).filter_by(
            paid_by_email=paid_by, owed_by_email=owed_by
        ).update({"settled": False}, synchronize_session='fetch')

        db_session.commit()
        return jsonify({'success': True, 'message': 'Settlement reversed successfully.'})
@main_bp.route("/analytics", methods=["GET"])
@login_required
def analytics_dashboard():
    return render_template("analytics.html")
from datetime import date
from sqlalchemy import func
# you already import func, datetime etc. in this file

@main_bp.route("/api/analytics/overview", methods=["GET"])
@login_required
def analytics_overview():
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "not authenticated"}), 403

    modelShop = models.get("groceries")

    with get_session() as db_session:
        # total spent (sum of all item prices)
        total_spent = db_session.query(
            func.coalesce(func.sum(modelShop.price), 0)
        ).filter_by(userid=userid).scalar()

        today = date.today()
        this_year = today.year
        this_month = today.strftime("%B").lower()  # matches how you store month

        this_month_spent = db_session.query(
            func.coalesce(func.sum(modelShop.price), 0)
        ).filter_by(userid=userid, year=this_year, month=this_month).scalar()

        bill_count = db_session.query(
            func.count(func.distinct(modelShop.billId))
        ).filter_by(userid=userid).scalar()

        last_bill_date = db_session.query(
            func.max(modelShop.date)
        ).filter_by(userid=userid).scalar()

        # top shop
        shop_totals = db_session.query(
            modelShop.shopName,
            func.sum(modelShop.price).label("total")
        ).filter_by(userid=userid).group_by(modelShop.shopName).order_by(
            func.sum(modelShop.price).desc()
        ).all()

        top_shop = shop_totals[0][0] if shop_totals else None
        top_shop_amount = float(shop_totals[0][1]) if shop_totals else 0.0

        # simple label strings for UI
        last_bill_label = f"Last: {last_bill_date}" if last_bill_date else ""
        top_shop_label = f"€{top_shop_amount:.2f} total" if top_shop else ""

        return jsonify({
            "total_spent": float(total_spent),
            "this_month_spent": float(this_month_spent),
            "bill_count": int(bill_count),
            "last_bill_label": last_bill_label,
            "top_shop": top_shop,
            "top_shop_label": top_shop_label,
            "month_change_label": "",         # you can compute later if you want
            "this_month_bills_label": ""      # e.g. "5 bills this month"
        })
@main_bp.route("/api/analytics/spending_by_users", methods=["GET"])
@login_required
def analytics_spending_by_users():
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "not authenticated"}), 403

    with get_session() as db_session:
        # We use Settlements to see for each paid_by_email, how much they covered.
        settlements = models.get("settlements") if hasattr(models, "get") else Settlements  # adjust if needed
        # You already imported Settlements at top of file

        rows = db_session.query(
            Settlements.paid_by_email,
            func.sum(Settlements.amount).label("total")
        ).filter_by(userid=userid).group_by(Settlements.paid_by_email).all()

        result = []
        for email, total in rows:
            user = db_session.query(SplitInvoiceUser).filter_by(email=email).first()
            result.append({
                "email": email,
                "name": user.name if user else email,
                "total": float(total or 0)
            })

        return jsonify(result)
@main_bp.route("/api/analytics/overall", methods=["GET"])
@login_required
def analytics_overall():
    import calendar as _cal
    _month_order = {m.lower(): i for i, m in enumerate(_cal.month_name) if m}

    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "not authenticated"}), 403

    modelShop = models.get("groceries")

    year_filter = request.args.get("year")
    shop_filter = request.args.get("shop")
    month_filter = request.args.get("month")

    with get_session() as db_session:
        base_q = db_session.query(modelShop).filter(modelShop.userid == userid)
        if year_filter:
            base_q = base_q.filter(modelShop.year == int(year_filter))
        if shop_filter:
            base_q = base_q.filter(modelShop.shopName == shop_filter)
        if month_filter:
            base_q = base_q.filter(modelShop.month == month_filter.lower())

        # Monthly totals — sort in Python so month strings are ordered by calendar position
        monthly_rows = db_session.query(
            modelShop.year,
            modelShop.month,
            func.sum(modelShop.price).label("total")
        ).filter(modelShop.userid == userid,
                 *([modelShop.year == int(year_filter)] if year_filter else []),
                 *([modelShop.shopName == shop_filter] if shop_filter else []),
                 *([modelShop.month == month_filter.lower()] if month_filter else [])
        ).group_by(modelShop.year, modelShop.month).all()

        monthly_rows = sorted(
            monthly_rows,
            key=lambda r: (r[0], _month_order.get((r[1] or "").lower(), 0))
        )

        monthly_labels = [f"{year}-{month.capitalize()}" for (year, month, _) in monthly_rows]
        monthly_values = [float(total or 0) for (_, _, total) in monthly_rows]

        # Shop-wise totals
        shop_rows = db_session.query(
            modelShop.shopName,
            func.sum(modelShop.price).label("total")
        ).filter(modelShop.userid == userid,
                 *([modelShop.year == int(year_filter)] if year_filter else []),
                 *([modelShop.month == month_filter.lower()] if month_filter else [])
        ).group_by(modelShop.shopName).all()

        shop_labels = [name for (name, _) in shop_rows]
        shop_values = [float(total or 0) for (_, total) in shop_rows]

        # Available years and shops for filter dropdowns
        year_rows = db_session.query(modelShop.year).filter(
            modelShop.userid == userid
        ).distinct().order_by(modelShop.year).all()
        available_years = [r[0] for r in year_rows]

        shop_name_rows = db_session.query(modelShop.shopName).filter(
            modelShop.userid == userid
        ).distinct().order_by(modelShop.shopName).all()
        available_shops = [r[0] for r in shop_name_rows if r[0]]

        return jsonify({
            "monthly": {
                "labels": monthly_labels,
                "values": monthly_values
            },
            "shopwise": {
                "labels": shop_labels,
                "values": shop_values
            },
            "available_years": available_years,
            "available_shops": available_shops,
        })
# ==========================
#  ANALYTICS DASHBOARD
# ==========================

from flask_login import login_required
from sqlalchemy import func
from sqlalchemy.orm import aliased
from datetime import datetime
from flask import session, jsonify, render_template
from decimal import Decimal

# Helper: get current user id
# (you already have fetch_user_id_from_session defined above)


@main_bp.route("/api/analytics/overview")
@login_required
def api_analytics_overview():
    """
    High-level overview numbers:
      - total_spent (all time)
      - this_month_spent
      - bill_count
      - top_shop
    Based on groceries table, filtered by logged-in user's userid.
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    with get_session() as db_session:
        # Total spent (sum of all prices)
        total_spent = db_session.query(
            func.coalesce(func.sum(modelShop.price), 0)
        ).filter(
            modelShop.userid == userid
        ).scalar() or 0

        # This month
        now = datetime.now()
        current_year = str(now.year)
        current_month_name = now.strftime("%B").lower()  # e.g. "november"

        this_month_spent = db_session.query(
            func.coalesce(func.sum(modelShop.price), 0)
        ).filter(
            modelShop.userid == userid,
            modelShop.year == current_year,
            modelShop.month == current_month_name
        ).scalar() or 0

        # Distinct bill count
        bill_count = db_session.query(
            func.count(func.distinct(modelShop.billId))
        ).filter(
            modelShop.userid == userid
        ).scalar() or 0

        # Top shop by spending
        top_shop_row = db_session.query(
            modelShop.shopName,
            func.coalesce(func.sum(modelShop.price), 0).label("total")
        ).filter(
            modelShop.userid == userid
        ).group_by(
            modelShop.shopName
        ).order_by(
            func.sum(modelShop.price).desc()
        ).first()

        top_shop = top_shop_row.shopName if top_shop_row else None

        return jsonify({
            "total_spent": float(total_spent),
            "this_month_spent": float(this_month_spent),
            "bill_count": int(bill_count),
            "top_shop": top_shop
        })
@main_bp.route("/api/analytics/frequent_items")
@login_required
def api_analytics_frequent_items():
    """
    Frequently bought items, with optional year/month/shop filters.
    Query params: ?year=2026&month=july&shop=REWE
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    year_filter = request.args.get("year")
    shop_filter = request.args.get("shop")
    month_filter = request.args.get("month")

    with get_session() as db_session:
        q = db_session.query(
            modelShop.item,
            func.count().label("count"),
            func.coalesce(func.sum(modelShop.price), 0).label("total_cost")
        ).filter(
            modelShop.userid == userid,
            ~modelShop.item.ilike("%pfand%"),
            ~modelShop.item.ilike("%banderole%"),
            ~modelShop.item.ilike("%pfandartikel%"),
        )

        if year_filter:
            q = q.filter(modelShop.year == int(year_filter))
        if shop_filter:
            q = q.filter(modelShop.shopName == shop_filter)
        if month_filter:
            q = q.filter(modelShop.month == month_filter.lower())

        rows = q.group_by(modelShop.item).order_by(func.count().desc()).all()

        result = [
            {"item": item, "count": int(count or 0), "total_cost": float(total_cost or 0)}
            for item, count, total_cost in rows
        ]
        return jsonify(result)


@main_bp.route("/api/analytics/spending_by_users")
@login_required
def api_analytics_spending_by_users():
    """
    Spending (covered) by users, broken down by shop.
    Uses DebtSettlementsBills (amount / paid_by_email) joined to groceries (shopName)
    and SplitInvoiceUser (for friendly names).

    Returns list of rows:
      {
        "email": "user@example.com",
        "name": "User Name",
        "shopName": "rewe",
        "total": 123.45
      }
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    with get_session() as db_session:
        PaidByUser = aliased(SplitInvoiceUser)

        rows = db_session.query(
            DebtSettlementsBills.paid_by_email,
            PaidByUser.name.label("paid_by_name"),
            modelShop.shopName,
            func.coalesce(func.sum(DebtSettlementsBills.amount), 0).label("total")
        ).join(
            PaidByUser, PaidByUser.email == DebtSettlementsBills.paid_by_email, isouter=True
        ).join(
            modelShop, modelShop.billId == DebtSettlementsBills.billId, isouter=True
        ).filter(
            DebtSettlementsBills.created_by == userid  # only bills created by this user
        ).group_by(
            DebtSettlementsBills.paid_by_email,
            PaidByUser.name,
            modelShop.shopName
        ).all()

        result = []
        for email, name, shopName, total in rows:
            result.append({
                "email": email,
                "name": name,
                "shopName": shopName,
                "total": float(total or 0)
            })

        return jsonify(result)


@main_bp.route('/api/planner/items', methods=['GET'])
@login_required
def list_planner_items():
    """
    List the logged-in user's shopping-list items.
    Query params: ?include_bought=1 to include already-bought items (default: only unbought).
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    include_bought = request.args.get("include_bought") == "1"

    with get_session() as db_session:
        q = db_session.query(ShoppingListItem).filter(ShoppingListItem.userid == userid)
        if not include_bought:
            q = q.filter(ShoppingListItem.bought == False)  # noqa: E712

        rows = q.order_by(ShoppingListItem.created_at).all()

        result = [
            {
                "id": row.id,
                "item": row.item,
                "quantity": row.quantity,
                "bought": bool(row.bought),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
        return jsonify(result)


@main_bp.route('/api/planner/items', methods=['POST'])
@login_required
def add_planner_item():
    """
    Add an item to the shopping list. Body: {"item": "...", "quantity": 2}
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    payload = request.get_json(silent=True) or {}
    item_name = collapse_whitespace(str(payload.get("item") or ""))
    if not item_name:
        return jsonify({"error": "Item name is required."}), 400
    if not sanitize_input(item_name):
        return jsonify({"error": "Item name contains invalid characters."}), 400

    quantity_raw = payload.get("quantity")
    quantity_value = float_from_cell(quantity_raw) if quantity_raw not in (None, "") else None

    with get_session() as db_session:
        new_item = ShoppingListItem(
            userid=int(userid),
            item=item_name,
            quantity=quantity_value,
            bought=False,
        )
        db_session.add(new_item)
        db_session.commit()

        return jsonify({
            "id": new_item.id,
            "item": new_item.item,
            "quantity": new_item.quantity,
            "bought": False,
        }), 201


@main_bp.route('/api/planner/items/<int:item_id>/toggle_bought', methods=['POST'])
@login_required
def toggle_planner_item(item_id):
    """Flip an item's bought/unbought state."""
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    with get_session() as db_session:
        item = db_session.query(ShoppingListItem).filter_by(id=item_id, userid=userid).first()
        if not item:
            return jsonify({"error": "Item not found."}), 404

        item.bought = not item.bought
        item.bought_at = datetime.utcnow() if item.bought else None
        db_session.commit()

        return jsonify({"id": item.id, "bought": bool(item.bought)})


@main_bp.route('/api/planner/items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_planner_item(item_id):
    """Remove an item from the shopping list entirely."""
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    with get_session() as db_session:
        item = db_session.query(ShoppingListItem).filter_by(id=item_id, userid=userid).first()
        if not item:
            return jsonify({"error": "Item not found."}), 404

        db_session.delete(item)
        db_session.commit()

        return jsonify({"success": True})


@main_bp.route('/settlement_history', methods=['GET'])
@login_required
def view_settlement_history():
    user_email = session.get('username')
    with get_session() as db_session:
        history = db_session.query(SettlementLog).filter(
            (SettlementLog.paid_by_email == user_email) | (SettlementLog.owed_by_email == user_email)
        ).order_by(SettlementLog.timestamp.desc()).all()

    return render_template("settlement_history.html", history=history)

@main_bp.route('/settle_debt', methods=['POST'])
@login_required
def settle_debt():
    data = request.get_json()
    paid_by = data.get('paid_by')
    owed_by = data.get('owed_by')
    amount_paid = data.get('amount_paid')
    full = data.get('full_settlement')
    method = data.get('method')
    note = data.get('note')

    # Sanity check
    if not paid_by or not owed_by:
        return jsonify({'error': 'Missing required fields'}), 400

    # Convert to Decimal for safe math
    if amount_paid is not None:
        try:
            amount_paid = Decimal(str(amount_paid))
            if amount_paid <= 0:
                return jsonify({'error': 'Invalid amount'}), 400
        except:
            return jsonify({'error': 'Amount must be a valid number'}), 400

    full = bool(full) if full is not None else False

    with get_session() as db_session:
        settlement = db_session.query(DebtSummary).filter_by(
            paid_by_email=paid_by,
            owed_by_email=owed_by,
            settled=False
        ).first()

        if not settlement:
            return jsonify({'error': 'No outstanding debt found.'}), 404

        remaining = settlement.total_amount
        print (' remaining amount: ', remaining)
        # Prevent overpayment
        if not full and amount_paid > remaining:
            return jsonify({'error': 'Payment exceeds remaining debt.'}), 400

        # Determine actual amount to apply
        actual_payment = remaining if full or amount_paid is None else amount_paid
        try:
            # Update DebtSummary
            settlement.settled_amount += actual_payment
            settlement.total_amount -= actual_payment
            settlement.total_amount = max(settlement.total_amount, Decimal("0.00"))  # avoid negative

            if settlement.total_amount == 0:
                settlement.settled = True
                settlement.last_settled_on = datetime.now(timezone.utc)
            else:
                settlement.num_partial_settlements = (settlement.num_partial_settlements or 0) + 1
            db_session.flush()
            print("DebtSummary update flushed to database")
            print(f"Before SettlementLog creation - paid_by: {paid_by}, owed_by: {owed_by}, amount: {actual_payment}")
            # Log in SettlementLog
            log_entry = SettlementLog(
                paid_by_email=paid_by,
                owed_by_email=owed_by,
                amount_paid=actual_payment,
                method=method,
                note=note,
                timestamp=datetime.now(timezone.utc)
            )
            print(f"Inserting into SettlementLog: {paid_by} → {owed_by}, {actual_payment}")
            db_session.add(log_entry)
            mark_related_debts_as_settled(db_session, paid_by, owed_by)
            print(f"Entry added to session - now committing")
            db_session.commit()
            print(f"Commit successful")
        except Exception as e:
            db_session.rollback()
            print(" Commit failed:", e)
            return jsonify({'error': 'Database error during commit.'}), 500

    return jsonify({'success': True})

@main_bp.route("/export_invoice_flat", methods=["GET"])
@login_required
def export_invoice_flat():
    """
    /export_invoice_flat?billId=<BILL_ID>&format=csv|excel|json

    Produces ONE flat table with these columns:
    row_type in {metadata, item, settlement},
    billId, invoiceNumber, shopName, address, date, month, year, totalSum,
    item, quantity, price, shared_with,
    paid_by_name, owed_by_name, amount

    Only available when the bill has a split.
    CSV is the default (best for Tableau/Power BI).
    """
    try:
        # ---- inputs / session
        userid = fetch_user_id_from_session()
        if not userid:
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        billid = request.args.get('billId') or session.get('billid')
        if not billid:
            return jsonify({'error': 'Bill ID not found in request/session.'}), 400

        fmt = (request.args.get("format") or "csv").lower()

        # ---- models
        modelShop = models.get("groceries")
        if not modelShop:
            return jsonify({'error': 'groceries table is not available.'}), 500

        with get_session() as db_session:
            # Base invoice records (items)
            records = (
                db_session.query(modelShop)
                .filter_by(userid=userid, billId=billid)
                .all()
            )
            if not records:
                return jsonify({'error': f'No records found for bill {billid}.'}), 404

            # Is the bill split? (gate the export)
            split_details = db_session.query(SplitInvoiceDetail).filter_by(
                billId=billid, userid=userid
            ).all()
            if not split_details:
                return jsonify({'error': 'Export available only for split invoices.'}), 400

            # Map email -> name for shared_with
            split_invoice_users = db_session.query(SplitInvoiceUser).filter_by(userid=userid).all()
            email_to_name = {u.email: u.name for u in split_invoice_users}

            # Build shared_with per item
            shared_map = {}
            for d in split_details:
                display = email_to_name.get(d.shared_with, d.shared_with)
                shared_map.setdefault(d.item, []).append(display)

            # Debt settlement rows (Paid by/Owed by/Amount)
            debt_settlement_details = fetch_debt_settlement_details(billid, userid) or []

            # Metadata from first record
            r0 = records[0]
            meta = {
                "billId": r0.billId,
                "invoiceNumber": r0.invoiceNumber,
                "shopName": r0.shopName,
                "address": r0.address,
                "date": r0.date,
                "month": r0.month,
                "year": r0.year,
                "totalSum": r0.totalSum,
            }

            # Rows: metadata (1), item (*), settlement (*)
            rows = []

            # 1) metadata row
            rows.append({
                "row_type": "metadata",
                **meta,
                "item": None, "quantity": None, "price": None, "shared_with": None,
                "paid_by_name": None, "owed_by_name": None, "amount": None,
            })

            # 2) item rows
            for rec in records:
                rows.append({
                    "row_type": "item",
                    **meta,
                    "item": rec.item,
                    "quantity": rec.quantity,
                    "price": rec.price,
                    "shared_with": ", ".join(shared_map.get(rec.item, [])) if shared_map.get(rec.item) else None,
                    "paid_by_name": None, "owed_by_name": None, "amount": None,
                })

            # 3) settlement rows (be robust to Row / dict)
            for s in debt_settlement_details:
                if hasattr(s, "_mapping"):      # SQLAlchemy Row
                    sd = dict(s._mapping)
                elif hasattr(s, "_asdict"):     # namedtuple-ish
                    sd = s._asdict()
                elif isinstance(s, dict):       # already a dict
                    sd = s
                else:
                    # last resort: attribute access
                    sd = {
                        "paid_by_name": getattr(s, "paid_by_name", None),
                        "owed_by_name": getattr(s, "owed_by_name", None),
                        "amount": getattr(s, "amount", None),
                    }

                rows.append({
                    "row_type": "settlement",
                    **meta,
                    "item": None, "quantity": None, "price": None, "shared_with": None,
                    "paid_by_name": sd.get("paid_by_name"),
                    "owed_by_name": sd.get("owed_by_name"),
                    "amount": sd.get("amount"),
                })

            # Build DataFrame
            df = pd.DataFrame(rows)
            col_order = [
                "row_type",
                "billId","invoiceNumber","shopName","address","date","month","year","totalSum",
                "item","quantity","price","shared_with",
                "paid_by_name","owed_by_name","amount"
            ]
            df = df.reindex(columns=col_order)

            # ---- BI-friendly coercions
            from decimal import Decimal
            for col in ["price", "amount", "totalSum", "quantity"]:
                if col in df.columns:
                    df[col] = df[col].apply(lambda v: float(v) if isinstance(v, Decimal) else v)

            if "date" in df.columns:
                df["date"] = df["date"].astype(str)

            if "shared_with" in df.columns:
                df["shared_with"] = df["shared_with"].replace({"": None})

            # ---- Output
            if fmt == "json":
                return jsonify({"data": df.to_dict(orient="records")})

            if fmt == "excel":
                buf = BytesIO()
                with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
                    df.to_excel(writer, index=False, sheet_name="Invoice")
                    ws = writer.sheets["Invoice"]
                    ws.set_column(0, len(df.columns)-1, 18)
                buf.seek(0)
                filename = f"{billid}_flat_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.xlsx"
                return send_file(buf, as_attachment=True, download_name=filename,
                                 mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            # default CSV (best for Tableau/Power BI)
            csv_bytes = df.to_csv(index=False).encode("utf-8")
            filename = f"{billid}_flat_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
            return Response(csv_bytes, mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename={filename}"})

    except Exception as e:
        logger.exception("Export failed")
        return jsonify({'error': 'An internal error occurred.', 'details': str(e)}), 500

SHOP_OPTIONS = [
    ("rewe", "Rewe"),
    ("kaufland", "Kaufland"),
    ("rossmann", "Rossmann"),
    ("woolworth", "Woolworth"),
    ("dm", "DM"),
    ("edeka", "Edeka"),
    ("lidl", "Lidl"),
    ("penny", "Penny"),
    ("euroshop","EuroShop","Euroshop")
]

@main_bp.route('/expenditures', methods=['GET'])
@login_required
def expenditures():
    user_email = session.get('username')
    userid = fetch_user_id_from_session()
    if not user_email or not userid:
        return jsonify({'error': 'User not authenticated or invalid session.'}), 403

    shop  = request.args.get('shop') or None
    month = request.args.get('month') or None      # 'YYYY-MM' (e.g., 2025-03)
    start = request.args.get('start') or None      # 'YYYY-MM'
    end   = request.args.get('end') or None        # 'YYYY-MM'

    with get_session() as db_session:
        bill_ids, filter_requested = get_filtered_bill_ids(db_session, userid, shop, month, start, end)

        # If the user asked for a filter but nothing matched → return empty results.
        if filter_requested and not bill_ids:
            return render_template(
                "expenditures.html",
                filters={"shop": shop or "", "month": month or "", "start": start or "", "end": end or ""},
                who_owes=[],
                bills=[],
                shop_options=SHOP_OPTIONS
            )

        dsb = DebtSettlementsBills

        # “Who owes who”
        pairs_q = db_session.query(
            dsb.paid_by_email,
            dsb.owed_by_email,
            func.coalesce(func.sum(dsb.amount), 0).label("total")
        ).filter(dsb.settled == False)

        if bill_ids:
            pairs_q = pairs_q.filter(dsb.billId.in_(bill_ids))

        pairs = pairs_q.group_by(dsb.paid_by_email, dsb.owed_by_email).all()

        email_to_name = {u.email: u.name for u in db_session.query(SplitInvoiceUser).filter_by(userid=userid).all()}

        # Show "who owes who" as: OWER → PAYER (matches the label)
        who_owes = [
            {
                "from_email": o,  # ower
                "from_name": email_to_name.get(o, o),
                "to_email": p,  # payer
                "to_name": email_to_name.get(p, p),
                "amount": round(float(t), 2)
            }
            for p, o, t in pairs
            if p != o and float(t) > 0
        ]

        # Receipts table
        groceries = models.get("groceries")
        base_g = db_session.query(
            groceries.billId,
            func.max(groceries.shopName).label("shop"),
            func.max(groceries.address).label("address"),
            func.max(groceries.totalSum).label("totalSum"),
            func.max(groceries.date).label("date")
        ).filter(groceries.userid == userid)

        if bill_ids:
            base_g = base_g.filter(groceries.billId.in_(bill_ids))

        bills = base_g.group_by(groceries.billId).order_by(func.max(groceries.date).desc()).all()

        rows = []
        for bill in bills:
            split_rows = db_session.query(
                dsb.owed_by_email,
                func.coalesce(func.sum(dsb.amount), 0).label("amt"),
                func.max(dsb.paid_by_email).label("payer")
            ).filter(
                dsb.billId == bill.billId,
                dsb.created_by == userid
            ).group_by(dsb.owed_by_email).all()

            per_person = []
            payer_email = None
            for owed_email, amt, payer in split_rows:
                payer_email = payer or payer_email
                per_person.append(f"{email_to_name.get(owed_email, owed_email)}: {round(float(amt), 2)}")

            rows.append({
                "billId": bill.billId,
                "shop": bill.shop,
                "address": bill.address,
                "total": round(float(bill.totalSum or 0), 2),
                "split_str": " | ".join(per_person) if per_person else "—",
                "paid_by": email_to_name.get(payer_email, payer_email) if payer_email else "—",
                "date": bill.date
            })

        return render_template(
            "expenditures.html",
            filters={"shop": shop or "", "month": month or "", "start": start or "", "end": end or ""},
            who_owes=who_owes,
            bills=rows,
            shop_options=SHOP_OPTIONS
        )

@main_bp.route('/upload_sheet_invoice', methods=['POST'])
@login_required
def upload_sheet_invoice():
    """
    Upload a CSV/XLSX/XLS/TSV of manual invoices and create groceries entries.

    Expected columns (case-insensitive; header aliases tolerated):
      - shop
      - date / datum
      - no / nr / no. / nummer         (optional; not stored)
      - items / item
      - quantity / qty / menge
      - price (€) / price / unit_price / preis
      - price per kg / price_per_kg / priceperkg / preis/kg / preis pro kg (optional)
      - total_price / total / summe    (optional; may be merged once per bill like your screenshot)

    Behavior
      - Group rows by (normalized shop, normalized date) to form a bill.
      - Duplicate check: compare item signatures (item|qty|price|pricePerKg). If identical exists: skip.
      - billId = "{shop}_{YYYY-MM-DD}" with automatic _1, _2... suffixes if non-duplicate bills exist same day.
      - totalSum:
          * If exactly one non-empty cell in the group's Total column -> use it as the bill's grand total (merged cell).
          * If multiple non-empty totals -> treat as per-item totals and sum them.
          * If no totals -> compute sum(price * quantity) with per-row fallback to row_total when price is blank.
    """
    try:
        user_id = fetch_user_id_from_session()
        if not user_id:
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        GroceriesModel = models.get("groceries")
        if not GroceriesModel:
            return jsonify({'error': 'groceries table is not available.'}), 500

        if 'file' not in request.files or request.files['file'].filename == '':
            return jsonify({'error': 'No file provided'}), 400

        uploaded_file = request.files['file']
        if not is_allowed_sheet(uploaded_file.filename):
            return jsonify({'error': 'File type not allowed. Use CSV/XLSX/XLS/TSV.'}), 400

        # ---- Load into DataFrame with pandas ----
        file_extension = uploaded_file.filename.rsplit('.', 1)[1].lower()
        if file_extension in ('xlsx', 'xls'):
            dataframe = pd.read_excel(uploaded_file)
        elif file_extension == 'tsv':
            dataframe = pd.read_csv(uploaded_file, sep='\t')
        else:
            dataframe = pd.read_csv(uploaded_file)

        if dataframe is None or dataframe.empty:
            return jsonify({'error': 'Uploaded sheet is empty.'}), 400

        # Normalize headers: lowercase + strip
        original_headers = list(dataframe.columns)
        dataframe.columns = dataframe.columns.str.strip().str.lower()

        # Header resolver
        def pick_header(*aliases):
            for alias in aliases:
                if alias in dataframe.columns:
                    return alias
            return None

        column_shop = pick_header('shop')
        column_date = pick_header('date', 'datum')
        column_no = pick_header('no.', 'no', 'nr', 'nummer')  # optional
        column_item = pick_header('items', 'item')
        column_quantity = pick_header('quantity', 'qty', 'menge')
        column_price = pick_header('price (€)', 'price', 'unit_price', 'preis')
        column_price_per_kg = pick_header('price per kg', 'price_per_kg', 'priceperkg', 'preis/kg', 'preis pro kg')
        column_total = pick_header('total_price', 'total', 'summe')  # optional

        required_headers = [column_shop, column_date, column_item, column_quantity]
        if any(header is None for header in required_headers):
            return jsonify({
                'error': 'Missing required columns. Need at least: shop, date, items, quantity.',
                'detected_columns': original_headers
            }), 400

        # ---- Convert sheet rows to a clean list of dicts ----
        cleaned_rows = []
        for _, dataframe_row in dataframe.iterrows():
            raw_shop = str(dataframe_row[column_shop]) if pd.notna(dataframe_row[column_shop]) else ""
            raw_date = str(dataframe_row[column_date]) if pd.notna(dataframe_row[column_date]) else ""
            raw_item = str(dataframe_row[column_item]) if pd.notna(dataframe_row[column_item]) else ""
            raw_quantity = dataframe_row[column_quantity] if (column_quantity and pd.notna(dataframe_row[column_quantity])) else 0
            raw_price = dataframe_row[column_price] if (column_price and pd.notna(dataframe_row[column_price])) else 0
            raw_price_per_kg = dataframe_row[column_price_per_kg] if (column_price_per_kg and pd.notna(dataframe_row[column_price_per_kg])) else None
            raw_total = dataframe_row[column_total] if (column_total and pd.notna(dataframe_row[column_total])) else None

            # Skip rows without an item name
            if not collapse_whitespace(raw_item):
                continue

            try:
                date_iso = normalize_date_string(raw_date)
            except ValueError:
                return jsonify({'error': f'Bad date value: {raw_date}'}), 400

            normalized_shop = normalize_shop_name(raw_shop)
            quantity_value = float_from_cell(raw_quantity)
            price_value = float_from_cell(raw_price)
            price_per_kg_value = None if raw_price_per_kg in (None, "", "None") else float_from_cell(raw_price_per_kg)

            cleaned_rows.append({
                'shop': normalized_shop,
                'date': date_iso,
                'no': str(dataframe_row[column_no]) if column_no and pd.notna(dataframe_row[column_no]) else None,
                'item': collapse_whitespace(raw_item),
                'quantity': quantity_value,
                'price': price_value,
                'pricePerKg': price_per_kg_value,
                'row_total': float_from_cell(raw_total) if raw_total is not None else None,
            })

        if not cleaned_rows:
            return jsonify({'error': 'No valid rows found in the sheet.'}), 400

        # ---- Group rows by (shop, date) ----
        rows_grouped_by_bill = defaultdict(list)
        for row_dict in cleaned_rows:
            rows_grouped_by_bill[(row_dict['shop'], row_dict['date'])].append(row_dict)

        inserted_bills_summary = []
        skipped_duplicates_summary = []

        with get_session() as database_session:
            for (group_shop, group_date_iso), group_rows in rows_grouped_by_bill.items():
                # Build signature for the incoming group
                incoming_signature = build_items_signature(group_rows)

                # Existing signatures for same (shop, date)
                existing_signatures = fetch_existing_signatures_for_group(
                    database_session, user_id, group_shop, group_date_iso
                )

                # If identical bill (same signature) already exists, skip inserting (duplicate)
                if incoming_signature in existing_signatures.values():
                    duplicate_bill_id = next(
                        (bill_id for bill_id, sig in existing_signatures.items() if sig == incoming_signature),
                        None
                    )
                    skipped_duplicates_summary.append({
                        'shop': group_shop,
                        'date': group_date_iso,
                        'existing_billId': duplicate_bill_id
                    })
                    continue

                # Choose unique billId base & suffix if needed
                base_bill_id = f"{group_shop}_{group_date_iso}"
                existing_ids_same_base = {bill_id for bill_id in existing_signatures.keys() if bill_id.startswith(base_bill_id)}
                unique_bill_id = next_available_bill_id(existing_ids_same_base, base_bill_id)

                # ---- Compute the bill total with merged-cell awareness ----
                # ---- Compute the bill total with merged-cell awareness ----
                per_row_line_sums = []
                non_empty_row_totals = []

                # If the chosen column was explicitly 'unit_price', we multiply by qty.
                price_is_unit = (column_price == 'unit_price')

                for row_dict in group_rows:
                    quantity_value = float(row_dict.get('quantity') or 0)
                    price_value = float(row_dict.get('price') or 0)

                    # Default: treat 'price' as a LINE TOTAL (do NOT multiply).
                    line_sum = price_value

                    # If sheet explicitly says unit_price, then multiply.
                    if price_is_unit:
                        line_sum = quantity_value * price_value

                    # If a per-row total is present and price is blank/zero, use that.
                    if row_dict.get('row_total') is not None and (price_value in [0, 0.0, None]):
                        try:
                            line_sum = float(row_dict['row_total'])
                        except Exception:
                            pass

                    per_row_line_sums.append(line_sum)

                    if row_dict.get('row_total') is not None:
                        try:
                            non_empty_row_totals.append(float(row_dict['row_total']))
                        except Exception:
                            pass

                computed_from_line_totals = round(sum(per_row_line_sums), 2)

                # Merged grand-total logic:
                if len(non_empty_row_totals) == 1:
                    bill_total_sum = round(non_empty_row_totals[0], 2)
                elif len(non_empty_row_totals) > 1:
                    bill_total_sum = round(sum(non_empty_row_totals), 2)
                else:
                    bill_total_sum = computed_from_line_totals

                # month/year from date
                month_name = get_month_name_from_iso(group_date_iso)
                year_number = int(group_date_iso[:4])

                # Insert each item row
                for row_dict in group_rows:
                    new_row_kwargs = dict(
                        userid=int(user_id),
                        billId=unique_bill_id,
                        month=month_name,
                        year=year_number,
                        item=row_dict['item'],
                        quantity=row_dict['quantity'],
                        price=row_dict['price'],
                        shopName=group_shop,
                        address='',
                        invoiceNumber='',
                        totalSum=bill_total_sum,
                        date=group_date_iso
                    )
                    # include pricePerKg if column exists in your ORM
                    if hasattr(GroceriesModel, 'pricePerKg'):
                        new_row_kwargs['pricePerKg'] = row_dict['pricePerKg']

                    new_grocery_entry = GroceriesModel(**new_row_kwargs)
                    database_session.add(new_grocery_entry)

                database_session.commit()
                inserted_bills_summary.append({
                    'billId': unique_bill_id,
                    'shop': group_shop,
                    'date': group_date_iso,
                    'total': bill_total_sum
                })

        # Remember last inserted bill so your /view can open it
        if inserted_bills_summary:
            session['billid'] = inserted_bills_summary[-1]['billId']

        return jsonify({
            'success': True,
            'inserted_bills': inserted_bills_summary,
            'skipped_duplicates': skipped_duplicates_summary,
            'redirect': url_for('main.view_data') if inserted_bills_summary else None
        }), 200

    except Exception as exc:
        logger.error(f"Error during sheet upload: {str(exc)}")
        return jsonify({'error': 'An internal error occurred.', 'details': str(exc)}), 500


@main_bp.route('/upload_manual_bill', methods=['POST'])
@login_required
def upload_manual_bill():
    """
    Create a bill from manually typed-in items, for receipts you don't have
    a digital copy of. Expects JSON:
      { NameOfTheShop, datePicker, items: [{item, quantity, price}, ...] }
    Reuses the same (shop, date) duplicate-signature logic as upload_sheet_invoice.
    """
    try:
        user_id = fetch_user_id_from_session()
        if not user_id:
            return jsonify({'error': 'User not authenticated or invalid session.'}), 403

        GroceriesModel = models.get("groceries")
        if not GroceriesModel:
            return jsonify({'error': 'groceries table is not available.'}), 500

        payload = request.get_json(silent=True) or {}
        raw_shop = payload.get('NameOfTheShop')
        raw_date = payload.get('datePicker')
        raw_items = payload.get('items') or []

        if not raw_shop or not sanitize_input(raw_shop):
            return jsonify({'error': 'Missing or invalid shop name.'}), 400
        if not raw_date:
            return jsonify({'error': 'Missing date.'}), 400

        try:
            date_iso = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return jsonify({'error': f'Bad date value: {raw_date}'}), 400

        shop_name = normalize_shop_name(raw_shop)

        cleaned_items = []
        for entry in raw_items:
            item_name = collapse_whitespace(str(entry.get('item') or ''))
            if not item_name:
                continue
            if not sanitize_input(item_name):
                return jsonify({'error': f'Invalid characters in item name: {item_name}'}), 400
            quantity_value = float_from_cell(entry.get('quantity')) or 1.0
            price_value = float_from_cell(entry.get('price'))
            cleaned_items.append({'item': item_name, 'quantity': quantity_value, 'price': price_value})

        if not cleaned_items:
            return jsonify({'error': 'Add at least one item with a name.'}), 400

        month_name = get_month_name_from_iso(date_iso)
        year_number = int(date_iso[:4])

        with get_session() as db_session:
            incoming_signature = build_items_signature(cleaned_items)
            existing_signatures = fetch_existing_signatures_for_group(db_session, user_id, shop_name, date_iso)

            if incoming_signature in existing_signatures.values():
                duplicate_bill_id = next(
                    (bill_id for bill_id, sig in existing_signatures.items() if sig == incoming_signature),
                    None
                )
                return jsonify({
                    'warning': f'An identical bill already exists ({duplicate_bill_id}).',
                    'Bill_exists': True
                }), 200

            base_bill_id = f"{shop_name}_{date_iso}"
            existing_ids_same_base = {bid for bid in existing_signatures.keys() if bid.startswith(base_bill_id)}
            unique_bill_id = next_available_bill_id(existing_ids_same_base, base_bill_id)

            total_sum = round(sum(entry['price'] for entry in cleaned_items), 2)

            for entry in cleaned_items:
                new_entry = GroceriesModel(
                    userid=int(user_id),
                    billId=unique_bill_id,
                    month=month_name,
                    year=year_number,
                    item=entry['item'],
                    quantity=entry['quantity'],
                    price=entry['price'],
                    shopName=shop_name,
                    address='',
                    invoiceNumber='',
                    totalSum=total_sum,
                    date=date_iso
                )
                db_session.add(new_entry)

            db_session.commit()

        session['billid'] = unique_bill_id
        session['NameOfTheShop'] = shop_name

        return jsonify({
            'success': True,
            'billId': unique_bill_id,
            'total': total_sum,
            'redirect': url_for('main.view_data')
        }), 200

    except Exception as exc:
        logger.error(f"Error during manual bill entry: {str(exc)}")
        return jsonify({'error': 'An internal error occurred.', 'details': str(exc)}), 500
###################################################################################################################
def sanitize_input(input_str):
    """
    Sanitizes input to ensure it does not contain invalid characters.
    """
    invalid_chars = r'<>:"/\|?*'
    return not any(char in invalid_chars for char in input_str)


def fetch_user_id_from_session():
    """
        Fetches the user ID from the session and validates it against the database.
        Returns None if the user ID is invalid or missing.
        """
    username = session.get('username')
    if not username:
        from flask import current_app
        current_app.logger.warning("No username found in session.")
        return None

    try:
        with get_session() as db_session:
            user = db_session.query(Users).filter_by(username=username).first()
            if user:
                logger.info(f"Fetched user ID {user.userid} for username {username}. User ID type: {type(user.userid)}")
                return user.userid
            else:
                logger.warning(f"No user found for username {username}.")
                return None
    except Exception as e:
        logger.error(f"Error fetching user ID: {str(e)}")
        return None


def create_dataframe(pdf_path, filename, month, year, shopname, userid,bill_date_str):
    """
    Processes a PDF file, extracts data into a DataFrame, and associates it with a user ID.

    Args:
        pdf_path (str): Path to the input PDF file.
        filename (str): Name of the PDF file without extension.
        month (str): The month associated with the data.
        year (str):The year associated with the data.
        shopname (str): The name of the shop of purchased items
        userid (int): The user ID to associate with the extracted data.

    Returns:
        pd.DataFrame: Extracted data as a pandas DataFrame.
    """
    try:
        # Convert PDF to an image
        scanned_object = None
        if shopname in ( 'rewe','kaufland'):
        #if shopname in ('rewe', 'kaufland'):
            extracted_text = extract_text_from_pdf(pdf_path,filename)
            scanned_object = ImageScanner('', filename, extracted_text, month, year,bill_date_str)
            text = scanned_object.get_text()
            if not text or len(text) < 10:
                logger.warning("Insufficient text detected. Attempting secondary processing.")
                scanned_object.get_groceries_bill()
                df = scanned_object.create_data_frame()
            else:
                #scanned_object.get_groceries_bill()
                df = scanned_object.create_data_frame()
        else:
            image_path = pdf_to_jpg(pdf_path, filename, current_app.config['IMAGE_FOLDER'])
            scanned_object = ImageScanner(image_path, filename, '', month, year,bill_date_str)
            text = scanned_object.get_text()
            if not text or len(text) < 10:
                logger.warning("Insufficient text detected. Attempting secondary processing.")
                scanned_object.get_groceries_bill()
                df = scanned_object.create_data_frame()
            else:
                scanned_object.get_groceries_bill()
                df = scanned_object.create_data_frame()
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


def fetch_data_by_date(userid, date):
    """
    Fetch data from the groceries table for a specific user and date.

    Args:
        userid (int): User ID to filter the data.
        date (str): date to filter the data.

    Returns:
        list: A list of rows from the groceries table.
    """
    try:

        groceries = models.get('groceries')
        if not groceries:
            logger.error("groceries model not initialized.")
            return []

        with get_session() as session:
            results = session.query(groceries).filter_by(userid=userid, date=date).all()
            return [row.__dict__ for row in results]

    except Exception as e:
        logger.error(f"Error fetching groceries data: {str(e)}")
        return []


def calculate_overall_debts_and_insert(db_session,current_user_id):
    """
    This function calculates the total debt for each user based on all unsettled transactions
    and inserts the debt data into the DebtSettlements table.

    :param db_session: The active database session.
    """
    try:
        # Fetch all unsettled settlements
        settlements = db_session.query(Settlements).filter_by(settled=False).all()

        # Create a dictionary to track the total amount owed by each user to another user
        debt_summary = {}

        # Iterate through each settlement record
        for settlement in settlements:
            paid_by = settlement.paid_by_email  # Who paid
            owed_by = settlement.owed_by_email  # Who owes

            amount = settlement.amount  # Amount owed

            # Add the debt to the summary (Paid by owes to Owed by)
            if paid_by not in debt_summary:
                debt_summary[paid_by] = {}

            if owed_by not in debt_summary[paid_by]:
                debt_summary[paid_by][owed_by] = 0.0

            # Add the amount owed to the owed_by user
            debt_summary[paid_by][owed_by] += amount

        # Insert the calculated debt summary into DebtSettlements table
        for paid_by_email, owed_users in debt_summary.items():
            for owed_by_email, total_amount in owed_users.items():
                # Insert the calculated debt into the DebtSettlements table
                new_debt = DebtSettlements(
                    paid_by_email=paid_by_email,  # User who paid
                    owed_by_email=owed_by_email,  # User who owes
                    amount=total_amount,  # Total amount owed
                    settled=False,
                    userid=current_user_id# Initially not settled
                )
                db_session.add(new_debt)

        # Commit the transaction to the database
        db_session.commit()
        logger.info(
            "Debt summary for all unsettled transactions successfully calculated and inserted into DebtSettlements.")

    except Exception as e:
        db_session.rollback()  # Rollback in case of error
        logger.error(f"Error calculating and inserting overall debts: {str(e)}")
        raise


def calculate_debts_basedON_billId(billid, db_session,current_user_id):
    """
    This function fetches the details from the Settlements table, calculates the debt summary,
    and inserts the debt data into the DebtSettlements table.

    :param billid: The Bill ID for which the debt needs to be calculated.
    :param db_session: The active database session.
    """
    try:
        # Fetch all the unsettled settlements for the given bill
        #settlements = db_session.query(Settlements).filter_by(billId=billid, settled=False).all()
        settlements = db_session.query(Settlements).filter_by(billId=billid, settled=False,
                                                              userid=current_user_id).all()

        # Create a dictionary to track the total amount owed by each user to another user
        debt_summary = {}

        # Iterate through each settlement record
        for settlement in settlements:
            paid_by = settlement.paid_by_email  # Who paid
            owed_by = settlement.owed_by_email  # Who owes

            amount = settlement.amount  # Amount owed

            # Add the debt to the summary (Paid by owes to Owed by)
            if paid_by not in debt_summary:
                debt_summary[paid_by] = {}

            if owed_by not in debt_summary[paid_by]:
                debt_summary[paid_by][owed_by] = 0.0

            # Add the amount owed to the owed_by user
            debt_summary[paid_by][owed_by] += amount

        # Insert the calculated debt summary into DebtSettlements table
        for paid_by_email, owed_users in debt_summary.items():
            for owed_by_email, total_amount in owed_users.items():
                # Insert the calculated debt into the DebtSettlements table
                new_debt = DebtSettlementsBills(
                    billId=billid,
                    paid_by_email=paid_by_email,  # User who paid
                    owed_by_email=owed_by_email,  # User who owes
                    amount=total_amount,  # Total amount owed
                    settled=False,
                    created_by= current_user_id# Initially not settled
                )
                db_session.add(new_debt)

        # Commit the transaction to the database
        db_session.commit()
        logger.info(f"Debt summary for Bill ID {billid} successfully calculated and inserted into DebtSettlements.")

    except Exception as e:
        db_session.rollback()  # Rollback in case of error
        logger.error(f"Error calculating and inserting debts for Bill ID {billid}: {str(e)}")
        raise


def fetch_debt_settlement_details(billid, userid):
    """
    Fetches debt settlement details (amount, paid_by, owed_by) for a specific bill ID
    and user ID. Ensures data isolation per user.
    """
    try:
        with get_session() as db_session:
            logger.info(f"Fetching debt settlements for Bill ID: {billid} and User ID: {userid}")

            # Define aliases for SplitInvoiceUser
            PaidByUser = aliased(SplitInvoiceUser)
            OwedByUser = aliased(SplitInvoiceUser)

            # Query DebtSettlementsBills for the specified Bill ID and user ID
            results = db_session.query(
                DebtSettlementsBills.billId,
                DebtSettlementsBills.amount,
                PaidByUser.name.label('paid_by_name'),
                OwedByUser.name.label('owed_by_name')
            ).join(
                PaidByUser, PaidByUser.email == DebtSettlementsBills.paid_by_email, isouter=True
            ).join(
                OwedByUser, OwedByUser.email == DebtSettlementsBills.owed_by_email, isouter=True
            ).filter(
                DebtSettlementsBills.billId == billid,
                DebtSettlementsBills.created_by == userid  #  filtering by current user
            ).all()

            if not results:
                logger.info(f"No debt settlement records found for Bill ID: {billid} and User ID: {userid}")
            return results
    except Exception as e:
        logger.error(f"Error fetching debt settlement details for Bill ID {billid} and User ID {userid}: {str(e)}")
        return []


'''
def fetch_debt_settlement_details(billid):
    """
    Fetches debt settlement details (amount, paid_by, owed_by) for a specific bill ID
    and retrieves the user names from SplitInvoiceUser based on email.
    """
    try:
        with get_session() as db_session:
            logger.info(f"Fetching debt settlements for Bill ID: {billid}")

            # Define aliases for SplitInvoiceUser
            PaidByUser = aliased(SplitInvoiceUser)
            OwedByUser = aliased(SplitInvoiceUser)

            # Query DebtSettlementsBills for the specified Bill ID and join with SplitInvoiceUser using emails
            results = db_session.query(
                DebtSettlementsBills.billId,
                DebtSettlementsBills.amount,
                PaidByUser.name.label('paid_by_name'),
                OwedByUser.name.label('owed_by_name')
            ).join(
                PaidByUser, PaidByUser.email == DebtSettlementsBills.paid_by_email, isouter=True
            ).join(
                OwedByUser, OwedByUser.email == DebtSettlementsBills.owed_by_email, isouter=True
            ).filter(DebtSettlementsBills.billId == billid).all()

            if not results:
                logger.info(f"No debt settlement records found for Bill ID: {billid}")
            logger.info(f"results: {results}")
            return results
    except Exception as e:
        logger.error(f"Error fetching debt settlement details for Bill ID {billid}: {str(e)}")
        return []
'''

def update_debt_summary(db_session):
    """
    Update the DebtSummary table with the latest totals from DebtSettlementsBills.
    Reopens settled debts if new amounts are added.
    """
    try:
        totals = db_session.query(
            DebtSettlementsBills.paid_by_email,
            DebtSettlementsBills.owed_by_email,
            func.sum(DebtSettlementsBills.amount).label('total_amount')
        ).filter(
            DebtSettlementsBills.settled == False
        ).group_by(
            DebtSettlementsBills.paid_by_email,
            DebtSettlementsBills.owed_by_email
        ).all()

        for paid_by_email, owed_by_email, total_amount in totals:
            debt_summary = db_session.query(DebtSummary).filter_by(
                paid_by_email=paid_by_email,
                owed_by_email=owed_by_email
            ).first()

            if debt_summary:
                # Update existing record
                previous_total = debt_summary.total_amount or 0
                previous_original = debt_summary.original_amount or 0

                # Update current values
                debt_summary.total_amount = total_amount

                # If this is a "reopen", increase original_amount
                if total_amount > previous_total:
                    debt_summary.original_amount = float(previous_original) + (float(total_amount) - float(previous_total))


                # If it was marked settled, reopen it
                if debt_summary.settled:
                    debt_summary.settled = False

                debt_summary.last_updated = datetime.utcnow()
            else:
                # Insert new record
                new_summary = DebtSummary(
                    paid_by_email=paid_by_email,
                    owed_by_email=owed_by_email,
                    total_amount=total_amount,
                    original_amount=total_amount,
                    settled_amount=0,
                    settled=False,
                    last_updated=datetime.utcnow()
                )
                db_session.add(new_summary)

        db_session.commit()
        logger.info("DebtSummary table updated successfully.")

    except Exception as e:
        db_session.rollback()
        logger.error(f"Error updating DebtSummary table: {str(e)}")
        raise


def settle_debt_summary(db_session, paid_by_email, owed_by_email, amount_paid, method=None, note=None):
    record = db_session.query(DebtSummary).filter_by(
        paid_by_email=paid_by_email,
        owed_by_email=owed_by_email,
        settled=False
    ).first()

    if not record:
        return {'error': 'No active debt found.'}

    # Log the payment
    log = SettlementLog(
        paid_by_email=paid_by_email,
        owed_by_email=owed_by_email,
        amount_paid=amount_paid,
        method=method,
        note=note
    )
    db_session.add(log)

    # Update debt record
    remaining = record.total_amount
    if amount_paid >= remaining:
        record.total_amount = 0
        record.settled = True
        record.settled_amount = record.original_amount
    else:
        record.total_amount -= amount_paid
        record.settled_amount += amount_paid
        record.num_partial_settlements += 1

    record.last_settled_on = datetime.now(timezone.utc)
    record.last_updated = datetime.now(timezone.utc)

    db_session.commit()
    return {'success': True, 'remaining_balance': record.total_amount}

def mark_related_debts_as_settled(db_session, paid_by, owed_by):
    # Mark bills
    db_session.query(DebtSettlementsBills).filter_by(
        paid_by_email=paid_by,
        owed_by_email=owed_by,
        settled=False
    ).update({"settled": True}, synchronize_session='fetch')

    # Mark summarized debts
    db_session.query(DebtSettlements).filter_by(
        paid_by_email=paid_by,
        owed_by_email=owed_by,
        settled=False
    ).update({"settled": True}, synchronize_session='fetch')

    # If you use a separate 'settlements' table too
    db_session.query(Settlements).filter_by(
        paid_by_email=paid_by,
        owed_by_email=owed_by,
        settled=False
    ).update({"settled": True}, synchronize_session='fetch')




def get_filtered_bill_ids(db_session, userid: int, shop: str | None,
                          month: str | None, start: str | None, end: str | None) -> tuple[set[str], bool]:
    """
    Returns (bill_ids, any_filter_requested).
    Strategy:
      1) Build predicates using groceries.(year, month) (TRIM+LOWER on month, robust YEAR compare)
      2) If that yields zero and a time filter was requested, fallback to groceries.date (cast to DATE)
    """
    groceries = models.get("groceries")
    if not groceries:
        current_app.logger.error("groceries model not initialized.")
        return set(), False

    any_filter = bool(shop or month or start or end)

    # ---- Base (shop + userid) ----
    shop_user_filters = [groceries.userid == userid]
    if shop:
        shop_user_filters.append(
            func.lower(func.trim(groceries.shopName)) == shop.strip().lower()
        )

    # ---- TIME via (year, month) ----
    ym_time_clause = None
    if month:
        y, m = split_ym(month)
        ym_time_clause = and_(
            year_eq(groceries.year, y),
            func.lower(func.trim(groceries.month)) == MONTH_NUM_TO_NAME[m]
        )
    elif start or end:
        if start:
            ys, ms = split_ym(start)
        else:
            ys, ms = 1900, 1
        if end:
            ye, me = split_ym(end)
        else:
            ye, me = 2999, 12

        ors = []
        for y, m in ym_iter(ys, ms, ye, me):
            ors.append(and_(
                year_eq(groceries.year, y),
                func.lower(func.trim(groceries.month)) == MONTH_NUM_TO_NAME[m]
            ))
        ym_time_clause = or_(*ors) if ors else None

    # Try YEAR+MONTH first
    ym_filters = list(shop_user_filters)
    if ym_time_clause is not None:
        ym_filters.append(ym_time_clause)

    bill_ids = set()
    if ym_time_clause is not None:
        rows = db_session.query(groceries.billId).filter(*ym_filters).distinct().all()
        bill_ids = {r[0] for r in rows}

    # ---- Fallback: DATE range (cast to DATE) if a time filter was requested and nothing matched
    if not bill_ids and (month or start or end):
        date_filters = list(shop_user_filters)
        if month:
            s, e = parse_month_str(month)
            date_filters.append(cast(groceries.date, Date) >= s)
            date_filters.append(cast(groceries.date, Date) < e)
        else:
            if start:
                s, _ = parse_month_str(start)
                date_filters.append(cast(groceries.date, Date) >= s)
            if end:
                _, e = parse_month_str(end)
                date_filters.append(cast(groceries.date, Date) < e)

        rows = db_session.query(groceries.billId).filter(*date_filters).distinct().all()
        bill_ids = {r[0] for r in rows}

    # Debug log (optional)
    current_app.logger.info(
        f"[expenditures] filters any={any_filter} shop={shop} month={month} start={start} end={end} -> matched_bills={len(bill_ids)}"
    )

    return bill_ids, any_filter




def fetch_existing_signatures_for_group(db_session, user_id: int, shop_name: str, date_iso: str):
    """
    Return {billId: signature} for all existing bills for this user + (shop, date).
    """
    GroceriesModel = models.get("groceries")
    existing_rows = db_session.query(GroceriesModel).filter_by(
        userid=user_id, shopName=shop_name, date=date_iso
    ).all()

    rows_by_bill_id = defaultdict(list)
    for row in existing_rows:
        rows_by_bill_id[row.billId].append({
            'item': row.item,
            'quantity': row.quantity,
            'price': row.price,
            'pricePerKg': getattr(row, 'pricePerKg', None),
        })

    signatures_by_bill_id = {bill_id: build_items_signature(row_list)
                             for bill_id, row_list in rows_by_bill_id.items()}
    return signatures_by_bill_id

'''
def next_available_bill_id(existing_bill_ids_for_base, base_bill_id: str) -> str:
    """Return base bill id or suffixed version base_1, base_2, ... if conflicts exist."""
    if base_bill_id not in existing_bill_ids_for_base:
        return base_bill_id
    suffix_index = 1
    while f"{base_bill_id}_{suffix_index}" in existing_bill_ids_for_base:
        suffix_index += 1
    return f"{base_bill_id}_{suffix_index}" '''

from sqlalchemy import bindparam

def _normalize_bill_ids_to_str(ids):
    if not ids:
        return []
    out = []
    stack = list(ids)
    while stack:
        x = stack.pop()
        # flatten containers but NOT strings/bytes
        if isinstance(x, (list, tuple, set)):
            stack.extend(x)
            continue

        # ignore booleans explicitly (True/False are ints in Python)
        if isinstance(x, bool):
            continue

        # coerce everything else to string
        s = str(x).strip()
        if s:  # drop empty strings
            out.append(s)

    # dedupe preserving order
    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


@main_bp.route("/analytics", methods=["GET"])
@login_required
def analytics_page():
    return render_template("analytics.html")
