import streamlit as st

st.set_page_config(page_title="영업일보 매뉴얼", layout="wide", page_icon="📖")

st.title("📖 영업일보 데이터 작성 매뉴얼")

# ───────────────────────────────
# 데이터 폴더 링크
# ───────────────────────────────
DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders/1W0lmeFTgKNP95QygYqxt09-Hz2EajR0h"
PLAN_SHEET_URL = "https://docs.google.com/spreadsheets/d/1aBcDeFgHiJkLmNoPqRsTuVwXyZ123456"  # 실제 시트 ID로 변경

st.info(f"📁 **[원본 데이터 폴더 바로가기 (Google Drive)]({DRIVE_FOLDER_URL})**")

# ───────────────────────────────
# 1. 핵심 계산 공식
# ───────────────────────────────
st.header("1. 핵심 계산 공식")

st.error("⚠️ **실적 계산 공식** (가장 중요)")
st.latex(r"\text{실적} = \text{공급전 증감} + \text{폐전 증감} - \text{용도변경 합계}")

col1, col2, col3 = st.columns(3)

with col1:
    st.markdown("""
**📦 공급전 증감**
```
당월 공급건수 - 전월 공급건수
```
- 파일: `공급전_YYYYMM.csv`
- 컬럼: `건수`
""")

with col2:
    st.markdown("""
**🚫 폐전 증감**
```
당월 폐전건수 - 전월 폐전건수
```
- 파일: `계약전_YYYYMM.csv`
- 컬럼: `폐전`
""")

with col3:
    st.markdown("""
**🔄 용도변경 합계**
```
유입(변경용도) - 유출(이전용도)
```
- 파일: `용도변경_YYYYMM.csv`
- 컬럼: `전수`
""")

# ───────────────────────────────
# 2. 원본 데이터 구조
# ───────────────────────────────
st.markdown("---")
st.header("2. 원본 데이터 구조")

tab1, tab2, tab3, tab4 = st.tabs(["공급전", "계약전", "시설전", "용도변경"])

with tab1:
    st.markdown("**파일명**: `공급전_YYYYMM.csv` (예: 공급전_202501.csv)")
    st.dataframe({
        "컬럼명": ["용도", "업종", "건수"],
        "예시": ["(11)공동주택", "아파트", "1,234"],
        "용도": ["그룹 기준", "CES_죽곡 제외용", "집계 대상"]
    }, hide_index=True, use_container_width=True)

with tab2:
    st.markdown("**파일명**: `계약전_YYYYMM.csv`")
    st.dataframe({
        "컬럼명": ["용도", "업종", "계약전", "폐전"],
        "예시": ["(12)단독주택", "다세대주택", "500", "120"],
        "용도": ["그룹 기준", "다세대→단독 변환", "순증감용", "폐전 집계"]
    }, hide_index=True, use_container_width=True)

with tab3:
    st.markdown("**파일명**: `시설전_YYYYMM.csv`")
    st.dataframe({
        "컬럼명": ["용도", "업종", "계약전"],
        "예시": ["(21)영업용", "음식점", "80"],
        "용도": ["그룹 기준", "CES_죽곡 제외용", "시설 건수"]
    }, hide_index=True, use_container_width=True)

with tab4:
    st.markdown("**파일명**: `용도변경_YYYYMM.csv`")
    st.dataframe({
        "컬럼명": ["이전용도", "변경용도", "전수"],
        "예시": ["(12)단독주택", "(21)영업용", "5"],
        "용도": ["유출 기준", "유입 기준", "변경 건수"]
    }, hide_index=True, use_container_width=True)

# ───────────────────────────────
# 3. 특수 처리 로직
# ───────────────────────────────
st.markdown("---")
st.header("3. 특수 처리 로직")

st.markdown("""
| 처리 | 내용 | 예시 |
|------|------|------|
| **용도 코드 정규화** | 괄호 코드 제거 | `(11)공동주택` → `공동주택` |
| **다세대/연립 변환** | 업종이 다세대/연립이면 단독주택으로 | 업종=`다세대주택` → 용도=`단독주택` |
| **CES_죽곡 제외** | 업종이 CES_죽곡이면 건수 0 처리 | 공급전, 시설전에 적용 |
""")

# ───────────────────────────────
# 4. 데이터 입력 체크리스트
# ───────────────────────────────
st.markdown("---")
st.header("4. 월별 데이터 입력")

st.markdown(f"""
| 순서 | 작업 |
|------|------|
| 1 | ERP에서 4개 파일 추출 (공급전/계약전/시설전/용도변경) |
| 2 | 파일명 확인: `{{종류}}_YYYYMM.csv` |
| 3 | [Google Drive 폴더]({DRIVE_FOLDER_URL})에 업로드 |
| 4 | 시스템에서 "🔄 새로고침" 후 확인 |
""")

st.info(f"""
**계획 데이터**: [Google Sheets]({PLAN_SHEET_URL})에 월별 입력
- 날짜: 매월 1일 (예: 2025-01-01)
- 용도별 계획 건수 입력
""")

st.markdown("---")
st.caption("© 대성에너지 영업기획팀")