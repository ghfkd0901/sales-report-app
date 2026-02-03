import streamlit as st
import pandas as pd
import numpy as np
import re
import html
import calendar
import io
from datetime import date
from dateutil.relativedelta import relativedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

# ───────────────────────────────
# 🔑 1. 구글 서비스 계정 인증 및 API 빌드
# ───────────────────────────────
@st.cache_resource
def get_gcp_services():
    creds_dict = st.secrets["gcp_service_account"]
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, 
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ]
    )
    drive = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    return drive, gc

drive_service, gc = get_gcp_services()

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
# 🛠️ 2. 유틸리티 함수 (첫번째 코드와 동일하게 통일)
# ───────────────────────────────
def remove_paren_codes(s: str) -> str:
    """'(11)공동주택' -> '공동주택' 정규화"""
    if pd.isna(s): return s
    s = str(s).strip()
    return re.sub(r"^\(\d+\)\s*", "", s).strip()

def to_numeric(series: pd.Series) -> pd.Series:
    """콤마 제거 및 수치형 변환"""
    return (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.extract(r'([-+]?\d*\.?\d+)', expand=False)
        .astype(float)
        .fillna(0)
        .astype(int)
    )

def add_totals_to_series(series: pd.Series) -> pd.Series:
    """소계, 총합계 추가"""
    series = series.fillna(0)
    series['소계'] = series.get('공동주택', 0) + series.get('단독주택', 0)
    base_cols = ["공동주택", "단독주택", "영업용", "업무용", "산업용", "열병합용"]
    series['총합계'] = sum(series.get(c, 0) for c in base_cols)
    return series.reindex(FINAL_COLUMN_ORDER).fillna(0)

# ───────────────────────────────
# ☁️ 3. 데이터 로더 (첫번째 코드 로직 완벽 반영)
# ───────────────────────────────
@st.cache_data(ttl=3600)
def load_csv_from_drive(folder_id, prefix, yyyymm):
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
        df = pd.read_csv(fh, encoding="cp949", low_memory=False)
        df.columns = [c.strip() for c in df.columns]
        return df
    except:
        return pd.DataFrame()

# ───────────────────────────────
# 📊 4. 집계 로직 (첫번째 코드와 완벽히 동일)
# ───────────────────────────────
def get_agg_series(df, mode="net"):
    """첫번째 코드의 집계 로직 그대로 사용"""
    if df.empty: return pd.Series(0, index=USAGE_COLS)
    
    df = df.copy()
    
    # 1. 용도 정규화
    if '용도' in df.columns: 
        df['용도'] = df['용도'].map(remove_paren_codes)
    
    # 2. 단독주택 업종 보정 (다세대/연립)
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
        if '업종' in df.columns:
            df.loc[df['업종'].astype(str).str.strip() == "CES_죽곡", 'val'] = 0
    else:  # mode == "supply"
        df['val'] = to_numeric(df['건수']) if '건수' in df.columns else pd.Series(0, index=df.index)
        if '업종' in df.columns:
            df.loc[df['업종'].astype(str).str.strip() == "CES_죽곡", 'val'] = 0
            
    return df.groupby("용도")['val'].sum().reindex(USAGE_COLS).fillna(0).astype(int)

def build_use_change_pivot(yyyymm: str):
    """용도변경 집계 (첫번째 코드와 동일)"""
    df = load_csv_from_drive(FOLDER_IDS["용도변경"], "용도변경", yyyymm)
    if df.empty: return pd.DataFrame(), pd.Series(0, index=USAGE_COLS)
    
    df = df.copy()
    df["전수"] = to_numeric(df["전수"])
    
    def get_sum(col_name):
        df['clean_key'] = df[col_name].astype(str).map(remove_paren_codes)
        return df.groupby('clean_key')["전수"].sum().reindex(USAGE_COLS).fillna(0).astype(int)
    
    in_s = get_sum("변경용도")   # 유입
    out_s = -get_sum("이전용도")  # 유출 (음수)
    net_s = in_s + out_s          # 합계
    
    table = pd.DataFrame([in_s, out_s, net_s], index=["유입", "유출", "합계"])
    res_rows = [add_totals_to_series(row) for _, row in table.iterrows()]
    pivot = pd.DataFrame(res_rows, index=["유입", "유출", "합계"])
    
    return pivot, net_s

