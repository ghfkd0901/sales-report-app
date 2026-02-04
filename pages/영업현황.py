import streamlit as st
import pandas as pd
import numpy as np
import re
import html
import calendar
import io
from datetime import date, datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

# ───────────────────────────────
# 🔑 구글 서비스 계정 인증 및 API 빌드
# ───────────────────────────────
creds_dict = st.secrets["gcp_service_account"]
creds = service_account.Credentials.from_service_account_info(
    creds_dict, 
    scopes=[
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]
)

drive_service = build('drive', 'v3', credentials=creds)
gc = gspread.authorize(creds)

# 📁 Secrets 설정 로드
FOLDER_IDS = {
    "공급전": st.secrets["drive_folders"]["supply"],
    "계약전": st.secrets["drive_folders"]["contract"],
    "시설전": st.secrets["drive_folders"]["facility"],
    "용도변경": st.secrets["drive_folders"]["usage_change"]
}
PLAN_SHEET_URL = st.secrets["external_urls"]["plan_sheet"]

USAGE_COLS = ["공동주택", "단독주택", "영업용", "업무용", "산업용", "열병합용"]
FINAL_COLUMN_ORDER = ["공동주택", "단독주택", "소계", "영업용", "업무용", "산업용", "열병합용", "총합계"]

# ───────────────────────────────
# 🛠️ 유틸리티 함수 (정제 및 수치화)
# ───────────────────────────────
def remove_paren_codes(s: str) -> str:
    """'(11)공동주택' -> '공동주택' 정규화"""
    if pd.isna(s): return s
    s = str(s).strip()
    return re.sub(r"^\(\d+\)\s*", "", s).strip()

def to_numeric(series: pd.Series) -> pd.Series:
    """콤마 제거 및 수치형 변환 (NaN 방지)"""
    return (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.extract(r'([-+]?\d*\.?\d+)', expand=False)
        .astype(float)
        .fillna(0)
        .astype(int)
    )

def add_totals_to_series(series: pd.Series) -> pd.Series:
    series = series.fillna(0)
    series['소계'] = series.get('공동주택', 0) + series.get('단독주택', 0)
    base_cols = ["공동주택", "단독주택", "영업용", "업무용", "산업용", "열병합용"]
    series['총합계'] = sum(series.get(c, 0) for c in base_cols)
    return series.reindex(FINAL_COLUMN_ORDER).fillna(0)

# ───────────────────────────────
# ☁️ 구글 드라이브 파일 로더 (CP949 고정)
# ───────────────────────────────
def load_csv_from_drive(folder_id, prefix, target_date: date):
    yyyymm = target_date.strftime('%Y%m')
    query = f"'{folder_id}' in parents and name contains '{prefix}_{yyyymm}' and name contains '.csv' and trashed = false"
    
    try:
        results = drive_service.files().list(q=query, fields="files(id, name)", orderBy="name desc").execute()
        files = results.get('files', [])
        if not files: return pd.DataFrame()

        file_id = files[0]['id']
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done: _, done = downloader.next_chunk()
        
        fh.seek(0)
        df = pd.read_csv(fh, encoding="cp949")
        df.columns = [c.strip() for c in df.columns] 
        return df
    except: return pd.DataFrame()

# ───────────────────────────────
# 📊 데이터 집계 로직 (공급전만 CES_죽곡 제외)
# ───────────────────────────────
def get_agg_series(df, mode="net"):
    if df.empty: return pd.Series(0, index=USAGE_COLS)
    
    # 1. 용도 정규화
    if '용도' in df.columns: 
        df['용도'] = df['용도'].map(remove_paren_codes)
    
    # 2. 단독주택 업종 보정
    for col in ['업종', '업종분류']:
        if col in df.columns:
            mask = df[col].astype(str).str.contains("다세대|연립", na=False)
            df.loc[mask, "용도"] = "단독주택"
    
    # 3. 수치 데이터 추출 및 제외 논리 적용
    if mode == "net":
        v_in = to_numeric(df['계약전']) if '계약전' in df.columns else pd.Series(0, index=df.index)
        v_out = to_numeric(df['폐전']) if '폐전' in df.columns else pd.Series(0, index=df.index)
        df['val'] = v_in - v_out
    elif mode == "cancel":
        df['val'] = to_numeric(df['폐전']) if '폐전' in df.columns else pd.Series(0, index=df.index)
    elif mode == "facility":
        df['val'] = to_numeric(df['계약전']) if '계약전' in df.columns else pd.Series(0, index=df.index)
        # 시설전은 CES_죽곡 제외 안 함 (전체 포함)
    else:  # mode == "supply"
        df['val'] = to_numeric(df['건수']) if '건수' in df.columns else pd.Series(0, index=df.index)
        if '업종' in df.columns:
            df.loc[df['업종'].astype(str).str.strip() == "CES_죽곡", 'val'] = 0
            
    return df.groupby("용도")['val'].sum().reindex(USAGE_COLS).fillna(0).astype(int)

