"""
app.py — Top View heatmap (Streamlit Community Cloud).

Auth: Microsoft device-code flow — users sign in with their company account.

Streamlit secrets required (app Settings → Secrets):
    AAD_CLIENT_ID = "your-application-id"     # from IT / Azure AD app registration
    AAD_TENANT_ID = "your-directory-id"       # from IT / Azure AD
"""

import os, sys, struct, threading, platform, subprocess
from datetime import date, timedelta, datetime

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import greedy_match, PROPS, PROP_LABELS

# ── Config ────────────────────────────────────────────────────────────────────
SYNAPSE_SERVER = "saw-spf-prod-weu-ondemand.sql.azuresynapse.net"
SYNAPSE_DB     = "PowerBiViewsDB"
MASTER_SERVER  = "eur1-ip1-integration-sql1.database.windows.net"
MASTER_DB      = "eur1-ip1-integration-sql1-master"
DRIVER         = "ODBC Driver 18 for SQL Server"
SQL_COPT_SS_ACCESS_TOKEN = 1256

TABLE_COUNT  = 1
TABLE_SELECT = 5
BATCH_SIZE   = 100_000
N_BINS       = 72

AAD_CLIENT_ID = st.secrets.get("AAD_CLIENT_ID", os.environ.get("AAD_CLIENT_ID", ""))
AAD_TENANT_ID = st.secrets.get("AAD_TENANT_ID", os.environ.get("AAD_TENANT_ID", ""))
AAD_SCOPES    = ["https://database.windows.net/user_impersonation"]

# Optional hardcoded account list (used when master DB firewall blocks access).
# Format in Streamlit secrets:
#   [accounts]
#   "Hoste"      = 68
#   "Burnbrae"   = 42
#   "Broachdale" = 136
_STATIC_ACCOUNTS: dict = dict(st.secrets.get("accounts", {})) or {}

# ── Visual constants ──────────────────────────────────────────────────────────
GREEN_DARK  = "#00662f"
BG_PAGE     = "#F5F5F5"
BG_CARD     = "#FFFFFF"
TEXT_DARK   = "#212121"
TEXT_MUTED  = "#757575"
LOGO_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meggsius_connect_logo.png")

HEATMAP_SCALE = [[0.0, "#aabef4"], [0.5, "#c494c2"], [1.0, "#bb6553"]]
EMPTY_BIN_COLOR = "#4f57a6"

PROPS_EXT = PROPS + ["Volume"]
PROP_LABELS_EXT = {**PROP_LABELS, "Volume": "Volume (ml)"}

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Top View",
    page_icon="🥚",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(f"""
<style>
html, body, [class*="css"] {{
    font-family: 'Trebuchet MS', sans-serif;
    background-color: {BG_PAGE};
    color: {TEXT_DARK};
    font-size: 14px;
}}
[data-testid="stSidebar"] {{
    background-color: {GREEN_DARK} !important;
    min-width: 240px !important;
    max-width: 240px !important;
}}
[data-testid="stSidebar"] [data-testid="stImage"] {{
    background-color: white;
    border-radius: 8px;
    padding: 8px;
}}
[data-testid="stSidebar"] * {{ color: white !important; }}
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] .stNumberInput input,
[data-testid="stSidebar"] .stDateInput input {{
    color: {TEXT_DARK} !important;
    background-color: white !important;
}}
[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] {{
    background-color: white !important;
}}
[data-testid="stSidebar"] .stSelectbox [data-baseweb="select"] * {{
    color: {TEXT_DARK} !important;
}}
section.main > div {{ padding-top: 8px !important; }}
.metric-tile {{
    background: {BG_CARD};
    border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.10);
    padding: 10px 14px;
    text-align: center;
}}
.metric-value {{ font-size: 22px; font-weight: 700; color: {GREEN_DARK}; line-height: 1.2; }}
.metric-label {{ font-size: 10px; color: {TEXT_MUTED}; text-transform: uppercase;
                 letter-spacing: 0.5px; margin-top: 3px; }}
#MainMenu {{ visibility: hidden; }}
footer    {{ visibility: hidden; }}
header    {{ visibility: hidden; }}
[data-testid="collapsedControl"] {{ display: none !important; }}
[data-testid="stSidebarCollapseButton"] {{ display: none !important; }}
</style>
""", unsafe_allow_html=True)


