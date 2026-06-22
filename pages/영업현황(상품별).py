import streamlit as st
import pandas as pd
import numpy as np
import re
import html
import calendar
import io
import os
from datetime import date, timedelta
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

FOLDER_IDS = {
    "공급전": st.secrets["drive_folders"]["supply"],
    "계약전": st.secrets["drive_folders"]["contract"],
    "시설전": st.secrets["drive_folders"]["facility"],
    "용도변경": st.secrets["drive_folders"]["usage_change"]
}

# ───────────────────────────────
# 📦 상품명 그룹핑 / 제외 규칙
# ───────────────────────────────
# 아래 매핑에 없는 상품명은 원래 이름 그대로 별도 컬럼으로 표시됩니다.
PRODUCT_GROUP_MAP = {
    "취사난방용": "가정용",
    "개별난방용": "가정용",
    "취사용": "가정용",
    "중앙난방용": "가정용",
    "일반용(1)": "일반용",
    "일반용(2)": "일반용",
    "업무난방용": "업무용",
    "주한미군": "업무용",
    "냉난방용(업무)": "업무용",
    "냉난방용(주택)": "업무용",
    "산업용": "산업용",
    "열병합용": "열병합용",
    "열병합용2": "열병합용",
    "자가열전용": "열병합용",
    "열전용설비용": "열병합용",
    "연료전지": "열병합용",
}

# 집계에서 완전히 제외할 상품명
EXCLUDE_EXACT = {"수송용(외주)", "수송용(CNG)"}
EXCLUDE_PREFIXES = ("미사용",)  # '미사용-'로 시작하는 모든 상품 제외

# 그룹화된 상품 표시 순서 (이후 매핑 안 된 나머지 상품은 등장 빈도순으로 뒤에 붙음)
PRODUCT_GROUP_ORDER = ["가정용", "일반용", "업무용", "산업용", "열병합용"]

def is_excluded_product(name) -> bool:
    if pd.isna(name): return True
    s = str(name).strip()
    if s in EXCLUDE_EXACT: return True
    if s.startswith(EXCLUDE_PREFIXES): return True
    return False

def map_product_group(name) -> str:
    return PRODUCT_GROUP_MAP.get(name, name)

# ───────────────────────────────
# 🛠️ 유틸리티 함수
# ───────────────────────────────
def remove_paren_codes(s) -> str:
    if pd.isna(s): return s
    s = str(s).strip()
    return re.sub(r"^\(\d+\)\s*", "", s).strip()

def to_numeric(series: pd.Series) -> pd.Series:
    return (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.extract(r'([-+]?\d*\.?\d+)', expand=False)
        .astype(float)
        .fillna(0)
        .astype(int)
    )

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
# 📊 상품명 기준 집계 로직
# ───────────────────────────────
def get_agg_by_product(df: pd.DataFrame, mode="net", product_col="상품명") -> pd.Series:
    """상품명을 그룹키로 사용하는 집계 (컬럼은 데이터에 등장하는 상품명에 따라 동적으로 결정)"""
    if df.empty or product_col not in df.columns:
        return pd.Series(dtype=int)

    df = df.copy()
    df[product_col] = df[product_col].map(remove_paren_codes)

    # 미사용- / 수송용(외주) 등 제외 대상 필터링
    df = df[~df[product_col].map(is_excluded_product)]

    # 그룹 매핑 적용 (예: 개별난방용 -> 가정용)
    df[product_col] = df[product_col].map(map_product_group)

    if mode == "net":
        v_in = to_numeric(df['계약전']) if '계약전' in df.columns else pd.Series(0, index=df.index)
        v_out = to_numeric(df['폐전']) if '폐전' in df.columns else pd.Series(0, index=df.index)
        df['val'] = v_in - v_out
    elif mode == "cancel":
        df['val'] = to_numeric(df['폐전']) if '폐전' in df.columns else pd.Series(0, index=df.index)
    elif mode == "facility":
        df['val'] = to_numeric(df['계약전']) if '계약전' in df.columns else pd.Series(0, index=df.index)
    else:  # supply
        df['val'] = to_numeric(df['건수']) if '건수' in df.columns else pd.Series(0, index=df.index)
        if '업종' in df.columns:
            df.loc[df['업종'].astype(str).str.strip() == "CES_죽곡", 'val'] = 0

    return df.groupby(product_col)['val'].sum().astype(int)

