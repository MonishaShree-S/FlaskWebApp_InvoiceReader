import streamlit as st
import queries as q
from db import get_connection
import plotly.express as px

st.set_page_config(page_title="Grocery Analytics Dashboard", layout="wide")

st.title("📊 Grocery Analytics Dashboard")

conn = get_connection()

st.sidebar.header("Filters")

user_id = st.sidebar.text_input("User ID", value="4")
year = st.sidebar.selectbox("Year", q.get_years(conn, user_id))
month = st.sidebar.selectbox(
    "Month",
    ["All"] + [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
)
shop = st.sidebar.selectbox("Shop", ["All"] + q.get_shops(conn, user_id))

selected_year = None if year == "All" else year
selected_month = None if month == "All" else month.lower()
selected_shop = None if shop == "All" else shop

st.subheader("📈 Overview")

overview = q.get_overview(
    conn,
    user_id=user_id,
    year=selected_year,
    month=selected_month,
    shop=selected_shop
)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Spent (€)", f"{overview['total_spent']:.2f}")
col2.metric("Filtered Bills", overview["bill_count"])
col4.metric("All Bills", q.get_bill_count(conn, user_id))
col3.metric("Top Shop", overview["top_shop"] or "—")

st.subheader("📅 Monthly Spending")

df_monthly = q.get_monthly_spending(
    conn,
    user_id=user_id,
    year=selected_year,
    shop=selected_shop
)

if not df_monthly.empty:
    fig = px.bar(
        df_monthly,
        x="month_label",
        y="total",
        text_auto=".2f",
    )

    fig.update_layout(
        xaxis_title="Month",
        yaxis_title="Total Spending (€)",
        xaxis=dict(categoryorder="array"),
        height=500
    )

    st.plotly_chart(fig, use_container_width=True)

else:
    st.info("No monthly spending data found.")

st.subheader("🛒 Frequently Bought Items")

df_items = q.get_frequent_items(
    conn,
    user_id=user_id,
    year=selected_year,
    month=selected_month,
    shop=selected_shop
)

if not df_items.empty:
    st.dataframe(df_items, use_container_width=True)
else:
    st.info("No item data found.")