# ── Color interpolation ───────────────────────────────────────────────────────
def _interp_color(t: float) -> str:
    s = HEATMAP_SCALE
    if t <= 0: return s[0][1]
    if t >= 1: return s[-1][1]
    for i in range(len(s) - 1):
        lo_t, lo_c = s[i]; hi_t, hi_c = s[i + 1]
        if lo_t <= t <= hi_t:
            f = (t - lo_t) / (hi_t - lo_t)
            def _h(c): h = c.lstrip('#'); return int(h[:2],16), int(h[2:4],16), int(h[4:],16)
            r1,g1,b1 = _h(lo_c); r2,g2,b2 = _h(hi_c)
            return f"rgb({int(r1+f*(r2-r1))},{int(g1+f*(g2-g1))},{int(b1+f*(b2-b1))})"
    return s[-1][1]


# ══════════════════════════════════════════════════════════════════════════════
# ── ODBC driver bootstrap (Linux / Streamlit Cloud) ──────────────────────────
@st.cache_resource(show_spinner=False)
def _ensure_odbc() -> str:
    """
    Install Microsoft ODBC Driver 18 on Linux if not already present.
    Runs once per deployment; result is cached.
    Returns the driver name string to use in connection strings.
    """
    if platform.system() != "Linux":
        return "ODBC Driver 18 for SQL Server"

    import pyodbc
    drivers = pyodbc.drivers()
    for candidate in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if candidate in drivers:
            return candidate

    # Driver not found — install it
    script = r"""
set -e
# Add Microsoft apt repository
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg

# Detect Ubuntu version
UBUNTU_VER=$(lsb_release -rs 2>/dev/null || echo "22.04")
curl -fsSL "https://packages.microsoft.com/config/ubuntu/${UBUNTU_VER}/prod.list" \
    -o /etc/apt/sources.list.d/mssql-release.list

apt-get update -qq 2>&1 | tail -3
ACCEPT_EULA=Y DEBIAN_FRONTEND=noninteractive apt-get install -y msodbcsql18 2>&1 | tail -5
"""
    result = subprocess.run(["bash", "-c", script],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"ODBC driver install failed:\n{result.stderr[-800:]}"
        )
    return "ODBC Driver 18 for SQL Server"


with st.spinner("Checking database driver…"):
    try:
        _DRIVER = _ensure_odbc()
    except RuntimeError as _odbc_err:
        st.error(f"**Cannot connect: ODBC driver unavailable.**\n\n```\n{_odbc_err}\n```")
        st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Auth — Microsoft device-code flow
# ══════════════════════════════════════════════════════════════════════════════
if not AAD_CLIENT_ID or not AAD_TENANT_ID:
    st.error(
        "**App not configured.**\n\n"
        "Go to **Settings → Secrets** and add:\n"
        "```\nAAD_CLIENT_ID = \"your-application-id\"\n"
        "AAD_TENANT_ID = \"your-directory-id\"\n```"
    )
    st.stop()


def _msal_app():
    import msal
    return msal.PublicClientApplication(
        AAD_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AAD_TENANT_ID}",
    )


def _token_bytes(token_str: str) -> bytes:
    b = token_str.encode("utf-16-le")
    return struct.pack("<I", len(b)) + b


for k, v in [("token", None), ("device_flow", None)]:
    if k not in st.session_state:
        st.session_state[k] = v

