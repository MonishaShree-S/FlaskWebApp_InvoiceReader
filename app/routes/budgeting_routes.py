# app/routes/budgeting_routes.py
"""
Budgeting & planning feature: category spend, budgets/thresholds,
frequently-bought-items, "cut spending" recommendations, and a
next-month shopping list prediction.

Register in your app factory next to the other blueprints, e.g.:

    from app.routes.budgeting_routes import budgeting_bp
    app.register_blueprint(budgeting_bp)

Requires:
  - migrate_budgeting.py to have been run already (adds `category` column
    + `Budgets` table).
  - app/services/category_mapper.py (categorize_item, all_categories).
  - The existing helper `fetch_user_id_from_session()` -- imported here from
    main_routes to avoid duplicating it. If that creates a circular import
    in your project layout, copy the function body in here instead.
"""

import calendar
import logging
from collections import defaultdict
from datetime import datetime, date

from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required
from sqlalchemy import func

from app.models.models import models, get_session, Budgets  # Budgets: see models_budgeting.py setup note
from app.services.category_mapper import categorize_item, all_categories
from app.routes.main_routes import fetch_user_id_from_session  # reuse existing helper
# NOTE on circular imports: if your app layout makes `main_routes -> budgeting_routes`
# and `budgeting_routes -> main_routes` circular, simply copy the small
# fetch_user_id_from_session() function (in main_routes.py, ~line 2295) into this
# file instead of importing it.

logger = logging.getLogger(__name__)

budgeting_bp = Blueprint('budgeting', __name__)

MONTH_NAMES_IN_ORDER = [m.lower() for m in calendar.month_name if m]  # ['january', ..., 'december']

# Routine staples: bought out of necessity on a near-weekly cadence, regardless
# of price. "Buy this less" or "switch brands" isn't useful advice for these --
# they're excluded from "where to cut" entirely. Match is case-insensitive
# substring against the item name. Extend this list as needed.
STAPLE_KEYWORDS = [
    "milch", "h-milch", "vollmilch",          # milk
    "eier", "ei m-l", "ei l", "ei xl",        # eggs
    "brot", "broetchen", "brötchen",          # bread/rolls
    "tomate", "rispentomate",                  # tomatoes
    "tortilla",                                # tortillas/wraps
    "butter",
    "mehl",                                    # flour
    "zucker",                                  # sugar
    "kartoffel",                               # potatoes
    "zwiebel",                                 # onions
    "nudel", "spaghetti",                      # pasta
    "reis",                                    # rice
    "haferflocken",                            # oats
    "toilettenpapier", "klopapier",            # toilet paper
]


def _is_staple(item_name: str) -> bool:
    name = (item_name or "").lower()
    return any(kw in name for kw in STAPLE_KEYWORDS)


def _current_year_month():
    today = date.today()
    return today.year, today.strftime("%B").lower()


def _prev_n_months(year: int, month_name: str, n: int):
    """Return a list of (year, month_name) tuples for the n months strictly
    before (year, month_name), most recent first."""
    idx = MONTH_NAMES_IN_ORDER.index(month_name)  # 0-based, jan=0
    result = []
    y, m_idx = year, idx
    for _ in range(n):
        m_idx -= 1
        if m_idx < 0:
            m_idx = 11
            y -= 1
        result.append((y, MONTH_NAMES_IN_ORDER[m_idx]))
    return result


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------

@budgeting_bp.route("/budgeting", methods=["GET"])
@login_required
def budgeting_dashboard():
    return render_template("budgeting.html")


# ---------------------------------------------------------------------------
# CATEGORY SPEND (this month + trend)
# ---------------------------------------------------------------------------