def build_use_change_pivot(snap_date: date) -> pd.DataFrame:
    df = load_csv_from_drive(FOLDER_IDS["용도변경"], "용도변경", snap_date)
    if df.empty: return pd.DataFrame()
    df["전수"] = to_numeric(df["전수"])
    
    def get_sum(col_name):
        df['clean_key'] = df[col_name].astype(str).map(remove_paren_codes)
        return df.groupby('clean_key')["전수"].sum().reindex(USAGE_COLS).fillna(0).astype(int)
    
    in_s = get_sum("변경용도")
    out_s = -get_sum("이전용도")
    table = pd.DataFrame([in_s, out_s, in_s + out_s], index=["유입", "유출", "합계"])
    res_rows = [add_totals_to_series(row) for _, row in table.iterrows()]
    return pd.DataFrame(res_rows, index=["유입", "유출", "합계"])

# ───────────────────────────────
# 🎨 테이블 렌더러 (컴팩트 스타일)
# ───────────────────────────────
def render_final_table(pivot: pd.DataFrame) -> str:
    pivot = pivot.fillna(0)
    ths = f"<th colspan='2' style='text-align:center;'>구분</th>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in pivot.columns)
    rows_html = [f"<thead><tr>{ths}</tr></thead><tbody>"]
    l0_vals = pivot.index.get_level_values(0)
    last_l0 = None
    for i in range(len(pivot)):
        curr_l0, curr_l1 = l0_vals[i], pivot.index.get_level_values(1)[i]
        row_parts = []
        is_summ = curr_l0 in ['실적', '계획', '계획 대비', '달성률']
        
        if curr_l0 != last_l0:
            rowspan = (l0_vals == curr_l0).sum()
            bg = '#fffbe6' if is_summ else '#f1f5f9'
            if is_summ: row_parts.append(f"<td colspan='2' style='text-align:center; font-weight:600; background:{bg};'>{curr_l0}</td>")
            else: row_parts.append(f"<td rowspan='{rowspan}' style='text-align:center; vertical-align:middle; font-weight:600; background:{bg};'>{curr_l0}</td>")
            last_l0 = curr_l0
        if not is_summ: row_parts.append(f"<td style='text-align:center;'>{curr_l1}</td>")
        
        for v in pivot.iloc[i]:
            style = "text-align:right;" + ("font-weight:600; background:#fffbe6;" if is_summ else "")
            val_f = v if not pd.isna(v) else 0
            if val_f < 0 and curr_l0 != '달성률': style += " color:red;"
            val_str = f"{val_f:.1%}" if curr_l0 == '달성률' else f"{int(val_f):,}"
            row_parts.append(f"<td style='{style}'>{val_str}</td>")
        rows_html.append(f"<tr>{''.join(row_parts)}</tr>")
    
    return f"""
    <style>
        .nice-table {{
            border-collapse: collapse; 
            width: auto;
            font-size: 18px; 
            border: 1px solid #e2e8f0;
            white-space: nowrap;
            margin: 0 auto;
        }} 
        .nice-table th, .nice-table td {{
            padding: 6px 12px;
            border: 1px solid #e2e8f0;
            line-height: 1.3;
        }}
        .nice-table thead th {{
            background: #f8fafc; 
            text-align: center; 
            font-weight: 700;
            padding: 8px 12px;
        }}
        .nice-table td:nth-child(n+3) {{
            min-width: 70px;
            text-align: right;
        }}
        .nice-table td:first-child {{
            min-width: 80px;
        }}
        .nice-table td:nth-child(2) {{
            min-width: 50px;
        }}
    </style>
    <table class='nice-table'>{''.join(rows_html)}</tbody></table>
    """

# ───────────────────────────────
# 🚀 메인 실행부
# ───────────────────────────────
st.set_page_config(page_title="대성에너지 영업일보", layout="wide")
st.title("📊 대성에너지 영업일보 (Cloud System)")

today = date.today()
default_date = today.replace(day=1) - timedelta(days=1)
selected_year = st.sidebar.selectbox("연도", range(2020, today.year+2), index=range(2020, today.year+2).index(default_date.year))
selected_month = st.sidebar.selectbox("월", range(1, 13), index=default_date.month - 1)

