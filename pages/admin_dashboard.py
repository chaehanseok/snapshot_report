import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import zipfile
from io import BytesIO

from utils.auth import verify_token
from utils.r2 import generate_presigned_pdf_url


# =================================================
# Page Config (⚠️ 반드시 최상단, 1회만)
# =================================================
st.set_page_config(
    page_title="관리자 · 발행 대시보드",
    layout="wide",
)

st.title("🛠 관리자 페이지")
st.caption("관리자 전용 발행 관리 화면입니다.")


# =================================================
# 0️⃣ 관리자 인증
# =================================================
token = st.query_params.get("token")

if not token:
    st.error("❌ 관리자 토큰이 없습니다.")
    st.info("정상적인 관리자 링크로 접속해 주세요.")
    st.stop()

if isinstance(token, list):
    token = token[0]

try:
    admin = verify_token(token)
except Exception as e:
    st.error("❌ 관리자 인증 실패")
    st.code(str(e))
    st.stop()

if admin.get("role") != "admin":
    st.error("❌ 관리자 권한이 없습니다.")
    st.stop()

kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
st.success(f"관리자 로그인 성공: {admin['name']}")
st.caption(f"기준 시각(KST): {kst_now}")

st.divider()


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
    r.raise_for_status()
    data = r.json()
    return data["result"][0]["results"] if data.get("result") else []


# =================================================
# 1️⃣ KPI 요약
# =================================================
sql_kpi = """
SELECT
  COUNT(*) AS total_cnt,
  COUNT(DISTINCT fc_id) AS fc_cnt,
  SUM(
    CASE WHEN DATE(created_at) = DATE('now', '+9 hours')
    THEN 1 ELSE 0 END
  ) AS today_cnt,
  MAX(created_at) AS last_issue_at
FROM report_issue;
"""
kpi = d1_query(sql_kpi, [])

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

f1, f2, f3, f4, f5 = st.columns(5)

with f1:
    fc_name = st.text_input("FC 이름")

with f2:
    customer_name = st.text_input("고객명")  # ✅ 추가

with f3:
    age_band = st.selectbox(
        "연령대",
        ["전체", "20대", "30대", "40대", "50대", "60대", "70대"],
    )

with f4:
    date_from = st.date_input("시작일")

with f5:
    date_to = st.date_input("종료일")   # ✅ 이것만 추가

where = ["1=1"]
params = []

if fc_name:
    where.append("fc_name LIKE ?")
    params.append(f"%{fc_name}%")

if customer_name:  # ✅ 추가
    where.append("customer_name LIKE ?")
    params.append(f"%{customer_name}%")

if age_band != "전체":
    where.append("customer_age_band = ?")
    params.append(age_band)

if date_from:
    where.append("DATE(created_at) >= ?")
    params.append(str(date_from))

if date_to:
    where.append("DATE(created_at) <= ?")
    params.append(str(date_to))

if date_from and date_to and date_from > date_to:
    st.warning("종료일은 시작일 이후여야 합니다.")
    st.stop()

# =================================================
# 3️⃣ 발행 목록 조회
# =================================================
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

st.subheader("📋 발행 목록")

if not rows:
    st.info("조회 결과가 없습니다.")
    st.stop()


# =================================================
# 4️⃣ 발행 목록 테이블
# =================================================
for r in rows:
    with st.container(border=True):
        c1, c2, c3, c4, c5, c6 = st.columns([3, 2, 2, 2, 1, 1])

        c1.markdown(f"**{r['compliance_code']}**")
        c2.write(r["fc_name"])
        c3.write(r["customer_name"] or "-")
        c4.write(r["customer_age_band"])

        with c5:
            st.link_button(
                "상세",
                f"/admin_issue_detail?code={r['compliance_code']}&token={token}",
                use_container_width=True,
            )

        with c6:
            pdf_url = generate_presigned_pdf_url(r["pdf_r2_key"])
            st.link_button(
                "PDF",
                pdf_url,
                use_container_width=True,
            )

st.divider()


# =================================================
# 5️⃣ 일괄 다운로드 (ZIP)
# =================================================
st.subheader("📦 일괄 다운로드")

def build_zip_from_issues(issues):
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in issues:
            signed_url = generate_presigned_pdf_url(r["pdf_r2_key"])
            resp = requests.get(signed_url, timeout=30)
            if resp.ok:
                z.writestr(r["pdf_filename"], resp.content)
    zip_buf.seek(0)
    return zip_buf.getvalue()

if st.button("현재 조건 전체 ZIP 다운로드"):
    with st.spinner("ZIP 생성 중..."):
        zip_bytes = build_zip_from_issues(rows)

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
            "📥 ZIP 다운로드",
            zip_bytes,
            file_name=f"reports_{ts}.zip",
            mime="application/zip",
        )

st.divider()


# =================================================
# 6️⃣ 통계 차트
# =================================================
st.subheader("📈 최근 30일 발행 추이")

sql_daily = """
SELECT
  DATE(created_at) AS d,
  COUNT(*) AS cnt
FROM report_issue
WHERE created_at >= DATE('now', '-30 days', '+9 hours')
GROUP BY d
ORDER BY d;
"""
daily = d1_query(sql_daily, [])

if daily:
    st.line_chart(
        {
            "날짜": [r["d"] for r in daily],
            "발행 건수": [r["cnt"] for r in daily],
        },
        x="날짜",
        y="발행 건수",
        use_container_width=True,
    )

st.subheader("🏆 FC 발행 TOP 10")

sql_fc = """
SELECT
  fc_name,
  COUNT(*) AS cnt
FROM report_issue
GROUP BY fc_id, fc_name
ORDER BY cnt DESC
LIMIT 10;
"""
fc_rows = d1_query(sql_fc, [])

if fc_rows:
    st.bar_chart(
        {
            "FC": [r["fc_name"] for r in fc_rows],
            "발행 건수": [r["cnt"] for r in fc_rows],
        },
        x="FC",
        y="발행 건수",
        use_container_width=True,
    )
