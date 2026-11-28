"""
Credit card section.

  GET /credit-card/dashboard?month=YYYY-MM    analysis dashboard (billing month)
"""
import logging
import re

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from app.models.models import get_session
from app.services import card_dashboard
from app.services import spend_categories as sc
from app.utils.route_helpers import fetch_user_id_from_session

logger = logging.getLogger(__name__)

credit_card_bp = Blueprint("credit_card", __name__, url_prefix="/credit-card")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@credit_card_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated or invalid session."}), 403

    month = request.args.get("month", "")
    month = month if _MONTH_RE.match(month) else None
    with get_session() as db_session:
        sc.categorize_user(db_session, userid)
        data = card_dashboard.load_dashboard(db_session, userid, month)
    return render_template("credit_card_dashboard.html", d=data)