@st.cache_data(ttl=3600)
def get_monthly_aggregates(target_date: date):
    """월별 집계 데이터 반환 (첫번째 코드 공식 완벽 반영)"""
    yyyymm_a = target_date.strftime('%Y%m')
    prev_month_date = target_date - relativedelta(months=1)
    yyyymm_b = prev_month_date.strftime('%Y%m')
    
    # 데이터 로드
    df_s_a = load_csv_from_drive(FOLDER_IDS["공급전"], "공급전", yyyymm_a)
    df_s_b = load_csv_from_drive(FOLDER_IDS["공급전"], "공급전", yyyymm_b)
    df_c_a = load_csv_from_drive(FOLDER_IDS["계약전"], "계약전", yyyymm_a)
    df_c_b = load_csv_from_drive(FOLDER_IDS["계약전"], "계약전", yyyymm_b)
    df_f_a = load_csv_from_drive(FOLDER_IDS["시설전"], "시설전", yyyymm_a)
    df_f_b = load_csv_from_drive(FOLDER_IDS["시설전"], "시설전", yyyymm_b)
    
    # 집계 (첫번째 코드와 동일한 방식)
    s_s_a = get_agg_series(df_s_a, "supply")
    s_s_b = get_agg_series(df_s_b, "supply")
    s_c_a_net = get_agg_series(df_c_a, "net")
    s_c_b_net = get_agg_series(df_c_b, "net")
    s_c_a_can = get_agg_series(df_c_a, "cancel")
    s_c_b_can = get_agg_series(df_c_b, "cancel")
    s_f_a = get_agg_series(df_f_a, "facility")
    s_f_b = get_agg_series(df_f_b, "facility")
    
    # 용도변경
    p_use, use_change_net = build_use_change_pivot(yyyymm_a)
    
    # 증감 계산
    supply_delta = s_s_a - s_s_b
    cancel_delta = s_c_a_can - s_c_b_can
    contract_delta = s_c_a_net - s_c_b_net
    facility_delta = s_f_a - s_f_b
    
    # ✅ 실적 공식: 공급전 증감 + 폐전 증감 - 용도변경 합계
    performance = supply_delta + cancel_delta - use_change_net
    
    # 계획 로드
    plan_series = pd.Series(0, index=USAGE_COLS)
    try:
        sh = gc.open_by_url(PLAN_SHEET_URL)
        df_p = pd.DataFrame(sh.get_worksheet(0).get_all_records())
        df_p['날짜'] = pd.to_datetime(df_p['날짜'])
        row_p = df_p[(df_p['날짜'].dt.year == target_date.year) & (df_p['날짜'].dt.month == target_date.month)]
        if not row_p.empty:
            plan_series = pd.to_numeric(row_p.iloc[0].drop('날짜'), errors='coerce').reindex(USAGE_COLS, fill_value=0).fillna(0).astype(int)
    except: pass
    
    return {
        "performance": performance,           # 실적
        "supply_delta": supply_delta,         # 공급전 증감
        "cancel_delta": cancel_delta,         # 폐전 증감
        "contract_delta": contract_delta,     # 계약전 증감
        "facility_delta": facility_delta,     # 시설전 증감
        "use_change_net": use_change_net,     # 용도변경 합계
        "use_change_pivot": p_use,            # 용도변경 상세 (유입/유출/합계)
        "plan": plan_series,                  # 계획
        "supply_total_curr": s_s_a,           # 당월 공급전 총계
        "supply_total_prev": s_s_b,           # 전월 공급전 총계
    }

