"""
app.py — Single-day Top View heatmap for Streamlit Community Cloud.
Auth: Microsoft device code flow — users log in with their company account.

Streamlit secrets required (Settings → Secrets):
    AAD_CLIENT_ID = "your-app-registration-client-id"
    AAD_TENANT_ID = "your-azure-tenant-id"

Usage (local):
    streamlit run streamlit_cloud/app.py
"""

import os, sys, struct, threading
from datetime import date

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import polars as pl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import greedy_match, PROPS, PROP_LABELS, N_BINS_DEFAULT

# ── Synapse / Azure config ────────────────────────────────────────────────────
SERVER   = "saw-spf-prod-weu-ondemand.sql.azuresynapse.net"
DATABASE = "PowerBiViewsDB"
DRIVER   = "ODBC Driver 18 for SQL Server"
SQL_COPT_SS_ACCESS_TOKEN = 1256

TABLE_COUNT  = 1
TABLE_SELECT = 5
BATCH_SIZE   = 100_000

AAD_CLIENT_ID = st.secrets.get("AAD_CLIENT_ID", os.environ.get("AAD_CLIENT_ID", ""))
AAD_TENANT_ID = st.secrets.get("AAD_TENANT_ID", os.environ.get("AAD_TENANT_ID", ""))
AAD_SCOPES    = ["https://database.windows.net/user_impersonation"]

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN_DARK = "#2E7D32"
BG_PAGE    = "#F0F0F0"
BG_CARD    = "#FFFFFF"
TEXT_DARK  = "#212121"
TEXT_MUTED = "#757575"
LOGO_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meggsius_connect_logo.png")

HEATMAP_SCALE = [
    [0.00, "#0D47A1"],
    [0.30, "#42A5F5"],
    [0.55, "#E3F2FD"],
    [0.75, "#EF5350"],
    [1.00, "#B71C1C"],
]

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Top View — Live",
    page_icon="🥚",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');