# Silent token refresh
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
            f"**Step 1** — Open: [{flow['verification_uri']}]({flow['verification_uri']})\n\n"
            f"**Step 2** — Enter code: `{flow['user_code']}`\n\n"
            f"**Step 3** — Sign in with your company account\n\n"
            f"**Step 4** — Click **Done** below"
        )
        if st.button("✓  Done — I've signed in", type="primary", use_container_width=True):
            result_holder: dict = {}
            def _acquire():
                result_holder["r"] = app_msal.acquire_token_by_device_flow(flow)
            t = threading.Thread(target=_acquire)
            t.start(); t.join(timeout=15)
            if "r" not in result_holder:
                st.warning("Still waiting — complete sign-in first, then click Done again.")
            elif "access_token" in result_holder["r"]:
                st.session_state.token       = result_holder["r"]["access_token"]
                st.session_state.device_flow = None
                st.rerun()
            else:
                st.error(f"Login failed: {result_holder['r'].get('error_description','Unknown')}")
                st.session_state.device_flow = None
        st.stop()

token = st.session_state.token


# ══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════════════════════
def _conn(server: str, database: str):
    import pyodbc
    cs = (f"DRIVER={{{_DRIVER}}};SERVER=tcp:{server},1433;DATABASE={database};"
          "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120;MARS_Connection=yes;")
    conn = pyodbc.connect(cs, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _token_bytes(token)})
    conn.timeout = 1800
    return conn


def _fetch(conn, sql: str) -> pd.DataFrame:
    cur = conn.cursor()
    cur.execute(sql)
    if cur.description is None:
        cur.close()
        return pd.DataFrame()
    cols = [c[0] for c in cur.description]
    rows: list = []
    while True:
        batch = cur.fetchmany(BATCH_SIZE)
        if not batch:
            break
        rows.extend(batch)
    cur.close()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([tuple(r) for r in rows], columns=cols)