# ───────────────────────────────
# 🎨 5. HTML 렌더링 함수
# ───────────────────────────────
def render_summary_table(df: pd.DataFrame) -> str:
    header1_cells, header2_cells = [], []
    level0 = df.columns.get_level_values(0)
    for col_name in level0.unique():
        is_standalone = (df.columns.get_level_values(1)[level0 == col_name] == '').all()
        if is_standalone:
            header1_cells.append(f"<th rowspan=2 style='text-align:center; vertical-align:middle;'>{html.escape(str(col_name))}</th>")
        else:
            colspan = (level0 == col_name).sum()
            header1_cells.append(f"<th colspan='{colspan}'>{html.escape(str(col_name))}</th>")
    for col in df.columns:
        if col[1] != '':
            header2_cells.append(f"<th>{html.escape(str(col[1]))}</th>")

    header1 = f"<tr><th rowspan=2 style='background:#e0e7ff; text-align:center; vertical-align:middle;'>구분</th>{''.join(header1_cells)}</tr>"
    header2 = f"<tr>{''.join(header2_cells)}</tr>"
    
    rows_html = []
    for idx, row in df.iterrows():
        row_html = f"<tr><th style='background:#f1f5f9;'>{idx}</th>"
        for i, val in enumerate(row):
            style, fmt_val = 'text-align:right;', ""
            col_level_1_name = df.columns[i][1]
            if "달성률" in col_level_1_name:
                fmt_val = f"{val:.1%}"
            elif isinstance(val, (int, float, np.number)):
                fmt_val = f"{int(val):,}"
                if val < 0: style += 'color:red;'
            else: fmt_val = str(val)
            row_html += f"<td style='{style}'>{fmt_val}</td>"
        rows_html.append(row_html + "</tr>")
        
    return f"""
    <style>
        .summary-table {{ 
            border-collapse: collapse; 
            width: auto; 
            font-size: 18px; 
            margin: 0 auto;
            white-space: nowrap;
        }}
        .summary-table th, .summary-table td {{ 
            border: 1px solid #dee2e6; 
            padding: 6px 12px;
            line-height: 1.3;
        }}
        .summary-table thead th {{ 
            background: #e9ecef; 
            text-align: center; 
            vertical-align: middle;
            padding: 8px 12px;
        }} 
        .summary-table tbody th {{ background: #f8f9fa; }}
        .summary-table td {{ min-width: 70px; }}
    </style>
    <table class='summary-table'><thead>{header1}{header2}</thead><tbody>{"".join(rows_html)}</tbody></table>
    """

# ───────────────────────────────
# 🚀 6. 메인 실행부
# ───────────────────────────────
st.set_page_config(page_title="영업일보 누계", layout="wide")
st.title("📊 대성에너지 영업일보 - 누계 현황")

st.sidebar.header("🗓️ 조회 월 선택")

today = date.today()
last_month = today - relativedelta(months=1)
YEARS = list(range(2020, today.year + 2))
MONTHS = list(range(1, 13))

default_year_idx = YEARS.index(last_month.year) if last_month.year in YEARS else len(YEARS)-2
default_month_idx = MONTHS.index(last_month.month)

selected_year = st.sidebar.selectbox("연도", YEARS, index=default_year_idx)
selected_month_num = st.sidebar.selectbox("월", MONTHS, index=default_month_idx)

if st.sidebar.button("🔄 데이터 강제 새로고침"):
    st.cache_data.clear()
    st.rerun()

target_date = date(selected_year, selected_month_num, calendar.monthrange(selected_year, selected_month_num)[1])
prev_month_date = target_date - relativedelta(months=1)

