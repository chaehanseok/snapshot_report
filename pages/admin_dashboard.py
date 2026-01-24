import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import zipfile
from io import BytesIO

# =================================================
# D1 Query Helper
# =================================================
def d1_query(sql: str, params: list):
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{st.secrets['CF_ACCOUNT_ID']}/d1/database/"
        f"{st.secrets['D1_DATABASE_ID']}/query"
    )
    headers = {
        "Authorization": f"Bearer {st.secrets['CF_API_TOKEN']}",
        "Content-Type": "application/json",
    }
    r = requests.post(
        url,
        headers=headers,
        json={"sql": sql, "params": params},
        timeout=30,
    )
    if not r.ok:
        st.error("D1 ERROR")
        st.code(r.text)
        r.raise_for_status()

    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data)

    return data["result"][0]["results"] if data.get("result") else []

def build_zip_from_issues(issues: list[dict]) -> bytes:
    """
    R2에 있는 PDF들을 ZIP으로 묶어서 bytes 반환
    """
    endpoint = st.secrets["R2_ENDPOINT"]
    bucket = st.secrets["R2_BUCKET_NAME"]

    zip_buf = BytesIO()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in issues:
            signed_url = generate_presigned_pdf_url(r["pdf_r2_key"], expires=600)
            resp = requests.get(signed_url, timeout=30)
            if resp.ok:
                z.writestr(r["pdf_filename"], resp.content)

    zip_buf.seek(0)
    return zip_buf.getvalue()

# =================================================
# 관리자 인증
# =================================================
def verify_admin():
    token = st.query_params.get("token")
    if not token:
        st.error("관리자 토큰이 없습니다.")
        st.stop()

    # 기존 app.py 의 verify_token 재사용
    from app import verify_token
    user = verify_token(token)

    if user.get("role") != "admin":
        st.error("관리자 전용 페이지입니다.")
        st.stop()

    return user


# =================================================
# Page Config
# =================================================
st.set_page_config(
    page_title="관리자 · 발행 대시보드",
    layout="wide",
)

admin = verify_admin()

st.title("📊 발행 관리 대시보드")
st.caption("보장점검 리포트 발행 현황 관리")

kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
st.caption(f"기준 시각(KST): {kst_now}")

st.divider()

# =================================================
# 1️⃣ KPI 요약
# =================================================
sql_kpi = """
SELECT
  COUNT(*) AS total_cnt,
  COUNT(DISTINCT fc_id) AS fc_cnt,
  SUM(
    CASE
      WHEN DATE(created_at) = DATE('now', '+9 hours')
      THEN 1 ELSE 0
    END
  ) AS today_cnt,
  MAX(created_at) AS last_issue_at
FROM report_issue;
"""
kpi = d1_query(sql_kpi, [])

if kpi:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📄 전체 발행 수", f"{kpi[0]['total_cnt']:,}")
    c2.metric("👤 참여 FC 수", f"{kpi[0]['fc_cnt']:,}")
    c3.metric("🗓 오늘 발행", f"{kpi[0]['today_cnt']:,}")
    c4.metric("⏱ 최근 발행", kpi[0]["last_issue_at"][:16])

st.divider()

# =================================================
# 2️⃣ 필터
# =================================================
st.subheader("🔎 발행 목록 필터")

f1, f2, f3 = st.columns(3)

with f1:
    fc_name = st.text_input("FC 이름")

with f2:
    age_band = st.selectbox(
        "연령대",
        ["전체", "20대", "30대", "40대", "50대", "60대", "70대"],
    )

with f3:
    date_from = st.date_input("시작일")

# =================================================
# 3️⃣ 목록 조회
# =================================================
where = ["1=1"]
params = []

if fc_name:
    where.append("fc_name LIKE ?")
    params.append(f"%{fc_name}%")

if age_band != "전체":
    where.append("customer_age_band = ?")
    params.append(age_band)

if date_from:
    where.append("DATE(created_at) >= ?")
    params.append(str(date_from))

sql_list = f"""
SELECT
  compliance_code,
  fc_name,
  customer_name,
  customer_age_band,
  created_at,
  pdf_r2_key,
  pdf_filename
FROM report_issue
WHERE {' AND '.join(where)}
ORDER BY created_at DESC
LIMIT 200;
"""

rows = d1_query(sql_list, params)

# =================================================
# 4️⃣ 발행 목록
# =================================================
st.subheader("📋 발행 목록")

if not rows:
    st.info("조회 결과가 없습니다.")
    st.stop()

for r in rows:
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])

        c1.markdown(f"**{r['compliance_code']}**")
        c2.write(r["fc_name"])
        c3.write(r["customer_name"] or "-")
        c4.write(r["customer_age_band"])
        c5.link_button(
            "상세",
            f"/admin_issue_detail?code={r['compliance_code']}&token={st.query_params.get('token')}",
        )

st.divider()
st.subheader("📦 일괄 다운로드")

if st.button("선택 조건 전체 PDF ZIP 다운로드"):
    with st.spinner("PDF ZIP 생성 중..."):
        zip_bytes = build_zip_from_issues(rows)

        # 🔹 관리자 다운로드 로그 기록
        for r in rows:
            d1_query(
                """
                INSERT INTO report_issue_event
                (compliance_code, event_type, actor_type, actor_id)
                VALUES (?, 'bulk_download', 'admin', ?);
                """,
                [r["compliance_code"], admin.get("id")],
            )

        ts = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M")

        st.download_button(
            label="📥 ZIP 파일 다운로드",
            data=zip_bytes,
            file_name=f"report_bulk_{ts}.zip",
            mime="application/zip",
        )


st.subheader("📈 발행 추이 (최근 30일)")

sql_daily = """
SELECT
  DATE(created_at) AS issue_date,
  COUNT(*) AS cnt
FROM report_issue
WHERE created_at >= DATE('now', '-30 days', '+9 hours')
GROUP BY DATE(created_at)
ORDER BY issue_date;
"""
daily_rows = d1_query(sql_daily, [])

if daily_rows:
    daily_df = {
        "날짜": [r["issue_date"] for r in daily_rows],
        "발행 건수": [r["cnt"] for r in daily_rows],
    }
    st.line_chart(daily_df, x="날짜", y="발행 건수", use_container_width=True)
else:
    st.info("최근 30일 발행 데이터가 없습니다.")

st.subheader("🏆 FC 발행 Top 10")

sql_fc_top = """
SELECT
  fc_name,
  COUNT(*) AS cnt
FROM report_issue
GROUP BY fc_id, fc_name
ORDER BY cnt DESC
LIMIT 10;
"""
fc_rows = d1_query(sql_fc_top, [])

if fc_rows:
    fc_df = {
        "FC": [r["fc_name"] for r in fc_rows],
        "발행 건수": [r["cnt"] for r in fc_rows],
    }
    st.bar_chart(fc_df, x="FC", y="발행 건수", use_container_width=True)
else:
    st.info("FC 발행 데이터가 없습니다.")

st.subheader("👥 고객 연령대 분포")

sql_age = """
SELECT
  customer_age_band,
  COUNT(*) AS cnt
FROM report_issue
GROUP BY customer_age_band
ORDER BY cnt DESC;
"""
age_rows = d1_query(sql_age, [])

if age_rows:
    age_df = {
        "연령대": [r["customer_age_band"] for r in age_rows],
        "발행 건수": [r["cnt"] for r in age_rows],
    }
    st.bar_chart(age_df, x="연령대", y="발행 건수", use_container_width=True)