@budgeting_bp.route("/api/budgeting/category_spend", methods=["GET"])
@login_required
def category_spend():
    """
    Spend per category for a given month (defaults to current month).
    Query params: ?year=2026&month=june
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    year = request.args.get("year")
    month = request.args.get("month")
    if not year or not month:
        year, month = _current_year_month()
    else:
        year = int(year)
        month = month.lower()

    with get_session() as db_session:
        rows = db_session.query(
            func.coalesce(modelShop.category, "Other").label("category"),
            func.coalesce(func.sum(modelShop.price), 0).label("total"),
            func.count().label("item_count"),
        ).filter(
            modelShop.userid == userid,
            modelShop.year == year,
            modelShop.month == month,
        ).group_by(
            modelShop.category
        ).order_by(
            func.sum(modelShop.price).desc()
        ).all()

        result = [
            {"category": cat, "total": float(total or 0), "item_count": int(item_count or 0)}
            for cat, total, item_count in rows
        ]

        return jsonify({
            "year": year,
            "month": month,
            "categories": result,
            "total_spent": round(sum(r["total"] for r in result), 2),
        })


# ---------------------------------------------------------------------------
# BUDGETS — CRUD
# ---------------------------------------------------------------------------

@budgeting_bp.route("/api/budgeting/budgets", methods=["GET"])
@login_required
def list_budgets():
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    with get_session() as db_session:
        rows = db_session.query(Budgets).filter_by(userid=userid).all()
        return jsonify([
            {
                "id": b.id,
                "category": b.category,
                "monthly_limit": float(b.monthly_limit),
                "warn_threshold_pct": b.warn_threshold_pct,
            }
            for b in rows
        ])


@budgeting_bp.route("/api/budgeting/budgets", methods=["POST"])
@login_required
def upsert_budget():
    """
    Create or update a per-category monthly budget.
    Body JSON: { "category": "Dairy & Eggs", "monthly_limit": 100, "warn_threshold_pct": 90 }
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    data = request.get_json(silent=True) or {}
    category = (data.get("category") or "").strip()
    monthly_limit = data.get("monthly_limit")
    warn_threshold_pct = data.get("warn_threshold_pct", 90)

    if not category:
        return jsonify({"error": "category is required"}), 400
    try:
        monthly_limit = float(monthly_limit)
        if monthly_limit < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "monthly_limit must be a positive number"}), 400

    try:
        warn_threshold_pct = int(warn_threshold_pct)
        warn_threshold_pct = max(1, min(100, warn_threshold_pct))
    except (TypeError, ValueError):
        warn_threshold_pct = 90

    with get_session() as db_session:
        existing = db_session.query(Budgets).filter_by(userid=userid, category=category).first()
        if existing:
            existing.monthly_limit = monthly_limit
            existing.warn_threshold_pct = warn_threshold_pct
            existing.updated_at = datetime.utcnow()
        else:
            existing = Budgets(
                userid=userid,
                category=category,
                monthly_limit=monthly_limit,
                warn_threshold_pct=warn_threshold_pct,
            )
            db_session.add(existing)
        db_session.commit()

        return jsonify({
            "id": existing.id,
            "category": existing.category,
            "monthly_limit": float(existing.monthly_limit),
            "warn_threshold_pct": existing.warn_threshold_pct,
        })


@budgeting_bp.route("/api/budgeting/budgets/<int:budget_id>", methods=["DELETE"])
@login_required
def delete_budget(budget_id):
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    with get_session() as db_session:
        existing = db_session.query(Budgets).filter_by(id=budget_id, userid=userid).first()
        if not existing:
            return jsonify({"error": "Budget not found"}), 404
        db_session.delete(existing)
        db_session.commit()
        return jsonify({"success": True})


@budgeting_bp.route("/api/budgeting/categories", methods=["GET"])
@login_required
def list_known_categories():
    """All categories the auto-classifier knows about, for the budget-setup UI."""
    return jsonify(all_categories())


# ---------------------------------------------------------------------------
# BUDGET VS ACTUAL (the core "threshold + notify" endpoint)
# ---------------------------------------------------------------------------