with st.spinner('데이터를 계산 중...'):
    try:
        # 월별 데이터 수집
        monthly_data = {}
        for m in range(1, target_date.month + 1):
            iter_date = date(selected_year, m, calendar.monthrange(selected_year, m)[1])
            monthly_data[m] = get_monthly_aggregates(iter_date)

        # 연간 계획 로드
        year_plan = pd.Series(0, index=USAGE_COLS)
        try:
            sh = gc.open_by_url(PLAN_SHEET_URL)
            df_p = pd.DataFrame(sh.get_worksheet(0).get_all_records())
            df_p['날짜'] = pd.to_datetime(df_p['날짜'])
            year_plan_df = df_p[df_p['날짜'].dt.year == selected_year]
            if not year_plan_df.empty:
                year_plan = year_plan_df[USAGE_COLS].sum().astype(int)
        except: pass

        curr = monthly_data[target_date.month]
        
        # 전월말 공급전 (전월 데이터에서 가져오기)
        if prev_month_date.month in monthly_data and prev_month_date.year == selected_year:
            prev_supply_total = monthly_data[prev_month_date.month]['supply_total_curr']
        else:
            prev_data = get_monthly_aggregates(prev_month_date)
            prev_supply_total = prev_data['supply_total_curr']
        
        # 누계 계산
        cumulative_plan = sum(m['plan'] for m in monthly_data.values())
        cumulative_perf = sum(m['performance'] for m in monthly_data.values())
        cumulative_canc = sum(m['cancel_delta'] for m in monthly_data.values())
        cumulative_uc = sum(m['use_change_net'] for m in monthly_data.values())

        # 요약 데이터 구성
        data_dict = {
            ('전월말', ''): prev_supply_total,
            ('당월', '계획'): curr['plan'], 
            ('당월', '실적'): curr['performance'], 
            ('당월', '폐전'): curr['cancel_delta'], 
            ('당월', '용도변경'): curr['use_change_net'],
            ('당월', '달성률'): (curr['performance'] / curr['plan'].replace(0, np.nan)).fillna(0),
            ('당년', '연간계획'): year_plan, 
            ('당년', '누계실적'): cumulative_perf, 
            ('당년', '누계폐전'): cumulative_canc,
            ('당년', '누계용도변경'): cumulative_uc,
            ('당년', '연간달성률'): (cumulative_perf / year_plan.replace(0, np.nan)).fillna(0),
            ('당년', '누계계획'): cumulative_plan,
            ('당년', '누계달성률'): (cumulative_perf / cumulative_plan.replace(0, np.nan)).fillna(0),
            ('당월말', ''): curr['supply_total_curr'],
        }

        df_by_usage = pd.DataFrame(data_dict).reindex(USAGE_COLS)
        
        # 합계 행 계산
        total_row = df_by_usage.sum(numeric_only=True)
        with np.errstate(divide='ignore', invalid='ignore'):
            total_row[('당월', '달성률')] = total_row[('당월', '실적')] / total_row[('당월', '계획')] if total_row[('당월', '계획')] != 0 else 0
            total_row[('당년', '연간달성률')] = total_row[('당년', '누계실적')] / total_row[('당년', '연간계획')] if total_row[('당년', '연간계획')] != 0 else 0
            total_row[('당년', '누계달성률')] = total_row[('당년', '누계실적')] / total_row[('당년', '누계계획')] if total_row[('당년', '누계계획')] != 0 else 0

        final_df = pd.concat([pd.DataFrame(total_row, columns=['합계']).T, df_by_usage]).fillna(0)
        final_df.index.name = "구분"

        # 메인 테이블 출력
        st.markdown(render_summary_table(final_df), unsafe_allow_html=True)

        # ───────────────────────────────
        # 📋 검증용 월별 상세
        # ───────────────────────────────
        st.markdown("---")
        st.subheader("📋 월별 상세 내역 (검증용)")

        perf_records, canc_records, uc_records, supply_records = [], [], [], []
        for m, data in monthly_data.items():
            date_str = f"{selected_year}-{m:02d}"
            
            p_row = data['performance'].copy()
            p_row['날짜'] = date_str
            p_row['합계'] = data['performance'].sum()
            perf_records.append(p_row)
            
            c_row = data['cancel_delta'].copy()
            c_row['날짜'] = date_str
            c_row['합계'] = data['cancel_delta'].sum()
            canc_records.append(c_row)
            
            u_row = data['use_change_net'].copy()
            u_row['날짜'] = date_str
            u_row['합계'] = data['use_change_net'].sum()
            uc_records.append(u_row)
            
            s_row = data['supply_delta'].copy()
            s_row['날짜'] = date_str
            s_row['합계'] = data['supply_delta'].sum()
            supply_records.append(s_row)

        cols = ['날짜'] + USAGE_COLS + ['합계']

        with st.expander("✅ 실적 (공급증감 + 폐전증감 - 용도변경)", expanded=True):
            df_perf = pd.DataFrame(perf_records)[cols]
            st.dataframe(df_perf, use_container_width=True, hide_index=True)
            st.info(f"**누계 합계:** {df_perf['합계'].sum():,}")

        with st.expander("📦 공급전 증감", expanded=False):
            df_supply = pd.DataFrame(supply_records)[cols]
            st.dataframe(df_supply, use_container_width=True, hide_index=True)
            st.info(f"**누계 합계:** {df_supply['합계'].sum():,}")

        with st.expander("🚫 폐전 증감", expanded=False):
            df_canc = pd.DataFrame(canc_records)[cols]
            st.dataframe(df_canc, use_container_width=True, hide_index=True)
            st.info(f"**누계 합계:** {df_canc['합계'].sum():,}")

        with st.expander("🔄 용도변경 합계", expanded=False):
            df_uc = pd.DataFrame(uc_records)[cols]
            st.dataframe(df_uc, use_container_width=True, hide_index=True)
            st.info(f"**누계 합계:** {df_uc['합계'].sum():,}")

        # CSV 다운로드
        csv = final_df.reset_index().to_csv(index=False, encoding='utf-8-sig').encode('utf-8-sig')
        st.sidebar.download_button("📥 요약 CSV 다운로드", data=csv, file_name=f"영업일보_누계_{target_date.strftime('%Y%m')}.csv", mime="text/csv")

    except Exception as e:
        st.error(f"데이터 처리 중 오류 발생: {e}")
        import traceback
        st.code(traceback.format_exc())