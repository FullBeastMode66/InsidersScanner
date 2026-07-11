"""
Insider / Politician Scanner — Dashboard
Run with:  streamlit run dashboard.py
"""

import sqlite3
import pandas as pd
import streamlit as st

from scanner import DB_PATH, init_db, run_once, MIN_ALERT_SCORE

st.set_page_config(page_title="Insider / Politician Scanner", layout="wide")

st.title("📡 Insiders & Politician Buy Scanner")
st.caption("SEC Form 4 insider buys + congressional trade disclosures, scored 0-100 like a breakout scanner.")

init_db()

col1, col2, col3 = st.columns([1, 1, 3])
with col1:
    if st.button("🔄 Run scan now"):
        with st.spinner("Scanning SEC + congressional data..."):
            run_once()
        st.success("Scan complete")
with col2:
    min_score = st.number_input("Min score", min_value=0, max_value=100, value=MIN_ALERT_SCORE, step=5)

conn = sqlite3.connect(DB_PATH)
try:
    df = pd.read_sql_query("SELECT * FROM signals ORDER BY score DESC, seen_at DESC", conn)
finally:
    conn.close()

if df.empty:
    st.info("No signals yet. Click **Run scan now** to pull the latest data.")
else:
    df = df[df["score"] >= min_score]

    source_filter = st.multiselect("Source", options=sorted(df["source"].unique()), default=list(df["source"].unique()))
    df = df[df["source"].isin(source_filter)]

    ticker_search = st.text_input("Filter by ticker (optional)")
    if ticker_search:
        df = df[df["ticker"].str.contains(ticker_search.upper(), na=False)]

    st.write(f"**{len(df)} signals**")

    st.dataframe(
        df[["score", "source", "ticker", "person", "role", "action", "value",
            "trade_date", "filed_date", "reasons", "url"]],
        column_config={
            "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
            "url": st.column_config.LinkColumn("Filing"),
        },
        hide_index=True,
        use_container_width=True,
        height=600,
    )

st.divider()
st.caption(
    "Data sources: SEC EDGAR current Form 4 feed (free, no key) · "
    "House Stock Watcher & Senate Stock Watcher datasets (free, no key). "
    "Politician trades are disclosed after the fact (STOCK Act allows up to 45 days) — "
    "this is not a real-time feed of politician trading, only of when it becomes public."
)