def _nday(d: date) -> date:
    return d.replace(day=d.day + 1) if d.day < 28 else date(d.year + (d.month // 12), (d.month % 12) + 1, 1)


# ── Master DB queries ─────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False, ttl=3600)
def fetch_accounts(_token_key: str) -> pd.DataFrame:
    return _fetch(_conn(MASTER_SERVER, MASTER_DB), "SELECT Id, Name FROM mst.Account ORDER BY Name")


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_house_names(account_id: int, _token_key: str) -> dict:
    df = _fetch(_conn(MASTER_SERVER, MASTER_DB), f"""
        SELECT h.Id, h.Name AS HouseName, s.Name AS SiteName
        FROM mst.House h JOIN mst.Site s ON h.SiteId = s.Id
        WHERE s.AccountId = {account_id}
    """)
    mapping: dict = {}
    for _, row in df.iterrows():
        label = f"{row['HouseName']}  ({row['SiteName']})"
        mapping[str(row["Id"])]   = label
        mapping[f"[{row['Id']}]"] = label
    return mapping


# ── Synapse queries ───────────────────────────────────────────────────────────
def _sql_count(account_id: int, day: date) -> str:
    d, dn = day.strftime("%Y-%m-%d"), _nday(day).strftime("%Y-%m-%d")
    return f"""
SELECT r.HouseIdSet, r.BestUtcDateTime, r.OffsetMinutes,
       r.linenmbr, r.distancedonepercent, r.eggsincrease
FROM OPENROWSET(BULK '/{TABLE_COUNT}/PartitionKey={day.year}-{day.month:02d}-*/*.parquet',
    DATA_SOURCE='ArchiveDataLake', FORMAT='PARQUET') AS r
WHERE r.BestLocalDateTime>='{d}' AND r.BestLocalDateTime<'{dn}'
  AND r.AccountId={account_id}
  AND TRY_CAST(r.eggsincrease AS float)>0 AND r.linenmbr IS NOT NULL"""


def _sql_select(account_id: int, day: date) -> str:
    d, dn = day.strftime("%Y-%m-%d"), _nday(day).strftime("%Y-%m-%d")
    return f"""
SELECT r.HouseIdSet, r.BestUtcDateTime, r.OffsetMinutes, r.selectv2_lane,
       r.selectv2_volume,
       r.selectv2_isrejectedonmanure, r.selectv2_isrejectedonblood,
       r.selectv2_isrejectedoncrack, r.selectv2_isrejectedongroupdirt,
       r.selectv2_isrejectedongroupdamage
FROM OPENROWSET(BULK '/{TABLE_SELECT}/PartitionKey={day.year}-{day.month:02d}-*/*.parquet',
    DATA_SOURCE='ArchiveDataLake', FORMAT='PARQUET') AS r
WHERE r.BestLocalDateTime>='{d}' AND r.BestLocalDateTime<'{dn}'
  AND r.AccountId={account_id} AND r.selectv2_lane IS NOT NULL"""


# ── Transform ─────────────────────────────────────────────────────────────────
def _xform_count(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    raw = raw.copy()
    for col in ["eggsincrease","distancedonepercent","OffsetMinutes"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw[raw["eggsincrease"].notna() & (raw["eggsincrease"] > 0)]
    if raw.empty:
        return pd.DataFrame()
    off = float(raw["OffsetMinutes"].dropna().iloc[0])

    def _ts(v):
        try:
            return datetime.strptime(str(v).replace("Z","").replace("T"," ")[:19],
                                     "%Y-%m-%d %H:%M:%S") + timedelta(minutes=off)
        except Exception:
            return None

    raw["_dt"]      = raw["BestUtcDateTime"].apply(_ts)
    raw             = raw.dropna(subset=["_dt"])
    raw["Time"]     = (raw["_dt"].dt.hour*3600 + raw["_dt"].dt.minute*60 + raw["_dt"].dt.second).astype(float)
    raw["House"]    = raw["HouseIdSet"].astype(str).str.strip()
    raw["Line"]     = pd.to_numeric(raw["linenmbr"], errors="coerce").astype("Int64")
    raw["Position"] = raw["distancedonepercent"]
    raw["Eggs"]     = raw["eggsincrease"].astype(int)
    return raw[["Time","House","Line","Position","Eggs"]].dropna(subset=["Time","House","Position","Eggs"])


def _xform_select(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    FLAG = {"selectv2_isrejectedonmanure":"p_Manure","selectv2_isrejectedonblood":"p_Blood",
            "selectv2_isrejectedoncrack":"p_Crack","selectv2_isrejectedongroupdirt":"p_Dirt",
            "selectv2_isrejectedongroupdamage":"p_Damaged"}
    raw = raw.copy()
    raw["selectv2_lane"] = pd.to_numeric(raw["selectv2_lane"], errors="coerce")
    raw["OffsetMinutes"] = pd.to_numeric(raw["OffsetMinutes"], errors="coerce")
    raw = raw[raw["selectv2_lane"].notna()]
    if raw.empty:
        return pd.DataFrame()
    off = float(raw["OffsetMinutes"].dropna().iloc[0])

    def _ts(v):
        try:
            return datetime.strptime(str(v).replace("Z","").replace("T"," ")[:19],
                                     "%Y-%m-%d %H:%M:%S") + timedelta(minutes=off)
        except Exception:
            return None

    raw["_dt"]   = raw["BestUtcDateTime"].apply(_ts)
    raw          = raw.dropna(subset=["_dt"])
    for src, dst in FLAG.items():
        raw[dst] = (pd.to_numeric(raw.get(src, pd.Series(0, index=raw.index)), errors="coerce").fillna(0) == 1.0)
    raw["Volume"] = pd.to_numeric(raw.get("selectv2_volume", pd.Series(dtype=float)), errors="coerce")
    raw["Time"]   = (raw["_dt"].dt.hour*3600 + raw["_dt"].dt.minute*60 + raw["_dt"].dt.second).astype(float)
    raw["House"]  = raw["HouseIdSet"].astype(str).str.strip()
    raw["Lane"]   = raw["selectv2_lane"].astype(int)
    cols = ["Time","House","Lane","Volume"] + list(FLAG.values())
    return raw[cols].dropna(subset=["Time","House"])


@st.cache_data(show_spinner=False, ttl=600)
def fetch_day(account_id: int, day_str: str, _token_key: str):
    day  = date.fromisoformat(day_str)
    conn = _conn(SYNAPSE_SERVER, SYNAPSE_DB)
    dc   = _xform_count(_fetch(conn, _sql_count(account_id, day)))
    ds   = _xform_select(_fetch(conn, _sql_select(account_id, day)))
    return dc, ds


# ══════════════════════════════════════════════════════════════════════════════
# Session state
# ══════════════════════════════════════════════════════════════════════════════
_token_key = token[:16] if token else ""   # cache-bust key when token changes

for k, v in [("sel_date", date.today()), ("account_id", None), ("account_name", None),
             ("sel_prop", PROPS[0]), ("normalize", True), ("combined", False),
             ("scale_cap", 95), ("cc_gamma", 2.0), ("do_fetch", False)]:
    if k not in st.session_state:
        st.session_state[k] = v


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, use_container_width=True)
    else:
        st.markdown("### 🥚 Meggsius Connect")

    st.markdown("---")
    if _STATIC_ACCOUNTS:
        # Hardcoded list from secrets — works without master DB access
        account_names = list(_STATIC_ACCOUNTS.keys())
        default_idx   = (account_names.index(st.session_state.account_name)
                         if st.session_state.account_name in account_names else 0)
        selected_name = st.selectbox("Account", account_names, index=default_idx)
        account_id    = int(_STATIC_ACCOUNTS[selected_name])
    else:
        try:
            accounts_df   = fetch_accounts(_token_key)
            account_names = accounts_df["Name"].tolist()
            default_idx   = (account_names.index(st.session_state.account_name)
                             if st.session_state.account_name in account_names else 0)
            selected_name = st.selectbox("Account", account_names, index=default_idx)
            name_to_id    = dict(zip(accounts_df["Name"], accounts_df["Id"]))
            account_id    = int(name_to_id[selected_name])
        except Exception:
            # Master DB unreachable — manual ID entry
            account_id    = int(st.number_input("Account ID", min_value=1,
                                                 value=st.session_state.account_id or 68, step=1))
            selected_name = str(account_id)

    st.markdown("---")
    date_input = st.date_input("Date", value=st.session_state.sel_date)
    sel_prop   = st.selectbox("Rejection type", PROPS_EXT,
                              index=PROPS_EXT.index(st.session_state.sel_prop)
                              if st.session_state.sel_prop in PROPS_EXT else 0,
                              format_func=lambda p: PROP_LABELS_EXT[p])
    scale_cap  = st.slider("Colour scale cap", 50, 100, st.session_state.scale_cap, 5)
    cc_gamma   = st.slider("Gradient curve",   1.0, 4.0, st.session_state.cc_gamma, 0.5)
    normalize  = st.toggle("Normalize per house", value=st.session_state.normalize)
    combined   = st.toggle("Houses combined",     value=st.session_state.combined)

    st.markdown("---")
    if st.button("Search", type="primary", use_container_width=True):
        st.session_state.account_id   = account_id
        st.session_state.account_name = selected_name
        st.session_state.sel_date     = date_input
        st.session_state.sel_prop     = sel_prop
        st.session_state.normalize    = normalize
        st.session_state.combined     = combined
        st.session_state.scale_cap    = scale_cap
        st.session_state.cc_gamma     = cc_gamma
        st.session_state.do_fetch     = True
        st.rerun()

    st.markdown("---")
    if st.button("Sign out", use_container_width=True):
        st.session_state.token       = None
        st.session_state.device_flow = None
        st.session_state.do_fetch    = False
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# Main viewport
# ══════════════════════════════════════════════════════════════════════════════
account_id = st.session_state.account_id
sel_date   = st.session_state.sel_date
sel_prop   = st.session_state.sel_prop
normalize  = st.session_state.normalize
combined   = st.session_state.combined
scale_cap  = st.session_state.scale_cap
cc_gamma   = st.session_state.cc_gamma

DAYS_LONG = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
date_str  = f"{DAYS_LONG[sel_date.weekday()]} {sel_date.strftime('%d/%m/%Y')}"
_prop_display = {**PROP_LABELS, "Volume": "Volume (ml)"}
account_label = st.session_state.account_name or str(account_id)

st.markdown(
    f'<div style="background:{GREEN_DARK};color:white;padding:10px 18px;font-size:17px;'
    f'font-weight:600;border-radius:8px;margin-bottom:12px">Top View'
    f'<span style="font-size:12px;font-weight:400;opacity:0.82;margin-left:8px">'
    f'{date_str} &nbsp;·&nbsp; {account_label} &nbsp;·&nbsp; {_prop_display.get(sel_prop, sel_prop)}'
    f'</span></div>',
    unsafe_allow_html=True,
)

if not st.session_state.do_fetch or account_id is None:
    st.info("Select an account and date in the sidebar, then click **Search**.")
    st.stop()

# ── Fetch ─────────────────────────────────────────────────────────────────────
with st.status(f"Querying Synapse for {sel_date}…", expanded=True) as fetch_status:
    try:
        st.write("Fetching count data…")
        st.write("Fetching select / grading data…")
        dc, ds = fetch_day(account_id, str(sel_date), _token_key)
        fetch_status.update(label=f"Loaded ✓", state="complete", expanded=False)
    except Exception as e:
        fetch_status.update(label="Query failed", state="error")
        st.error(f"Query failed: {e}")
        st.stop()

if dc is None or dc.empty:
    st.warning(f"No count data for {sel_date}.")
    st.stop()
if ds is None or ds.empty:
    st.warning(f"No select data for {sel_date}.")
    st.stop()

# ── Greedy match ──────────────────────────────────────────────────────────────
both = sorted(set(dc["House"].dropna().unique()) & set(ds["House"].dropna().unique()))
if not both:
    st.error("No house overlap between count and select.")
    st.stop()

parts = []
for h in both:
    dc_h = dc[dc["House"] == h]; ds_h = ds[ds["House"] == h]
    if dc_h.empty or ds_h.empty:
        continue
    parts.append(greedy_match(dc_h, ds_h, N_BINS, 0.0))

if not parts:
    st.warning("No matched data.")
    st.stop()

agg       = pd.concat(parts, ignore_index=True)
agg_clean = agg.dropna(subset=["Bin"]).copy()
agg_clean["Bin"] = agg_clean["Bin"].astype(int)
if "Line" in agg_clean.columns and agg_clean["Line"].notna().any():
    agg_clean["Line"] = agg_clean["Line"].astype("Int64")
if "Lane" in agg_clean.columns:
    agg_clean = agg_clean[agg_clean["Lane"].notna()].copy()
    agg_clean["Lane"] = agg_clean["Lane"].astype(int)

# House name mapping
try:
    house_name_map = fetch_house_names(account_id, _token_key)
    agg_clean["House"] = agg_clean["House"].map(lambda h: house_name_map.get(str(h), h))
except Exception:
    house_name_map = {}

# ── Summary metric tiles ──────────────────────────────────────────────────────
total_eggs    = len(agg_clean)
flag_cols     = [p for p in PROPS if p in agg_clean.columns]
rej_any       = agg_clean[flag_cols].any(axis=1).sum() if flag_cols else 0
rej_pct       = rej_any / total_eggs * 100 if total_eggs else 0
total_counted = int(dc["Eggs"].sum())
drift         = abs(total_counted - total_eggs) / max(total_counted, 1) * 100
avg_volume    = (agg_clean["Volume"].mean()
                 if "Volume" in agg_clean.columns and agg_clean["Volume"].notna().any() else None)

def _tile(val: str, lbl: str) -> str:
    return (f'<div class="metric-tile"><div class="metric-value">{val}</div>'
            f'<div class="metric-label">{lbl}</div></div>')

c1, c2, c3, c4, c5 = st.columns(5)
for col, val, lbl in [
    (c1, f"{total_eggs:,}",                          "Eggs (Select)"),
    (c2, f"{rej_pct:.1f}%",                          "Rejected (any)"),
    (c3, f"{drift:.1f}%",                            "Count/select drift"),
    (c4, f"{avg_volume:.1f}" if avg_volume else "—", "Avg volume (ml)"),
    (c5, str(len(both)),                             "Houses"),
]:
    col.markdown(f'<div class="metric-tile"><div class="metric-value">{val}</div>'
                 f'<div class="metric-label">{lbl}</div></div>', unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Heatmap ────────────────────────────────────────────────────────────────────
bin_labels = [f"{int(b*100/N_BINS)}–{int((b+1)*100/N_BINS)}%" for b in range(N_BINS)]

if combined:
    display_groups = [("All houses", agg_clean)]
else:
    house_list     = sorted(agg_clean["House"].unique())
    display_groups = [(h, agg_clean[agg_clean["House"] == h]) for h in house_list]

rates: dict = {}
has_vol = "Volume" in agg_clean.columns
for label_h, hd in display_groups:
    for b in range(N_BINS):
        bd  = hd[hd["Bin"] == b]
        n   = len(bd)
        k   = int(bd[sel_prop].sum()) if n > 0 and sel_prop in bd.columns else 0
        vol = bd["Volume"].mean() if has_vol and n > 0 else None
        rates[(label_h, b)] = (k/n*100 if n > 0 else None, n, k, vol)

if sel_prop == "Volume":
    mv = [v[3] for v in rates.values() if v[3] is not None]
    if mv:
        s = pd.Series(mv)
        low_pct    = (100 - scale_cap) / 100
        global_min = float(s.quantile(low_pct))
        global_max = max(float(s.quantile(scale_cap/100)), global_min + 1e-9)
    else:
        global_min, global_max = 0.0, 1.0
else:
    mv = [v[0] for v in rates.values() if v[0] is not None]
    global_min = 0.0
    global_max = max(float(pd.Series(mv).quantile(scale_cap/100)), 1e-9) if mv else 1.0

N      = len(display_groups)
LANE_H = 74; BAR_H = 22; CELL_H = 38; PAD = 6
shapes = []; s_x, s_y, s_t = [], [], []

for idx, (label_h, _) in enumerate(display_groups):
    inv     = N - 1 - idx
    y_base  = inv * LANE_H + PAD
    y_bar_b = y_base + CELL_H
    y_bar_t = y_bar_b + BAR_H

    if normalize:
        vi = 3 if sel_prop == "Volume" else 0
        hv = [rates[(label_h,b)][vi] for b in range(N_BINS)
              if rates.get((label_h,b),(None,))[vi] is not None]
        if hv:
            s = pd.Series(hv)
            low_pct = (100 - scale_cap) / 100
            h_min = float(s.quantile(low_pct)) if sel_prop == "Volume" else 0.0
            h_max = max(float(s.quantile(scale_cap/100)), h_min + 1e-9)
        else:
            h_min, h_max = 0.0, 1.0
    else:
        h_min, h_max = global_min, global_max

    shapes.append(dict(type="rect", x0=-0.5, x1=N_BINS-0.5, y0=y_bar_b, y1=y_bar_t,
                       fillcolor=GREEN_DARK, line=dict(width=0), layer="above"))
    for x0, x1 in [(-1.1,-0.5),(N_BINS-0.5,N_BINS+0.1)]:
        shapes.append(dict(type="rect", x0=x0, x1=x1, y0=y_bar_b-5, y1=y_bar_t+5,
                           fillcolor="#90A4AE", line=dict(color="#607D8B",width=1), layer="above"))

    for b in range(N_BINS):
        rate, n, k, vol = rates.get((label_h, b), (None, 0, 0, None))
        metric = vol if sel_prop == "Volume" else rate
        if metric is not None and h_max > h_min:
            t = max(0.0, min(1.0, (metric - h_min) / (h_max - h_min)))
            color = _interp_color(t if sel_prop == "Volume" else t ** cc_gamma)
        elif metric is not None:
            color = _interp_color(0.5)
        else:
            color = EMPTY_BIN_COLOR

        shapes.append(dict(type="rect", x0=b-0.44, x1=b+0.44, y0=y_base+2, y1=y_bar_b-2,
                           fillcolor=color,
                           line=dict(color="rgba(255,255,255,0.55)", width=0.5), layer="above"))

        if sel_prop == "Volume":
            tip = (f"<b>{label_h}</b>  ·  {bin_labels[b]}<br>Avg volume: {vol:.1f} ml  ({n} eggs)"
                   if vol is not None else f"<b>{label_h}</b>  ·  {bin_labels[b]}<br>No data")
        else:
            vol_line = f"<br>Avg volume: {vol:.1f} ml" if vol is not None else ""
            _lbl = PROP_LABELS.get(sel_prop, sel_prop)
            tip = (f"<b>{label_h}</b>  ·  {bin_labels[b]}<br>"
                   f"{_lbl}: {rate:.2f}%  ({k}/{n} eggs){vol_line}"
                   if rate is not None else f"<b>{label_h}</b>  ·  {bin_labels[b]}<br>No data")
        s_x.append(b); s_y.append((y_base+y_bar_b)/2); s_t.append(tip)

fig = go.Figure()
fig.add_trace(go.Scatter(x=s_x, y=s_y, mode="markers",
                         marker=dict(opacity=0, size=20),
                         hovertemplate="%{text}<extra></extra>",
                         text=s_t, showlegend=False))
for idx, (label_h, _) in enumerate(display_groups):
    inv = N - 1 - idx
    y_bar_b = inv * LANE_H + PAD + CELL_H
    y_bar_t = y_bar_b + BAR_H
    fig.add_annotation(x=(N_BINS-1)/2, y=(y_bar_b+y_bar_t)/2,
                       text=f"<b>{label_h}</b>",
                       font=dict(color="white", size=11, family="Trebuchet MS"),
                       showarrow=False, xanchor="center", yanchor="middle")

fig.update_layout(
    shapes=shapes, height=max(320, N*LANE_H+60),
    plot_bgcolor="white", paper_bgcolor=BG_PAGE,
    margin=dict(l=30, r=20, t=6, b=50),
    xaxis=dict(range=[-1.2, N_BINS+0.2], tickvals=list(range(N_BINS)), ticktext=bin_labels,
               tickangle=-40, tickfont=dict(size=9, color=TEXT_MUTED),
               showgrid=False, zeroline=False,
               title=dict(text="← select end (count sensor)   ·   belt position   ·   nest end →",
                          font=dict(size=10, color=TEXT_MUTED))),
    yaxis=dict(range=[0, N*LANE_H+PAD], showticklabels=False, showgrid=False, zeroline=False),
    hovermode="closest", font=dict(family="Trebuchet MS"),
)

st.plotly_chart(fig, use_container_width=True)

# ── Line statistics tiles ─────────────────────────────────────────────────────
st.markdown("---")
st.markdown("#### Line statistics")
st.caption("Contamination rates and average volume per belt line.")

flag_cols_present = [p for p in PROPS if p in agg_clean.columns]
has_line = "Line" in agg_clean.columns and agg_clean["Line"].notna().any()

for label_h, hd in display_groups:
    st.markdown(f"**{label_h}**")
    if has_line:
        line_groups = sorted(hd["Line"].dropna().unique().astype(int))
        sub_groups  = [(f"Line {ln}", hd[hd["Line"] == ln]) for ln in line_groups]
    else:
        sub_groups = [(label_h, hd)]

    cols = st.columns(max(len(sub_groups), 1))
    for ci, (line_label, ld) in enumerate(sub_groups):
        n_total = len(ld)
        any_rej = (ld[flag_cols_present].any(axis=1).mean() * 100
                   if flag_cols_present and n_total > 0 else None)
        vol_val = (ld["Volume"].dropna().mean()
                   if "Volume" in ld.columns and ld["Volume"].notna().any() else None)
        html = (f"<div style='font-size:11px;font-weight:600;color:{TEXT_MUTED};"
                f"text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px'>"
                f"{line_label}</div>")
        html += _tile(f"{n_total:,}", "Eggs")
        html += _tile(f"{any_rej:.1f}%" if any_rej is not None else "—", "Any rejection")
        for p in flag_cols_present:
            r = ld[p].mean() * 100 if n_total > 0 else None
            html += _tile(f"{r:.1f}%" if r is not None else "—", PROP_LABELS.get(p, p))
        html += _tile(f"{vol_val:.1f}" if vol_val is not None else "—", "Avg vol (ml)")
        cols[ci].markdown(html, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
