from __future__ import annotations

from datetime import datetime, date, timedelta
from functools import lru_cache
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

TZ = ZoneInfo("Asia/Taipei")
OPENAPI = "https://openapi.twse.com.tw/v1"
TWSE = "https://www.twse.com.tw"

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        )
    }
)

st.set_page_config(page_title="下單前檢查表", layout="wide")

st.markdown(
    """
<style>
.block-container { padding-top: 1.2rem; }
.status-box {
    border-radius: 18px;
    padding: 18px 20px;
    color: white;
    font-weight: 700;
    font-size: 1.15rem;
    box-shadow: 0 8px 24px rgba(0,0,0,0.10);
    margin-bottom: 12px;
}
.status-green { background: linear-gradient(135deg, #16a34a, #22c55e); }
.status-yellow { background: linear-gradient(135deg, #d97706, #f59e0b); }
.status-red { background: linear-gradient(135deg, #dc2626, #ef4444); }
.metric-card {
    border-radius: 16px;
    padding: 16px 16px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}
.small-label { font-size: 0.85rem; color: #64748b; margin-bottom: 4px; }
.big-value { font-size: 1.35rem; font-weight: 800; color: #0f172a; }
.good { color: #16a34a; font-weight: 700; }
.warn { color: #d97706; font-weight: 700; }
.bad  { color: #dc2626; font-weight: 700; }
</style>
""",
    unsafe_allow_html=True,
)

def to_float(x):
    if x is None:
        return None
    s = str(x).strip()
    if s in ("", "--", "-", "None", "nan", "NaN"):
        return None
    s = s.replace(",", "").replace("%", "").replace("+", "")
    try:
        return float(s)
    except Exception:
        return None

def fmt_price(x):
    if x is None or pd.isna(x):
        return "-"
    return f"{x:,.2f}"

def fmt_num(x, nd=1):
    if x is None or pd.isna(x):
        return "-"
    try:
        return f"{x:,.{nd}f}"
    except Exception:
        return str(x)

def fmt_int(x):
    if x is None or pd.isna(x):
        return "-"
    try:
        return f"{int(round(float(x))):,}"
    except Exception:
        return str(x)

def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df

def get_by_keywords(row: dict, include: list[str], exclude: list[str] | None = None):
    exclude = exclude or []
    for k, v in row.items():
        ks = str(k)
        if all(t in ks for t in include) and not any(e in ks for e in exclude):
            return v
    return None

def find_code_row(df: pd.DataFrame, code: str):
    if df is None or df.empty:
        return None

    df = clean_columns(df)
    code = str(code).strip()

    preferred_cols = [
        c for c in df.columns
        if any(t in str(c) for t in ["證券代號", "股票代號", "證券代碼", "代號"])
    ]
    for col in preferred_cols:
        try:
            hit = df[df[col].astype(str).str.strip() == code]
            if not hit.empty:
                return hit.iloc[0]
        except Exception:
            pass

    for col in df.columns:
        try:
            hit = df[df[col].astype(str).str.strip() == code]
            if not hit.empty:
                return hit.iloc[0]
        except Exception:
            pass

    return None

def roc_year(greg_date: date) -> int:
    return greg_date.year - 1911

@st.cache_data(ttl=24 * 3600)
def get_holiday_set(roc_year_num: int) -> set[date]:
    url = f"{TWSE}/holidaySchedule/holidaySchedule?queryYear={roc_year_num}&response=html"
    try:
        tables = pd.read_html(url)
        if not tables:
            return set()
        df = clean_columns(tables[0])
        date_col = None
        for c in df.columns:
            if "日期" in str(c):
                date_col = c
                break
        if date_col is None:
            date_col = df.columns[0]
        dates = pd.to_datetime(df[date_col], errors="coerce").dt.date
        return set([d for d in dates if pd.notna(d)])
    except Exception:
        return set()

def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in get_holiday_set(roc_year(d))