html, body, [class*="css"] {{
    font-family: 'Roboto', sans-serif;
    font-size: 14px;
    background-color: {BG_PAGE};
    color: {TEXT_DARK};
}}
[data-testid="stSidebar"] {{
    background-color: {GREEN_DARK} !important;
    min-width: 240px !important;
    max-width: 240px !important;
}}
[data-testid="stSidebar"] * {{ color: white !important; }}
[data-testid="stSidebar"] input {{
    color: {TEXT_DARK} !important;
    background-color: white !important;
}}
[data-testid="stSidebar"] .stNumberInput input,
[data-testid="stSidebar"] .stDateInput input {{
    color: {TEXT_DARK} !important;
    background-color: white !important;
}}
.card {{ background:{BG_CARD}; border-radius:8px; box-shadow:0 1px 4px rgba(0,0,0,0.12); margin-bottom:16px; overflow:hidden; }}
.card-header {{ background:{GREEN_DARK}; color:white; padding:0 16px; height:40px; display:flex; align-items:center; font-weight:600; font-size:14px; }}
.card-body {{ padding:16px; }}
.metric-tile {{ background:{BG_CARD}; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.10); padding:14px 16px; text-align:center; }}
.metric-value {{ font-size:26px; font-weight:700; color:{GREEN_DARK}; line-height:1.2; }}
.metric-label {{ font-size:11px; color:{TEXT_MUTED}; margin-top:4px; font-weight:500; text-transform:uppercase; letter-spacing:0.5px; }}
.house-label {{ font-size:12px; font-weight:600; color:{TEXT_MUTED}; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:2px; margin-top:12px; }}
.house-total {{ font-size:13px; font-weight:500; color:{TEXT_DARK}; margin-bottom:3px; }}
.breadcrumb {{ font-size:14px; font-weight:500; color:{TEXT_DARK}; padding:10px 0 6px 0; }}
.breadcrumb .sep {{ color:{TEXT_MUTED}; margin:0 6px; }}
#MainMenu {{ visibility:hidden; }} footer {{ visibility:hidden; }} header {{ visibility:hidden; }}
</style>
""", unsafe_allow_html=True)


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _msal_app():
    import msal
    return msal.PublicClientApplication(
        AAD_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AAD_TENANT_ID}",
    )


def _token_to_bytes(token_str: str) -> bytes:
    b = token_str.encode("utf-16-le")
    return struct.pack("<I", len(b)) + b


# ── Auth gate ─────────────────────────────────────────────────────────────────
if not AAD_CLIENT_ID or not AAD_TENANT_ID:
    st.error(
        "**App not configured.**\n\n"
        "Add `AAD_CLIENT_ID` and `AAD_TENANT_ID` to Streamlit secrets."
    )
    st.stop()

if "token" not in st.session_state:
    st.session_state.token       = None
if "device_flow" not in st.session_state:
    st.session_state.device_flow = None

# Try silent token refresh first (uses MSAL in-memory cache)
if st.session_state.token is None:
    try:
        app_msal = _msal_app()
        accounts = app_msal.get_accounts()
        if accounts:
            result = app_msal.acquire_token_silent(AAD_SCOPES, account=accounts[0])
            if result and "access_token" in result:
                st.session_state.token = result["access_token"]
    except Exception:
        pass

if st.session_state.token is None:
    # ── Login screen ──────────────────────────────────────────────────────────
    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        if os.path.exists(LOGO_PATH):
            st.image(LOGO_PATH, use_container_width=True)
        st.markdown("## Sign in")
        st.markdown("Use your **company Microsoft account** to access the dashboard.")

        app_msal = _msal_app()

        if st.session_state.device_flow is None:
            flow = app_msal.initiate_device_flow(scopes=AAD_SCOPES)
            if "user_code" not in flow:
                st.error(f"Could not start login: {flow.get('error_description')}")
                st.stop()
            st.session_state.device_flow = flow

        flow = st.session_state.device_flow
        st.info(
            f"**Step 1** — Open this link in your browser:\n\n"
            f"### [{flow['verification_uri']}]({flow['verification_uri']})\n\n"
            f"**Step 2** — Enter code:  `{flow['user_code']}`\n\n"
            f"**Step 3** — Sign in with your company account\n\n"
            f"**Step 4** — Click **Done** below"
        )

        if st.button("✓  Done — I've signed in", type="primary", use_container_width=True):
            result_holder: dict = {}

            def _acquire():
                result_holder["r"] = app_msal.acquire_token_by_device_flow(flow)

            t = threading.Thread(target=_acquire)
            t.start()
            t.join(timeout=15)

            if "r" not in result_holder:
                st.warning("Still waiting — please complete sign-in in your browser first, then click Done again.")
            elif "access_token" in result_holder["r"]:
                st.session_state.token       = result_holder["r"]["access_token"]
                st.session_state.device_flow = None
                st.rerun()
            else:
                err = result_holder["r"].get("error_description", "Unknown error")
                st.error(f"Login failed: {err}")
                st.session_state.device_flow = None

        st.stop()

token = st.session_state.token


# ── Synapse helpers ───────────────────────────────────────────────────────────
def _get_connection(token_str: str):
    import pyodbc
    token_bytes = _token_to_bytes(token_str)
    conn_str = (
        f"DRIVER={{{DRIVER}}};"
        f"SERVER=tcp:{SERVER},1433;"
        f"DATABASE={DATABASE};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120;"
        "MARS_Connection=yes;"
    )
    conn = pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_bytes})
    conn.timeout = 1800
    return conn


def _sql_count_day(account_id: int, day: date) -> str:
    d  = day.strftime("%Y-%m-%d")
    dn = (day.replace(day=day.day + 1) if day.day < 28
          else date(day.year + (day.month // 12), (day.month % 12) + 1, 1)
         ).strftime("%Y-%m-%d")
    return f"""