def build_use_change_product_pivot(snap_date: date):
    """용도변경: 이전상품명 -> 유출 / 변경상품명 -> 유입 으로 집계"""
    df = load_csv_from_drive(FOLDER_IDS["용도변경"], "용도변경", snap_date)
    if df.empty or '전수' not in df.columns:
        return pd.Series(dtype=int), pd.Series(dtype=int), pd.Series(dtype=int)

    df = df.copy()
    df['전수'] = to_numeric(df['전수'])

    in_s = pd.Series(dtype=int)
    out_s = pd.Series(dtype=int)

    # 실제 CSV 헤더는 공백이 포함된 '이전 상품명' / '변경 상품명' 형태이므로
    # 공백 유무 양쪽 다 대응
    in_col = next((c for c in ["변경 상품명", "변경상품명"] if c in df.columns), None)
    out_col = next((c for c in ["이전 상품명", "이전상품명"] if c in df.columns), None)

    if in_col:
        df[in_col] = df[in_col].map(remove_paren_codes)
        df_in = df[~df[in_col].map(is_excluded_product)].copy()
        df_in[in_col] = df_in[in_col].map(map_product_group)
        in_s = df_in.groupby(in_col)['전수'].sum()
    if out_col:
        df[out_col] = df[out_col].map(remove_paren_codes)
        df_out = df[~df[out_col].map(is_excluded_product)].copy()
        df_out[out_col] = df_out[out_col].map(map_product_group)
        out_s = -df_out.groupby(out_col)['전수'].sum()

    total_s = in_s.add(out_s, fill_value=0)
    return in_s, out_s, total_s

def add_total_col(series: pd.Series, product_cols: list) -> pd.Series:
    series = series.reindex(product_cols).fillna(0).astype(int)
    series['총합계'] = int(series.sum())
    return series

def build_row_df(s_curr: pd.Series, s_prev: pd.Series, name: str, product_cols: list) -> pd.DataFrame:
    recs = [
        {"구분": name, "세부": "당월", **add_total_col(s_curr, product_cols).to_dict()},
        {"구분": name, "세부": "전월", **add_total_col(s_prev, product_cols).to_dict()},
        {"구분": name, "세부": "증감", **add_total_col(s_curr - s_prev, product_cols).to_dict()},
    ]
    return pd.DataFrame(recs).set_index(["구분", "세부"])

# ───────────────────────────────
# 🎨 테이블 렌더러 (컴팩트 스타일, 동적 컬럼 지원)
# ───────────────────────────────
def render_final_table(pivot: pd.DataFrame) -> str:
    pivot = pivot.fillna(0)
    ths = "<th colspan='2' style='text-align:center;'>구분</th>" + "".join(
        f"<th>{html.escape(str(c))}</th>" for c in pivot.columns
    )
    rows_html = [f"<thead><tr>{ths}</tr></thead><tbody>"]
    l0_vals = pivot.index.get_level_values(0)
    last_l0 = None
    for i in range(len(pivot)):
        curr_l0, curr_l1 = l0_vals[i], pivot.index.get_level_values(1)[i]
        row_parts = []
        is_summ = curr_l0 in ['실적']

        if curr_l0 != last_l0:
            rowspan = (l0_vals == curr_l0).sum()
            bg = '#fffbe6' if is_summ else '#f1f5f9'
            if is_summ:
                row_parts.append(f"<td colspan='2' style='text-align:center; font-weight:600; background:{bg};'>{curr_l0}</td>")
            else:
                row_parts.append(f"<td rowspan='{rowspan}' style='text-align:center; vertical-align:middle; font-weight:600; background:{bg};'>{curr_l0}</td>")
            last_l0 = curr_l0
        if not is_summ:
            row_parts.append(f"<td style='text-align:center;'>{curr_l1}</td>")

        for v in pivot.iloc[i]:
            style = "text-align:right;" + ("font-weight:600; background:#fffbe6;" if is_summ else "")
            val_f = v if not pd.isna(v) else 0
            if val_f < 0:
                style += " color:red;"
            row_parts.append(f"<td style='{style}'>{int(val_f):,}</td>")
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
st.set_page_config(page_title="대성에너지 영업일보 - 상품별", layout="wide")
st.title("📦 대성에너지 영업일보 (상품별 집계)")

today = date.today()
default_date = today.replace(day=1) - timedelta(days=1)
selected_year = st.sidebar.selectbox("연도", range(2020, today.year + 2), index=range(2020, today.year + 2).index(default_date.year))
selected_month = st.sidebar.selectbox("월", range(1, 13), index=default_date.month - 1)

date_a = date(selected_year, selected_month, calendar.monthrange(selected_year, selected_month)[1])
date_b = (date_a.replace(day=1) - timedelta(days=1))
date_b = date(date_b.year, date_b.month, calendar.monthrange(date_b.year, date_b.month)[1])

st.sidebar.markdown("---")
st.sidebar.write(f"**기준 연월:** `{date_a.strftime('%Y-%m')}`")

# ───────────────────────────────
# 📋 작성 논리 안내 (화면 안내용 - 별도 .md 파일에서 불러옴)
# ───────────────────────────────
EXPLANATION_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "작성방법", "영업현황(상품별)_작성방법.md")