@st.cache_data(ttl=3600)
def fetch_stock_day_all_df(d: date) -> pd.DataFrame:
    try:
        r = SESSION.get(
            f"{OPENAPI}/exchangeReport/STOCK_DAY_ALL",
            params={"date": d.strftime("%Y%m%d")},
            timeout=20,
        )
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and "data" in j and "fields" in j:
            return clean_columns(pd.DataFrame(j["data"], columns=j["fields"]))
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def fetch_bwibbu_df(d: date) -> pd.DataFrame:
    for endpoint in ["BWIBBU_d", "BWIBBU_ALL"]:
        try:
            r = SESSION.get(
                f"{OPENAPI}/exchangeReport/{endpoint}",
                params={"date": d.strftime("%Y%m%d")} if endpoint == "BWIBBU_d" else None,
                timeout=20,
            )
            r.raise_for_status()
            j = r.json()
            if isinstance(j, dict) and "data" in j and "fields" in j:
                df = clean_columns(pd.DataFrame(j["data"], columns=j["fields"]))
                if not df.empty:
                    return df
        except Exception:
            continue
    return pd.DataFrame()

def _parse_t86_json_to_row(d: date, code: str):
    urls = [
        (f"{TWSE}/fund/T86", {"date": d.strftime("%Y%m%d"), "response": "json", "selectType": "ALLBUT0999"}),
        (f"{TWSE}/rwd/zh/fund/T86", {"date": d.strftime("%Y%m%d"), "response": "json", "selectType": "ALLBUT0999"}),
    ]
    for url, params in urls:
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            j = r.json()
            if isinstance(j, dict) and "data" in j and "fields" in j:
                df = clean_columns(pd.DataFrame(j["data"], columns=j["fields"]))
                row = find_code_row(df, code)
                if row is not None:
                    return row.to_dict()
        except Exception:
            continue
    return None

def _parse_t86_html_to_row(d: date, code: str):
    urls = [
        (f"{TWSE}/fund/T86", {"date": d.strftime("%Y%m%d"), "response": "html", "selectType": "ALLBUT0999"}),
        (f"{TWSE}/rwd/zh/fund/T86", {"date": d.strftime("%Y%m%d"), "response": "html", "selectType": "ALLBUT0999"}),
    ]
    for url, params in urls:
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            tables = pd.read_html(StringIO(r.text))
            for tb in tables:
                tb = clean_columns(tb)
                row = find_code_row(tb, code)
                if row is not None:
                    return row.to_dict()
        except Exception:
            continue
    return None

@st.cache_data(ttl=1800)
def fetch_t86_row(d: date, code: str):
    row = _parse_t86_json_to_row(d, code)
    if row is not None:
        return row
    return _parse_t86_html_to_row(d, code)

@st.cache_data(ttl=1800)
def fetch_stock_row(d: date, code: str):
    df = fetch_stock_day_all_df(d)
    if df.empty:
        return None
    row = find_code_row(df, code)
    return None if row is None else row.to_dict()

@st.cache_data(ttl=1800)
def fetch_bwibbu_row(d: date, code: str):
    df = fetch_bwibbu_df(d)
    if df.empty:
        return None
    row = find_code_row(df, code)
    return None if row is None else row.to_dict()

def get_verified_stock_days(code: str, needed: int, end_day: date) -> list[dict]:
    out = []
    cursor = end_day
    tries = 0

    while len(out) < needed and tries < 240:
        tries += 1
        if is_trading_day(cursor):
            row = fetch_stock_row(cursor, code)
            if row is not None:
                out.append({"date": cursor, "stock": row})
        cursor -= timedelta(days=1)

    return list(reversed(out))

def get_verified_inst_days(code: str, stock_days: list[dict], needed: int = 5) -> list[dict]:
    out = []
    for item in reversed(stock_days):
        row = fetch_t86_row(item["date"], code)
        if row is not None:
            out.append({"date": item["date"], "stock": item["stock"], "t86": row})
            if len(out) >= needed:
                break
    return list(reversed(out))

def get_latest_valuation(code: str, stock_days: list[dict]):
    for item in reversed(stock_days):
        row = fetch_bwibbu_row(item["date"], code)
        if row is not None:
            return item["date"], row
    return None, None