SELECT r.AccountId, r.HouseIdSet, r.BestUtcDateTime, r.OffsetMinutes,
       r.linenmbr, r.distancedonepercent, r.eggsincrease
FROM OPENROWSET(
    BULK '/{TABLE_COUNT}/PartitionKey={day.year}-{day.month:02d}-*/*.parquet',
    DATA_SOURCE = 'ArchiveDataLake', FORMAT = 'PARQUET'
) AS r
WHERE r.BestLocalDateTime >= '{d}'
  AND r.BestLocalDateTime <  '{dn}'
  AND r.AccountId = {account_id}
  AND TRY_CAST(r.eggsincrease AS float) > 0
  AND r.linenmbr IS NOT NULL
"""


def _sql_select_day(account_id: int, day: date) -> str:
    d  = day.strftime("%Y-%m-%d")
    dn = (day.replace(day=day.day + 1) if day.day < 28
          else date(day.year + (day.month // 12), (day.month % 12) + 1, 1)
         ).strftime("%Y-%m-%d")
    return f"""
SELECT r.AccountId, r.HouseIdSet, r.BestUtcDateTime, r.OffsetMinutes,
       r.selectv2_lane, r.selectv2_volume,
       r.selectv2_isrejectedonmanure, r.selectv2_isrejectedonblood,
       r.selectv2_isrejectedoncrack,
       r.selectv2_isrejectedongroupdirt, r.selectv2_isrejectedongroupdamage
FROM OPENROWSET(
    BULK '/{TABLE_SELECT}/PartitionKey={day.year}-{day.month:02d}-*/*.parquet',
    DATA_SOURCE = 'ArchiveDataLake', FORMAT = 'PARQUET'
) AS r
WHERE r.BestLocalDateTime >= '{d}'
  AND r.BestLocalDateTime <  '{dn}'
  AND r.AccountId = {account_id}
  AND r.selectv2_lane IS NOT NULL
