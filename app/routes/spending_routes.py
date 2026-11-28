"""
Purchase categories: spending-by-category page and its JSON API.

  GET    /spending?month=YYYY-MM                     page
  GET    /api/spending?month=YYYY-MM                 report (totals + purchases)
  GET    /api/spend_categories                       category tree
  PUT    /api/purchases/<type>/<ref>/category        {"category": slug}  manual category
                                                     {"category": slug, "apply_to_merchant": true, "keyword": "..."}
                                                       -> also saves a merchant rule
  DELETE /api/purchases/<type>/<ref>/category        drop manual category (rules apply again)
  GET    /api/category_rules                         the user's own rules
  POST   /api/category_rules                         {"keyword": "...", "category": slug}
  DELETE /api/category_rules/<id>

<type> is "card_transaction" or "bill"; <ref> is the transaction id or billId.
"""
import logging
import re
from datetime import date

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from app.models.models import get_session
from app.services import spend_categories as sc
from app.utils.route_helpers import fetch_user_id_from_session

logger = logging.getLogger(__name__)

spending_bp = Blueprint("spending", __name__)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _current_user_id():
    return fetch_user_id_from_session()


def _unauthorized():
    return jsonify({"error": "User not authenticated or invalid session."}), 403


def _month_arg(db_session, userid):
    month = request.args.get("month", "")
    if _MONTH_RE.match(month):
        return month
    months = sc.available_months(db_session, userid)
    return months[0] if months else date.today().strftime("%Y-%m")


def _serialize_report(report):
    out = dict(report)
    out["purchases"] = [dict(p, date=p["date"].isoformat()) for p in report["purchases"]]
    return out


@spending_bp.route("/spending", methods=["GET"])
@login_required
def spending_page():
    userid = _current_user_id()
    if not userid:
        return _unauthorized()
    with get_session() as db_session:
        sc.categorize_user(db_session, userid)
        month = _month_arg(db_session, userid)
        return render_template(
            "spending.html",
            report=sc.spending_report(db_session, userid, month),
            months=sc.available_months(db_session, userid),
            tree=sc.category_tree(db_session),
            rules=sc.list_user_rules(db_session, userid),
        )


@spending_bp.route("/api/spending", methods=["GET"])
@login_required
def spending_api():
    userid = _current_user_id()
    if not userid:
        return _unauthorized()
    with get_session() as db_session:
        sc.categorize_user(db_session, userid)
        month = _month_arg(db_session, userid)
        return jsonify(_serialize_report(sc.spending_report(db_session, userid, month)))


@spending_bp.route("/api/spend_categories", methods=["GET"])
@login_required
def spend_categories_api():
    with get_session() as db_session:
        return jsonify(sc.category_tree(db_session))


@spending_bp.route("/api/purchases/<purchase_type>/<path:ref>/category", methods=["PUT", "DELETE"])
@login_required
def purchase_category_api(purchase_type, ref):
    userid = _current_user_id()
    if not userid:
        return _unauthorized()
    if purchase_type not in sc.PURCHASE_TYPES:
        return jsonify({"error": f"Unknown purchase type: {purchase_type}"}), 404

    try:
        with get_session() as db_session:
            if request.method == "DELETE":
                sc.reset_purchase_category(db_session, userid, purchase_type, ref)
                return jsonify({"success": True})

            data = request.get_json(silent=True) or {}
            slug = (data.get("category") or "").strip()
            if not slug:
                return jsonify({"error": "category is required"}), 400

            if data.get("apply_to_merchant"):
                keyword = (data.get("keyword") or "").strip()
                if not keyword:
                    return jsonify({"error": "keyword is required to apply to a merchant"}), 400
                rule_id = sc.save_user_rule(db_session, userid, keyword, slug)
                # The rule now decides; drop any manual override on this purchase.
                sc.reset_purchase_category(db_session, userid, purchase_type, ref)
                return jsonify({"success": True, "rule_id": rule_id})

            sc.set_purchase_category(db_session, userid, purchase_type, ref, slug)
            return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@spending_bp.route("/api/category_rules", methods=["GET", "POST"])
@login_required
def category_rules_api():
    userid = _current_user_id()
    if not userid:
        return _unauthorized()
    with get_session() as db_session:
        if request.method == "GET":
            return jsonify(sc.list_user_rules(db_session, userid))
        data = request.get_json(silent=True) or {}
        try:
            rule_id = sc.save_user_rule(db_session, userid, data.get("keyword") or "", (data.get("category") or "").strip())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"success": True, "rule_id": rule_id}), 201


@spending_bp.route("/api/category_rules/<int:rule_id>", methods=["DELETE"])
@login_required
def category_rule_delete_api(rule_id):
    userid = _current_user_id()
    if not userid:
        return _unauthorized()
    try:
        with get_session() as db_session:
            sc.delete_user_rule(db_session, userid, rule_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"success": True})