def normalize_stock_row(d: date, row: dict):
    code = (
        get_by_keywords(row, ["證券代號"])
        or get_by_keywords(row, ["股票代號"])
        or get_by_keywords(row, ["代號"])
        or ""
    )
    name = (
        get_by_keywords(row, ["證券名稱"])
        or get_by_keywords(row, ["股票名稱"])
        or get_by_keywords(row, ["名稱"])
        or ""
    )

    return {
        "date": d,
        "code": str(code).strip(),
        "name": str(name).strip(),
        "open": to_float(get_by_keywords(row, ["開盤價"])),
        "high": to_float(get_by_keywords(row, ["最高價"])),
        "low": to_float(get_by_keywords(row, ["最低價"])),
        "close": to_float(get_by_keywords(row, ["收盤價"])),
        "volume": to_float(get_by_keywords(row, ["成交股數"])),
        "trades": to_float(get_by_keywords(row, ["成交筆數"])),
        "amount": to_float(get_by_keywords(row, ["成交金額"])),
        "change": get_by_keywords(row, ["漲跌價差"]) or get_by_keywords(row, ["漲跌"]) or "-",
    }

def normalize_val_row(row: dict):
    return {
        "pe": to_float(get_by_keywords(row, ["本益比"])),
        "dividend_yield": to_float(get_by_keywords(row, ["殖利率"])),
        "pb": to_float(get_by_keywords(row, ["股價淨值比"])),
    }

def normalize_t86_row(row: dict):
    foreign_buy = to_float(get_by_keywords(row, ["外資", "買進"]))
    foreign_sell = to_float(get_by_keywords(row, ["外資", "賣出"]))
    trust_buy = to_float(get_by_keywords(row, ["投信", "買進"]))
    trust_sell = to_float(get_by_keywords(row, ["投信", "賣出"]))
    dealer_buy = to_float(get_by_keywords(row, ["自營商", "買進"], exclude=["避險"]))
    dealer_sell = to_float(get_by_keywords(row, ["自營商", "賣出"], exclude=["避險"]))
    hedge_buy = to_float(get_by_keywords(row, ["自營商", "避險", "買進"]))
    hedge_sell = to_float(get_by_keywords(row, ["自營商", "避險", "賣出"]))
    total_diff = to_float(get_by_keywords(row, ["買賣超"]) or get_by_keywords(row, ["總差額"]))

    def to_lot(x):
        return None if x is None else x / 1000.0

    foreign_net = to_lot(foreign_buy - foreign_sell) if foreign_buy is not None and foreign_sell is not None else None
    trust_net = to_lot(trust_buy - trust_sell) if trust_buy is not None and trust_sell is not None else None

    dealer_total_buy = None
    dealer_total_sell = None
    dealer_net = None
    if dealer_buy is not None or hedge_buy is not None:
        dealer_total_buy = (dealer_buy or 0) + (hedge_buy or 0)
        dealer_total_sell = (dealer_sell or 0) + (hedge_sell or 0)
        dealer_net = to_lot(dealer_total_buy - dealer_total_sell)

    return {
        "foreign_buy_lot": to_lot(foreign_buy),
        "foreign_sell_lot": to_lot(foreign_sell),
        "foreign_net_lot": foreign_net,
        "trust_buy_lot": to_lot(trust_buy),
        "trust_sell_lot": to_lot(trust_sell),
        "trust_net_lot": trust_net,
        "dealer_buy_lot": to_lot(dealer_buy),
        "dealer_sell_lot": to_lot(dealer_sell),
        "hedge_buy_lot": to_lot(hedge_buy),
        "hedge_sell_lot": to_lot(hedge_sell),
        "dealer_total_buy_lot": to_lot(dealer_total_buy),
        "dealer_total_sell_lot": to_lot(dealer_total_sell),
        "dealer_net_lot": dealer_net,
        "total_diff_lot": to_lot(total_diff),
    }

def moving_average(values, n):
    out = []
    for i in range(len(values)):
        arr = values[max(0, i-n+1):i+1]
        arr = [v for v in arr if v is not None]
        out.append(sum(arr) / n if len(arr) == n else None)
    return out

st.title("📋 下單前檢查表")
st.caption("輸入股票代號，先看完重點再決定要不要下單。")

with st.sidebar:
    st.header("🔧 參數")
    stock_code = st.text_input("股票代號", value="2330")
    end_day = st.date_input("截至日期", value=datetime.now(TZ).date())
    stop_loss_pct = st.number_input("當沖停損 %", min_value=0.1, max_value=20.0, value=1.0, step=0.1)
    entry_price = st.number_input("預計進場價（可選）", min_value=0.0, value=0.0, step=0.5)
    st.caption("停損 % 只是風控參考值，不是官方規定。")