"""


def _fetch(conn, sql: str) -> pl.DataFrame | None:
    batches = pl.read_database(query=sql, connection=conn,
                               iter_batches=True, batch_size=BATCH_SIZE)
    parts = [b for b in batches if not b.is_empty()]
    return pl.concat(parts) if parts else None


def _transform_count(raw: pl.DataFrame) -> pl.DataFrame:
    raw = raw.with_columns([
        pl.col("eggsincrease").cast(pl.Float64, strict=False),
        pl.col("linenmbr").cast(pl.Float64, strict=False),
        pl.col("distancedonepercent").cast(pl.Float64, strict=False),
        pl.col("OffsetMinutes").cast(pl.Float64, strict=False),
    ]).filter(
        pl.col("eggsincrease").is_not_null() &
        pl.col("linenmbr").is_not_null() &
        (pl.col("eggsincrease") > 0)
    )
    if raw.is_empty():
        return pl.DataFrame()
    offset = float(raw["OffsetMinutes"].drop_nulls()[0])
    raw = raw.with_columns(
        pl.col("BestUtcDateTime")
          .str.replace("Z", "", literal=True).str.replace("T", " ", literal=True)
          .str.slice(0, 19)
          .str.strptime(pl.Datetime("us"), format="%Y-%m-%d %H:%M:%S", strict=False)
          .dt.offset_by(f"{int(offset)}m").alias("_local_ts")
    )
    return raw.select([
        pl.col("_local_ts").dt.date().alias("Date"),
        (pl.col("_local_ts").dt.hour().cast(pl.Int64) * 3600 +
         pl.col("_local_ts").dt.minute().cast(pl.Int64) * 60 +
         pl.col("_local_ts").dt.second().cast(pl.Int64)).cast(pl.Float64).alias("Time"),
        pl.col("HouseIdSet").cast(pl.Utf8).str.strip_chars().alias("House"),
        pl.col("linenmbr").cast(pl.Int64).alias("Line"),
        pl.col("distancedonepercent").alias("Position"),
        pl.col("eggsincrease").cast(pl.Int64).alias("Eggs"),
    ]).drop_nulls()


def _transform_select(raw: pl.DataFrame) -> pl.DataFrame:
    flag_map = {
        "selectv2_isrejectedonmanure":      "p_Manure",
        "selectv2_isrejectedonblood":       "p_Blood",
        "selectv2_isrejectedoncrack":       "p_Crack",
        "selectv2_isrejectedongroupdirt":   "p_Dirt",
        "selectv2_isrejectedongroupdamage": "p_Damaged",
    }
    raw = raw.with_columns([
        pl.col("selectv2_lane").cast(pl.Float64, strict=False),
        pl.col("selectv2_volume").cast(pl.Float64, strict=False),
        pl.col("OffsetMinutes").cast(pl.Float64, strict=False),
    ]).filter(pl.col("selectv2_lane").is_not_null())
    if raw.is_empty():
        return pl.DataFrame()
    offset = float(raw["OffsetMinutes"].drop_nulls()[0])
    raw = raw.with_columns(
        pl.col("BestUtcDateTime")
          .str.replace("Z", "", literal=True).str.replace("T", " ", literal=True)
          .str.slice(0, 19)
          .str.strptime(pl.Datetime("us"), format="%Y-%m-%d %H:%M:%S", strict=False)
          .dt.offset_by(f"{int(offset)}m").alias("_local_ts")
    )
    flag_exprs = [
        (pl.col(src).cast(pl.Float64, strict=False) == 1.0).fill_null(False).alias(dst)
        if src in raw.columns else pl.lit(False).alias(dst)
        for src, dst in flag_map.items()
    ]
    raw = raw.with_columns(flag_exprs)
    return raw.select([
        pl.col("_local_ts").dt.date().alias("Date"),
        (pl.col("_local_ts").dt.hour().cast(pl.Int64) * 3600 +
         pl.col("_local_ts").dt.minute().cast(pl.Int64) * 60 +
         pl.col("_local_ts").dt.second().cast(pl.Int64)).cast(pl.Float64).alias("Time"),
        pl.col("HouseIdSet").cast(pl.Utf8).str.strip_chars().alias("House"),
        pl.col("selectv2_lane").cast(pl.Int64).alias("Lane"),
        pl.col("selectv2_volume").alias("Volume"),
        pl.col("p_Manure"), pl.col("p_Blood"), pl.col("p_Crack"),
        pl.col("p_Dirt"),   pl.col("p_Damaged"),
    ]).drop_nulls(subset=["Time", "Lane"])


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_day(account_id: int, day_str: str, _token: str):
    """Fetch one day from Synapse. Creates and closes its own connection."""
    day  = date.fromisoformat(day_str)
    conn = _get_connection(_token)
    try:
        raw_c = _fetch(conn, _sql_count_day(account_id, day))
        raw_s = _fetch(conn, _sql_select_day(account_id, day))
    finally:
        conn.close()
    dc = _transform_count(raw_c).to_pandas()  if raw_c is not None else None
    ds = _transform_select(raw_s).to_pandas() if raw_s is not None else None
    return dc, ds


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, use_container_width=True)
    else:
        st.markdown("### 🥚 Meggsius Connect")

    st.markdown("---")
    account_id = int(st.number_input("Account ID", min_value=1, value=68, step=1))

    st.markdown("---")
    sel_date = st.date_input("Date", value=date.today())

    st.markdown("---")
    n_bins    = st.slider("Position bins", 5, 30, N_BINS_DEFAULT, 1)
    normalize = st.toggle("Normalize per day", value=False)
    combined  = st.toggle("Houses combined", value=False)
    sel_prop  = st.selectbox("Rejection type", options=PROPS,
                             format_func=lambda p: PROP_LABELS[p])

    st.markdown("---")
    if st.button("Sign out", use_container_width=True):
        st.session_state.token       = None
        st.session_state.device_flow = None
        st.rerun()


# ── Fetch data ────────────────────────────────────────────────────────────────
st.markdown(
    f'<div class="breadcrumb">'
    f'Production <span class="sep" style="color:#757575;margin:0 6px;">›</span> Account {account_id} '
    f'<span class="sep" style="color:#757575;margin:0 6px;">›</span> Top View '
    f'<span class="sep" style="color:#757575;margin:0 6px;">›</span> <b>{sel_date}</b>'
    f'</div>',
    unsafe_allow_html=True,
)

with st.spinner(f"Querying Synapse for {sel_date}…"):
    try:
        dc, ds = fetch_day(account_id, str(sel_date), token)
    except Exception as e:
        err_str = str(e)
        if "token" in err_str.lower() or "login" in err_str.lower() or "401" in err_str:
            st.session_state.token = None
            st.error("Session expired — please sign in again.")
            st.rerun()
        st.error(f"Query failed: {e}")
        st.stop()

if dc is None or dc.empty:
    st.warning(f"No count data for {sel_date} on account {account_id}.")
    st.stop()
if ds is None or ds.empty:
    st.warning(f"No select data for {sel_date} on account {account_id}.")
    st.stop()


# ── Greedy matching ───────────────────────────────────────────────────────────
count_houses  = set(dc["House"].dropna().unique())
select_houses = set(ds["House"].dropna().unique())
both          = sorted(count_houses & select_houses)

if not both:
    st.error(
        f"Count houses: {sorted(count_houses)}\n"
        f"Select houses: {sorted(select_houses)}\n"
        "No overlap — cannot match."
    )
    st.stop()

parts = []
for h in both:
    dc_h = dc[dc["House"] == h]
    ds_h = ds[ds["House"] == h]
    if dc_h.empty or ds_h.empty:
        continue
    matched = greedy_match(dc_h, ds_h, n_bins, 0.0)
    parts.append(matched)

if not parts:
    st.warning("Greedy matching returned no data.")
    st.stop()

agg       = pd.concat(parts, ignore_index=True)
agg_clean = agg.dropna(subset=["Bin"]).copy()
agg_clean["Bin"] = agg_clean["Bin"].astype(int)

total_counted = int(dc["Eggs"].sum())
drift = abs(total_counted - len(ds)) / max(total_counted, 1) * 100


# ── Metrics ───────────────────────────────────────────────────────────────────
mc1, mc2, mc3, mc4 = st.columns(4)
total_eggs = len(agg_clean)
rej_any    = agg_clean[[p for p in PROPS if p in agg_clean.columns]].any(axis=1).sum()
rej_pct    = rej_any / total_eggs * 100 if total_eggs else 0

mc1.markdown(f'<div class="metric-tile"><div class="metric-value">{total_eggs:,}</div><div class="metric-label">Eggs graded</div></div>', unsafe_allow_html=True)
mc2.markdown(f'<div class="metric-tile"><div class="metric-value">{rej_pct:.1f}%</div><div class="metric-label">Rejected (any)</div></div>', unsafe_allow_html=True)
mc3.markdown(f'<div class="metric-tile"><div class="metric-value">{drift:.1f}%</div><div class="metric-label">Count / grade drift</div></div>', unsafe_allow_html=True)
mc4.markdown(f'<div class="metric-tile"><div class="metric-value">{len(both)}</div><div class="metric-label">Houses matched</div></div>', unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)


# ── Heatmap ───────────────────────────────────────────────────────────────────
bin_labels = [
    f"{int(b * 100 / n_bins)}–{int((b + 1) * 100 / n_bins)}%"
    for b in range(n_bins)
]

def _build_strip(sub_df):
    row_z, row_text = [], []
    for b in range(n_bins):
        sub_b = sub_df[sub_df["Bin"] == b]
        n_b   = len(sub_b)
        if n_b == 0:
            row_z.append(None); row_text.append("—")
        else:
            k    = int(sub_b[sel_prop].sum()) if sel_prop in sub_b.columns else 0
            rate = k / n_b * 100
            row_z.append(rate)
            row_text.append(f"{rate:.2f}%  ({k}/{n_b})")
    return row_z, row_text

def _scale(row_z, global_max):
    valid = [v for v in row_z if v is not None]
    if normalize and valid:
        z_lo, z_hi = min(valid), max(valid)
        if z_lo == z_hi:
            z_lo, z_hi = 0.0, z_hi or 1.0
    else:
        z_lo, z_hi = 0.0, global_max or 1.0
    return z_lo, z_hi

def _render_strip(row_z, row_text, z_lo, z_hi, label, n_eggs, show_scale, is_last):
    st.markdown(
        f'<div class="house-label">{label}</div>'
        f'<div class="house-total">{n_eggs:,} eggs graded</div>',
        unsafe_allow_html=True,
    )
    fig = go.Figure(go.Heatmap(
        z=[row_z], x=bin_labels, y=[""],
        text=[row_text],
        hovertemplate="Position: %{x}<br>%{text}<extra></extra>",
        colorscale=HEATMAP_SCALE,
        colorbar=dict(title=dict(text="%", side="right"),
                      ticksuffix="%", thickness=10, len=1.0),
        zmin=z_lo, zmax=z_hi,
        zsmooth="best",
        showscale=show_scale,
    ))
    fig.update_layout(
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(l=0, r=50, t=0, b=24), height=70,
        font=dict(family="Roboto", size=11, color=TEXT_DARK),
        xaxis=dict(
            tickangle=0, tickfont=dict(size=10),
            title=dict(
                text="← nest end   belt position   grader end →" if is_last else "",
                font=dict(size=10, color=TEXT_MUTED),
            ),
        ),
        yaxis=dict(showticklabels=False, showgrid=False),
    )
    st.plotly_chart(fig, use_container_width=True)


hmap_houses = sorted(agg_clean["House"].unique()) if "House" in agg_clean.columns else ["Farm"]

all_vals = []
for h in hmap_houses:
    sub_h = agg_clean[agg_clean["House"] == h] if "House" in agg_clean.columns else agg_clean
    z, _  = _build_strip(sub_h)
    all_vals.extend(v for v in z if v is not None)
global_max = max(all_vals) if all_vals else 1.0

mode_label = "All houses combined" if combined else "Individual houses"
st.markdown(
    f'<div class="card">'
    f'<div class="card-header">Top View — {PROP_LABELS[sel_prop]}'
    f'<span style="font-size:11px;font-weight:400;opacity:0.85;margin-left:12px;">'
    f'blue = low &nbsp;·&nbsp; red = high &nbsp;·&nbsp; {mode_label} &nbsp;·&nbsp; '
    f'{"normalized" if normalize else "absolute scale"}'
    f'</span></div><div class="card-body">',
    unsafe_allow_html=True,
)

if combined:
    row_z, row_text = _build_strip(agg_clean)
    z_lo, z_hi = _scale(row_z, global_max)
    _render_strip(row_z, row_text, z_lo, z_hi,
                  label=f"All houses ({', '.join(hmap_houses)})",
                  n_eggs=len(agg_clean), show_scale=True, is_last=True)
else:
    for i, h in enumerate(hmap_houses):
        sub_h = agg_clean[agg_clean["House"] == h] if "House" in agg_clean.columns else agg_clean
        row_z, row_text = _build_strip(sub_h)
        z_lo, z_hi = _scale(row_z, global_max)
        _render_strip(row_z, row_text, z_lo, z_hi,
                      label=h, n_eggs=len(sub_h),
                      show_scale=(i == 0),
                      is_last=(i == len(hmap_houses) - 1))

st.markdown('</div></div>', unsafe_allow_html=True)