@budgeting_bp.route("/api/budgeting/status", methods=["GET"])
@login_required
def budget_status():
    """
    For the current (or requested) month: actual spend vs budget per category,
    with a status flag the front-end uses to render the banner / progress bars.

    status per category:
      "ok"      -> under warn_threshold_pct
      "warning" -> >= warn_threshold_pct and < 100%
      "over"    -> >= 100%
      "no_budget" -> category has spend but no budget set
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    year = request.args.get("year")
    month = request.args.get("month")
    if not year or not month:
        year, month = _current_year_month()
    else:
        year = int(year)
        month = month.lower()

    with get_session() as db_session:
        spend_rows = db_session.query(
            func.coalesce(modelShop.category, "Other").label("category"),
            func.coalesce(func.sum(modelShop.price), 0).label("total"),
        ).filter(
            modelShop.userid == userid,
            modelShop.year == year,
            modelShop.month == month,
        ).group_by(
            modelShop.category
        ).all()

        spend_by_category = {cat: float(total or 0) for cat, total in spend_rows}

        budgets = db_session.query(Budgets).filter_by(userid=userid).all()
        budget_by_category = {b.category: b for b in budgets}

        all_cats = set(spend_by_category.keys()) | set(budget_by_category.keys())

        results = []
        any_warning = False
        any_over = False

        for cat in sorted(all_cats):
            spent = round(spend_by_category.get(cat, 0.0), 2)
            budget = budget_by_category.get(cat)

            if budget is None:
                status = "no_budget" if spent > 0 else "ok"
                limit = None
                pct = None
            else:
                limit = float(budget.monthly_limit)
                pct = round((spent / limit) * 100, 1) if limit > 0 else 0.0
                if pct >= 100:
                    status = "over"
                    any_over = True
                elif pct >= budget.warn_threshold_pct:
                    status = "warning"
                    any_warning = True
                else:
                    status = "ok"

            results.append({
                "category": cat,
                "spent": spent,
                "limit": limit,
                "pct_used": pct,
                "status": status,
            })

        # Sort: over-budget first, then warning, then no_budget, then ok
        status_order = {"over": 0, "warning": 1, "no_budget": 2, "ok": 3}
        results.sort(key=lambda r: status_order.get(r["status"], 4))

        banner = None
        over_cats = [r["category"] for r in results if r["status"] == "over"]
        warning_cats = [r["category"] for r in results if r["status"] == "warning"]

        if over_cats:
            banner = {
                "level": "danger",
                "message": f"Over budget in {len(over_cats)} categor{'y' if len(over_cats)==1 else 'ies'}: "
                           + ", ".join(over_cats[:3]) + ("..." if len(over_cats) > 3 else ""),
            }
        elif warning_cats:
            banner = {
                "level": "warning",
                "message": f"Approaching limit in {len(warning_cats)} categor{'y' if len(warning_cats)==1 else 'ies'}: "
                           + ", ".join(warning_cats[:3]) + ("..." if len(warning_cats) > 3 else ""),
            }

        return jsonify({
            "year": year,
            "month": month,
            "categories": results,
            "banner": banner,
        })


# ---------------------------------------------------------------------------
# FREQUENTLY BOUGHT ITEMS (ranked, with avg price + monthly frequency)
# ---------------------------------------------------------------------------

@budgeting_bp.route("/api/budgeting/frequent_items", methods=["GET"])
@login_required
def frequent_items():
    """
    Top N most-frequently-bought items, all time (or within a lookback
    window via ?months=N), ranked by purchase count.
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    limit = int(request.args.get("limit", 15))
    lookback_months = request.args.get("months")

    with get_session() as db_session:
        base_filter = [modelShop.userid == userid]
        if lookback_months:
            lookback_months = int(lookback_months)
            year, month = _current_year_month()
            window = [(year, month)] + _prev_n_months(year, month, lookback_months - 1)
            from sqlalchemy import and_, or_
            base_filter.append(or_(*[
                and_(modelShop.year == y, modelShop.month == m) for y, m in window
            ]))

        rows = db_session.query(
            modelShop.item,
            func.count().label("purchase_count"),
            func.coalesce(func.sum(modelShop.price), 0).label("total_spent"),
            func.coalesce(func.avg(modelShop.price), 0).label("avg_price"),
        ).filter(
            *base_filter
        ).group_by(
            modelShop.item
        ).order_by(
            func.count().desc()
        ).limit(limit).all()

        result = [
            {
                "item": item,
                "purchase_count": int(count or 0),
                "total_spent": round(float(total or 0), 2),
                "avg_price": round(float(avg or 0), 2),
            }
            for item, count, total, avg in rows
        ]

        return jsonify(result)


# ---------------------------------------------------------------------------
# "WHERE TO CUT" RECOMMENDATIONS
# ---------------------------------------------------------------------------