if st.button("開始檢查"):
    code = stock_code.strip()

    if not code:
        st.warning("請先輸入股票代號。")
        st.stop()

    with st.spinner("正在核對 TWSE 開休市日期、成交資料與法人資料..."):
        stock_days = get_verified_stock_days(code, needed=40, end_day=end_day)

    if len(stock_days) < 5:
        st.error("可驗證的交易日少於 5 天，請確認股票代號或稍後再試。")
        st.stop()

    stock_hist = [normalize_stock_row(item["date"], item["stock"]) for item in stock_days]
    hist_df = pd.DataFrame(stock_hist).sort_values("date").reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume", "trades", "amount"]:
        if col in hist_df.columns:
            hist_df[col] = pd.to_numeric(hist_df[col], errors="coerce")

    hist_df["MA5"] = hist_df["close"].rolling(5).mean()
    hist_df["MA10"] = hist_df["close"].rolling(10).mean()
    hist_df["MA20"] = hist_df["close"].rolling(20).mean()

    latest = hist_df.iloc[-1].to_dict()
    latest_date = latest["date"]
    latest_code = latest.get("code", code)
    latest_name = latest.get("name", "")

    val_date, val_row = get_latest_valuation(code, stock_days)
    val_dict = normalize_val_row(val_row) if val_row else {}

    inst_days = get_verified_inst_days(code, stock_days, needed=5)
    inst_rows = []
    for item in inst_days:
        inst_rows.append({"date": item["date"], **normalize_t86_row(item["t86"])})
    inst_df = pd.DataFrame(inst_rows).sort_values("date").reset_index(drop=True)

    score = 0
    score_items = []

    cond_ma = pd.notna(latest.get("close")) and pd.notna(latest.get("MA5")) and pd.notna(latest.get("MA20")) and latest["close"] >= latest["MA5"] >= latest["MA20"]
    cond_vol = len(hist_df) >= 20 and pd.notna(latest.get("volume")) and latest["volume"] >= hist_df["volume"].tail(20).mean()
    cond_foreign = (not inst_df.empty) and ("foreign_net_lot" in inst_df.columns) and inst_df["foreign_net_lot"].sum(skipna=True) > 0
    cond_trust = (not inst_df.empty) and ("trust_net_lot" in inst_df.columns) and inst_df["trust_net_lot"].sum(skipna=True) > 0
    cond_dealer = (not inst_df.empty) and ("dealer_net_lot" in inst_df.columns) and inst_df["dealer_net_lot"].sum(skipna=True) > 0

    for label, cond in [
        ("站上 MA5 / MA20", cond_ma),
        ("量能是否大於 20 日均量", cond_vol),
        ("近 5 日外資是否偏買", cond_foreign),
        ("近 5 日投信是否偏買", cond_trust),
        ("近 5 日自營商是否偏買", cond_dealer),
    ]:
        score_items.append((label, cond))
        score += 1 if cond else 0

    if score >= 4:
        status_class = "status-green"
        status_text = "🟢 偏多：條件相對有利，可再自己確認進出場節奏。"
    elif score >= 2:
        status_class = "status-yellow"
        status_text = "🟡 觀望：有些訊號，但還不到很漂亮。"
    else:
        status_class = "status-red"
        status_text = "🔴 偏空：條件較弱，先別急著追。"

    st.markdown(f'<div class="status-box {status_class}">{status_text}</div>', unsafe_allow_html=True)

    st.subheader("🔍 一眼檢查")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("股票代號", str(latest_code))
    c2.metric("股票名稱", str(latest_name))
    c3.metric("收盤價", fmt_price(latest.get("close")))
    c4.metric("漲跌", str(latest.get("change", "-")))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("成交量(股)", fmt_int(latest.get("volume")))
    c6.metric("MA5", fmt_price(latest.get("MA5")))
    c7.metric("MA10", fmt_price(latest.get("MA10")))
    c8.metric("MA20", fmt_price(latest.get("MA20")))

    c9, c10, c11 = st.columns(3)
    c9.metric("本益比", fmt_num(val_dict.get("pe")))
    c10.metric("股價淨值比", fmt_num(val_dict.get("pb")))
    c11.metric("殖利率(%)", fmt_num(val_dict.get("dividend_yield")))

    st.write("### ✅ 快速判讀")
    for label, cond in score_items:
        st.write(f"- {'🟢' if cond else '🔴'} {label}")

    st.write("### 📊 近 20 個已驗證交易日資料")
    show_hist = hist_df[["date", "close", "volume", "MA5", "MA10", "MA20"]].copy()
    show_hist["date"] = show_hist["date"].astype(str)
    show_hist = show_hist.rename(
        columns={
            "date": "交易日",
            "close": "收盤價",
            "volume": "成交股數",
            "MA5": "MA5",
            "MA10": "MA10",
            "MA20": "MA20",
        }
    )
    st.dataframe(show_hist, use_container_width=True)

    st.write("### 🧾 估值資料")
    if val_dict:
        val_show = pd.DataFrame(
            [
                {
                    "最新估值交易日": str(val_date),
                    "本益比": val_dict.get("pe"),
                    "股價淨值比": val_dict.get("pb"),
                    "殖利率(%)": val_dict.get("dividend_yield"),
                }
            ]
        )
        st.dataframe(val_show, use_container_width=True)
    else:
        st.info("目前抓不到估值資料，可能該標的當天尚未更新或資料源暫時沒有回傳。")

    st.write("### 🏦 前五個營業日法人買賣張數（張）")
    if not inst_df.empty:
        inst_show = inst_df.copy()
        inst_show["date"] = inst_show["date"].astype(str)
        inst_show = inst_show.rename(
            columns={
                "date": "交易日",
                "foreign_buy_lot": "外資買進",
                "foreign_sell_lot": "外資賣出",
                "foreign_net_lot": "外資買賣超",
                "trust_buy_lot": "投信買進",
                "trust_sell_lot": "投信賣出",
                "trust_net_lot": "投信買賣超",
                "dealer_buy_lot": "自營商買進",
                "dealer_sell_lot": "自營商賣出",
                "hedge_buy_lot": "自營商避險買進",
                "hedge_sell_lot": "自營商避險賣出",
                "dealer_total_buy_lot": "自營商總買進",
                "dealer_total_sell_lot": "自營商總賣出",
                "dealer_net_lot": "自營商買賣超",
                "total_diff_lot": "總差額",
            }
        )
        st.dataframe(inst_show, use_container_width=True)
    else:
        st.info("目前抓不到法人資料。")

    st.write("### 🧯 當沖底線試算")
    if entry_price and entry_price > 0:
        stop_price = entry_price * (1 - stop_loss_pct / 100.0)
        st.write(f"- 預計進場價：`{entry_price:.2f}`")
        st.write(f"- 停損 {stop_loss_pct:.1f}% 後的底線：約 `{stop_price:.2f}`")
    elif pd.notna(latest.get("close")) and latest.get("close") is not None:
        base = float(latest["close"])
        stop_price = base * (1 - stop_loss_pct / 100.0)
        st.write(f"- 以最新收盤價 `{base:.2f}` 當參考")
        st.write(f"- 停損 {stop_loss_pct:.1f}% 後的底線：約 `{stop_price:.2f}`")
    else:
        st.info("目前沒有足夠價格資料可試算停損底線。")

    st.caption("這裡的停損是風控參考值，不是官方規定。")

    st.write("### 🧪 資料核對說明")
    st.write(f"- 已驗證的交易日數：`{len(stock_days)}`")
    st.write(f"- 最新股票資料日：`{str(latest_date)}`")
    if val_date is not None:
        st.write(f"- 最新估值資料日：`{str(val_date)}`")
    if not inst_df.empty:
        st.write(f"- 最新法人資料日：`{str(inst_df.iloc[-1]['date'])}`")

    csv_hist = show_hist.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ 下載近 20 日檢查表 CSV",
        data=csv_hist,
        file_name=f"{latest_code}_checklist.csv",
        mime="text/csv",
    )

    if not inst_df.empty:
        csv_inst = inst_show.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ 下載法人資料 CSV",
            data=csv_inst,
            file_name=f"{latest_code}_institution.csv",
            mime="text/csv",
        )
