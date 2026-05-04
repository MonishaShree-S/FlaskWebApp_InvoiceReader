import pandas as pd

MONTH_ORDER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _add_filters(query, params, user_id=None, year=None, month=None, shop=None):
    if user_id:
        query += " AND userid = ?"
        params.append(int(user_id))

    if year:
        query += " AND year = ?"
        params.append(int(year))

    if month:
        query += " AND LOWER(month) = ?"
        params.append(month.lower())

    if shop:
        query += " AND shopName = ?"
        params.append(shop)

    return query, params


def get_years(conn, user_id=None):
    query = """
        SELECT DISTINCT year
        FROM groceries
        WHERE year IS NOT NULL
    """
    params = []

    if user_id:
        query += " AND userid = ?"
        params.append(int(user_id))

    query += " ORDER BY year DESC"

    df = pd.read_sql(query, conn, params=params)
    years = [str(int(y)) for y in df["year"].dropna().tolist()]
    return ["All"] + years


def get_shops(conn, user_id=None):
    query = """
        SELECT DISTINCT shopName
        FROM groceries
        WHERE shopName IS NOT NULL
    """
    params = []

    if user_id:
        query += " AND userid = ?"
        params.append(int(user_id))

    query += " ORDER BY shopName"

    df = pd.read_sql(query, conn, params=params)
    return df["shopName"].dropna().tolist()


def get_overview(conn, user_id=None, year=None, month=None, shop=None):
    base_query = """
        FROM groceries
        WHERE 1=1
    """
    params = []

    base_query, params = _add_filters(
        base_query,
        params,
        user_id=user_id,
        year=year,
        month=month,
        shop=shop
    )

    total_query = f"""
        SELECT 
            COALESCE(SUM(COALESCE(total_price, price)), 0) AS total_spent,
            COUNT(DISTINCT billId) AS bill_count
        {base_query}
    """

    total_df = pd.read_sql(total_query, conn, params=params)

    top_shop_query = f"""
        SELECT TOP 1
            shopName,
            COALESCE(SUM(COALESCE(total_price, price)), 0) AS total
        {base_query}
        GROUP BY shopName
        ORDER BY total DESC
    """

    top_shop_df = pd.read_sql(top_shop_query, conn, params=params)

    return {
        "total_spent": float(total_df.iloc[0]["total_spent"] or 0),
        "bill_count": int(total_df.iloc[0]["bill_count"] or 0),
        "top_shop": None if top_shop_df.empty else top_shop_df.iloc[0]["shopName"],
    }


def get_monthly_spending(conn, user_id=None, year=None, shop=None):
    query = """
        SELECT
            year,
            LOWER(month) AS month,
            COALESCE(SUM(COALESCE(total_price, price)), 0) AS total
        FROM groceries
        WHERE 1=1
    """
    params = []

    query, params = _add_filters(
        query,
        params,
        user_id=user_id,
        year=year,
        shop=shop
    )

    query += """
        GROUP BY year, LOWER(month)
    """

    df = pd.read_sql(query, conn, params=params)

    if df.empty:
        return df

    df["month_number"] = df["month"].map(MONTH_ORDER)

    df = df.dropna(subset=["month_number"])

    df["year"] = df["year"].astype(int)
    df["month_number"] = df["month_number"].astype(int)

    df["month_date"] = pd.to_datetime(
        df["year"].astype(str)
        + "-"
        + df["month_number"].astype(str)
        + "-01"
    )

    df = df.sort_values("month_date")

    df["month_label"] = df["month_date"].dt.strftime("%Y-%b")

    return df


def get_frequent_items(conn, user_id=None, year=None, month=None, shop=None):
    query = """
        SELECT TOP 20
            item,
            COUNT(*) AS purchase_count,
            COALESCE(SUM(quantity), 0) AS total_quantity,
            COALESCE(SUM(price), 0) AS total_spent,
            COALESCE(AVG(price), 0) AS avg_price
        FROM groceries
        WHERE 1=1
    """
    params = []

    query, params = _add_filters(
        query,
        params,
        user_id=user_id,
        year=year,
        month=month,
        shop=shop
    )

    query += """
        GROUP BY item
        ORDER BY purchase_count DESC, total_spent DESC
    """

    df = pd.read_sql(query, conn, params=params)

    if not df.empty:
        money_cols = ["total_spent", "avg_price"]
        for col in money_cols:
            df[col] = df[col].round(2)

    return df

def get_bill_count(conn, user_id=None):
    query = """
        SELECT COUNT(DISTINCT billId) AS count
        FROM groceries
        WHERE 1=1
    """
    params = []

    if user_id:
        query += " AND userid = ?"
        params.append(int(user_id))

    df = pd.read_sql(query, conn, params=params)
    return int(df.iloc[0]["count"] or 0)