date_a = date(selected_year, selected_month, calendar.monthrange(selected_year, selected_month)[1])
date_b = (date_a.replace(day=1) - timedelta(days=1))
date_b = date(date_b.year, date_b.month, calendar.monthrange(date_b.year, date_b.month)[1])

st.sidebar.markdown("---")
st.sidebar.write(f"**기준 연월:** `{date_a.strftime('%Y-%m')}`")

with st.spinner('데이터를 불러오는 중...'):
    df_c_a, df_c_b = load_csv_from_drive(FOLDER_IDS["계약전"], "계약전", date_a), load_csv_from_drive(FOLDER_IDS["계약전"], "계약전", date_b)
    df_f_a, df_f_b = load_csv_from_drive(FOLDER_IDS["시설전"], "시설전", date_a), load_csv_from_drive(FOLDER_IDS["시설전"], "시설전", date_b)
    df_s_a, df_s_b = load_csv_from_drive(FOLDER_IDS["공급전"], "공급전", date_a), load_csv_from_drive(FOLDER_IDS["공급전"], "공급전", date_b)

    if df_c_a.empty and df_f_a.empty and df_s_a.empty:
        st.warning(f"⚠️ {date_a.strftime('%Y%m')}월 데이터를 불러오지 못했습니다.")
        st.stop()

    # 집계 수행
    s_c_a_net, s_c_b_net = get_agg_series(df_c_a, "net"), get_agg_series(df_c_b, "net")
    s_c_a_can, s_c_b_can = get_agg_series(df_c_a, "cancel"), get_agg_series(df_c_b, "cancel")
    s_f_a, s_f_b = get_agg_series(df_f_a, "facility"), get_agg_series(df_f_b, "facility")
    s_s_a, s_s_b = get_agg_series(df_s_a, "supply"), get_agg_series(df_s_b, "supply")
    
    p_use = build_use_change_pivot(date_a)
    
    def build_row_df(s_curr, s_prev, name):
        recs = [
            {"구분":name, "세부":"당월", **add_totals_to_series(s_curr)},
            {"구분":name, "세부":"전월", **add_totals_to_series(s_prev)},
            {"구분":name, "세부":"증감", **add_totals_to_series(s_curr - s_prev)}
        ]
        return pd.DataFrame(recs).set_index(["구분", "세부"])

    # ✅ 실적 계산: 열병합용 포함하도록 USAGE_COLS 기준으로 처리
    if not p_use.empty:
        use_change_sum = p_use.loc["합계"][USAGE_COLS]
    else:
        use_change_sum = pd.Series(0, index=USAGE_COLS)
    
    perf_raw = (s_s_a - s_s_b) + (s_c_a_can - s_c_b_can) - use_change_sum
    perf_s = add_totals_to_series(perf_raw)
    
    all_dfs = [pd.DataFrame([perf_s], index=pd.MultiIndex.from_tuples([('실적','')]))]
    
    # 계획 로드
    try:
        sh = gc.open_by_url(PLAN_SHEET_URL)
        df_p = pd.DataFrame(sh.get_worksheet(0).get_all_records())
        df_p['날짜'] = pd.to_datetime(df_p['날짜'])
        row_p = df_p[(df_p['날짜'].dt.year == date_a.year) & (df_p['날짜'].dt.month == date_a.month)]
        if not row_p.empty:
            plan_s = add_totals_to_series(pd.to_numeric(row_p.iloc[0].drop('날짜'), errors='coerce').fillna(0).astype(int))
            all_dfs.extend([
                pd.DataFrame([plan_s], index=pd.MultiIndex.from_tuples([('계획','')])),
                pd.DataFrame([(perf_s / plan_s.replace(0, np.nan)).fillna(0)], index=pd.MultiIndex.from_tuples([('달성률','')])),
                pd.DataFrame([perf_s - plan_s], index=pd.MultiIndex.from_tuples([('계획 대비','')]))
            ])
    except: pass

    all_dfs.extend([
        build_row_df(s_c_a_net, s_c_b_net, "계약전"),
        build_row_df(s_f_a, s_f_b, "시설전"),
        build_row_df(s_s_a, s_s_b, "공급전"),
        build_row_df(s_c_a_can, s_c_b_can, "폐전")
    ])
    
    if not p_use.empty:
        p_use.index = pd.MultiIndex.from_product([["용도변경"], p_use.index])
        all_dfs.append(p_use)

    combined = pd.concat(all_dfs).fillna(0)
    st.markdown(render_final_table(combined), unsafe_allow_html=True)

st.markdown("---")
st.caption("※ 실적 = 공급전 증감 + 폐전 증감 - 용도변경 합계 (열병합용 포함)")