with st.expander("📋 이 표는 이렇게 만들어집니다", expanded=False):
    try:
        with open(EXPLANATION_PATH, "r", encoding="utf-8") as f:
            st.markdown(f.read())
    except FileNotFoundError:
        st.warning(f"⚠️ 설명 파일을 찾을 수 없습니다: {EXPLANATION_PATH}")

with st.spinner('데이터를 불러오는 중...'):
    df_c_a, df_c_b = load_csv_from_drive(FOLDER_IDS["계약전"], "계약전", date_a), load_csv_from_drive(FOLDER_IDS["계약전"], "계약전", date_b)
    df_f_a, df_f_b = load_csv_from_drive(FOLDER_IDS["시설전"], "시설전", date_a), load_csv_from_drive(FOLDER_IDS["시설전"], "시설전", date_b)
    df_s_a, df_s_b = load_csv_from_drive(FOLDER_IDS["공급전"], "공급전", date_a), load_csv_from_drive(FOLDER_IDS["공급전"], "공급전", date_b)

    if df_c_a.empty and df_f_a.empty and df_s_a.empty:
        st.warning(f"⚠️ {date_a.strftime('%Y%m')}월 데이터를 불러오지 못했습니다.")
        st.stop()

    # 상품명 기준 집계 (당월/전월)
    s_c_a_net, s_c_b_net = get_agg_by_product(df_c_a, "net"), get_agg_by_product(df_c_b, "net")
    s_c_a_can, s_c_b_can = get_agg_by_product(df_c_a, "cancel"), get_agg_by_product(df_c_b, "cancel")
    s_f_a, s_f_b = get_agg_by_product(df_f_a, "facility"), get_agg_by_product(df_f_b, "facility")
    s_s_a, s_s_b = get_agg_by_product(df_s_a, "supply"), get_agg_by_product(df_s_b, "supply")

    use_in_a, use_out_a, use_total_a = build_use_change_product_pivot(date_a)

    # 등장하는 모든 상품(그룹)명을 모아 컬럼 순서 결정
    # 1) 지정된 그룹(가정용/일반용/업무용/산업용/열병합용)을 먼저 배치
    # 2) 매핑되지 않은 나머지 상품명은 전체 절댓값 합 기준 내림차순으로 뒤에 배치
    all_series = [s_c_a_net, s_c_b_net, s_c_a_can, s_c_b_can, s_f_a, s_f_b, s_s_a, s_s_b, use_in_a, use_out_a]
    union_idx = set()
    for s in all_series:
        union_idx |= set(s.index)
    combined_abs = pd.Series(0, index=sorted(union_idx))
    for s in all_series:
        combined_abs = combined_abs.add(s.abs(), fill_value=0)

    remaining_idx = [c for c in combined_abs.index if c not in PRODUCT_GROUP_ORDER]
    remaining_sorted = combined_abs.reindex(remaining_idx).sort_values(ascending=False).index.tolist()
    existing_groups = [g for g in PRODUCT_GROUP_ORDER if g in combined_abs.index]
    product_cols = existing_groups + remaining_sorted

    # 실적 = 공급전 증감 + 폐전 증감 - 용도변경 합계 (상품 기준)
    perf_raw = (s_s_a - s_s_b).reindex(product_cols).fillna(0) \
        + (s_c_a_can - s_c_b_can).reindex(product_cols).fillna(0) \
        - use_total_a.reindex(product_cols).fillna(0)
    perf_s = add_total_col(perf_raw, product_cols)

    all_dfs = [pd.DataFrame([perf_s], index=pd.MultiIndex.from_tuples([('실적', '')]))]

    all_dfs.extend([
        build_row_df(s_c_a_net, s_c_b_net, "계약전", product_cols),
        build_row_df(s_f_a, s_f_b, "시설전", product_cols),
        build_row_df(s_s_a, s_s_b, "공급전", product_cols),
        build_row_df(s_c_a_can, s_c_b_can, "폐전", product_cols),
    ])

    use_rows = pd.DataFrame([
        add_total_col(use_in_a, product_cols),
        add_total_col(use_out_a, product_cols),
        add_total_col(use_total_a, product_cols),
    ], index=pd.MultiIndex.from_product([["용도변경"], ["유입", "유출", "합계"]]))
    all_dfs.append(use_rows)

    combined = pd.concat(all_dfs).fillna(0)
    st.markdown(render_final_table(combined), unsafe_allow_html=True)

    with st.expander("🔍 집계에 사용된 상품명 목록 (등장 빈도순)"):
        st.write(product_cols)

st.markdown("---")
st.caption("※ 실적 = 공급전 증감 + 폐전 증감 - 용도변경 합계 (상품명 기준 / 컬럼은 데이터에 등장하는 상품명 기준 자동 생성)")