@budgeting_bp.route("/api/budgeting/recommendations", methods=["GET"])
@login_required
def cut_recommendations():
    """
    Suggests where to cut spending, based on:
      1. Categories currently over/near budget -> direct recommendation.
      2. Items bought very frequently with high total spend, even without
         a budget set -> "this adds up" style nudges.

    This is a heuristic, not financial advice -- it's meant to point at the
    biggest, easiest levers, not to dictate decisions.
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    year, month = _current_year_month()
    yr_param = request.args.get("year")
    mo_param = request.args.get("month")
    if yr_param and mo_param:
        year, month = int(yr_param), mo_param.lower()

    recommendations = []

    with get_session() as db_session:
        # 1) Budget-driven recommendations
        spend_rows = db_session.query(
            func.coalesce(modelShop.category, "Other").label("category"),
            func.coalesce(func.sum(modelShop.price), 0).label("total"),
        ).filter(
            modelShop.userid == userid,
            modelShop.year == year,
            modelShop.month == month,
        ).group_by(
            modelShop.category
        ).all()
        spend_by_category = {cat: float(total or 0) for cat, total in spend_rows}

        budgets = db_session.query(Budgets).filter_by(userid=userid).all()
        for b in budgets:
            spent = spend_by_category.get(b.category, 0.0)
            if b.monthly_limit <= 0:
                continue
            pct = (spent / b.monthly_limit) * 100
            if pct >= 100:
                over_by = round(spent - b.monthly_limit, 2)
                recommendations.append({
                    "type": "over_budget",
                    "category": b.category,
                    "detail": f"€{over_by:.2f} over your €{b.monthly_limit:.0f} budget this month. "
                              f"Consider trimming purchases in {b.category} next month.",
                    "severity": "high",
                })
            elif pct >= b.warn_threshold_pct:
                remaining = round(b.monthly_limit - spent, 2)
                recommendations.append({
                    "type": "near_budget",
                    "category": b.category,
                    "detail": f"Only €{remaining:.2f} left in your {b.category} budget this month.",
                    "severity": "medium",
                })

        # 2) Item-level: items that are genuinely recurring cost sinks --
        #    bought across several different months (not just many times in
        #    one binge), AND still being bought recently (not a habit that's
        #    already stopped). Staples are excluded outright since "buy this
        #    less" isn't actionable advice for weekly necessities. Items
        #    already flagged as the cheapest/store-brand option skip the
        #    "switch brands" suggestion and get different wording instead.
        #
        # SQL Server doesn't support COUNT(DISTINCT col1, col2), so distinct
        # (year, month) coverage per item is computed by first aggregating
        # per (item, year, month), then rolling that up in Python.
        per_item_month = db_session.query(
            modelShop.item,
            func.coalesce(modelShop.category, "Other").label("category"),
            modelShop.year,
            modelShop.month,
            func.count().label("purchase_count"),
            func.coalesce(func.sum(modelShop.price), 0).label("total_spent"),
            func.max(modelShop.is_budget_brand).label("is_budget_brand"),
        ).filter(
            modelShop.userid == userid
        ).group_by(
            modelShop.item, modelShop.category, modelShop.year, modelShop.month
        ).all()

        recent_years_months = set([(year, month)] + _prev_n_months(year, month, 2))  # current + prior 2 months

        item_summary = defaultdict(lambda: {
            "category": "Other", "purchase_count": 0, "total_spent": 0.0,
            "months": set(), "is_recent": False, "is_budget_brand": False,
        })
        for it, cat, yr, mo, count, total, is_budget_brand in per_item_month:
            s = item_summary[it]
            s["category"] = cat
            s["purchase_count"] += int(count or 0)
            s["total_spent"] += float(total or 0)
            s["months"].add((yr, mo))
            if (yr, mo) in recent_years_months:
                s["is_recent"] = True
            if is_budget_brand:
                s["is_budget_brand"] = True

        candidates = [
            (item, s) for item, s in item_summary.items()
            if len(s["months"]) >= 3 and s["is_recent"] and not _is_staple(item)
        ]
        candidates.sort(key=lambda pair: pair[1]["total_spent"], reverse=True)

        for item, s in candidates[:5]:
            if s["is_budget_brand"]:
                detail = (
                    f"'{item}' was bought {s['purchase_count']} times, totaling €{s['total_spent']:.2f}. "
                    f"Already on a budget/store-brand line, so the main lever here is buying less "
                    f"or in bulk, not switching brands."
                )
            else:
                detail = (
                    f"'{item}' was bought {s['purchase_count']} times, totaling €{s['total_spent']:.2f}. "
                    f"A cheaper brand or buying in bulk could reduce this."
                )

            recommendations.append({
                "type": "frequent_high_cost_item",
                "category": s["category"],
                "detail": detail,
                "severity": "low",
                "item": item,
            })

        # Sort by severity: high, medium, low
        severity_order = {"high": 0, "medium": 1, "low": 2}
        recommendations.sort(key=lambda r: severity_order.get(r["severity"], 3))

        return jsonify(recommendations)


# ---------------------------------------------------------------------------
# NEXT MONTH'S SHOPPING LIST (recurring items prediction)
# ---------------------------------------------------------------------------

@budgeting_bp.route("/api/budgeting/next_month_list", methods=["GET"])
@login_required
def next_month_list():
    """
    Predicts a shopping list for next month based on recurring purchase
    patterns over the last N months (default 4).

    An item qualifies as "recurring" if it was bought in at least
    `min_months` of the last `lookback_months` months (default: at least
    2 of the last 4).

    Returns items grouped by category, with the average quantity and last
    paid price, plus an estimated total cost for the list.
    """
    userid = fetch_user_id_from_session()
    if not userid:
        return jsonify({"error": "User not authenticated"}), 403

    modelShop = models.get("groceries")
    if not modelShop:
        return jsonify({"error": "groceries model not found"}), 500

    lookback_months = int(request.args.get("lookback_months", 4))
    min_months = int(request.args.get("min_months", 2))

    year, month = _current_year_month()
    window = [(year, month)] + _prev_n_months(year, month, lookback_months - 1)
    # window = current month + lookback_months-1 previous months (total = lookback_months)

    with get_session() as db_session:
        # Pull all rows in the lookback window in one query
        filters_or = []
        from sqlalchemy import and_, or_
        for y, m in window:
            filters_or.append(and_(modelShop.year == y, modelShop.month == m))

        rows = db_session.query(
            modelShop.item,
            func.coalesce(modelShop.category, "Other").label("category"),
            modelShop.year,
            modelShop.month,
            modelShop.quantity,
            modelShop.price,
        ).filter(
            modelShop.userid == userid,
            or_(*filters_or)
        ).all()

        # item -> { months_seen: set(), quantities: [], prices_with_month: [(year,month,price)] }
        item_stats = defaultdict(lambda: {
            "category": "Other",
            "months_seen": set(),
            "quantities": [],
            "prices_by_month": [],
        })

        for item, category, yr, mo, qty, price in rows:
            key = item
            stats = item_stats[key]
            stats["category"] = category or "Other"
            stats["months_seen"].add((yr, mo))
            if qty:
                stats["quantities"].append(float(qty))
            if price:
                stats["prices_by_month"].append(((yr, mo), float(price)))

        recurring_items = []
        for item, stats in item_stats.items():
            months_count = len(stats["months_seen"])
            if months_count < min_months:
                continue

            avg_qty = round(sum(stats["quantities"]) / len(stats["quantities"]), 1) if stats["quantities"] else 1.0

            # last paid price = price from the most recent month it was bought
            last_price = None
            if stats["prices_by_month"]:
                stats["prices_by_month"].sort(key=lambda t: (t[0][0], MONTH_NAMES_IN_ORDER.index(t[0][1])))
                last_price = stats["prices_by_month"][-1][1]

            recurring_items.append({
                "item": item,
                "category": stats["category"],
                "months_bought": months_count,
                "lookback_months": lookback_months,
                "avg_quantity": avg_qty,
                "estimated_price": round(last_price, 2) if last_price is not None else None,
            })

        # Group by category for the UI
        by_category = defaultdict(list)
        for entry in recurring_items:
            by_category[entry["category"]].append(entry)

        for cat in by_category:
            by_category[cat].sort(key=lambda e: -e["months_bought"])

        estimated_total = round(sum(
            (e["estimated_price"] or 0) for e in recurring_items
        ), 2)

        return jsonify({
            "lookback_months": lookback_months,
            "min_months_required": min_months,
            "categories": by_category,
            "estimated_total": estimated_total,
            "item_count": len(recurring_items),
        })