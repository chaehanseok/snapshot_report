import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

from utils.auth import verify_token


# =================================================
# 0️⃣ Page Config (⚠️ 반드시 최상단, 1번만)
# =================================================
st.set_page_config(
    page_title="관리자 · 발행 대시보드",
    layout="wide",
)

st.title("🛠 관리자 페이지")
st.caption("관리자 전용 발행 관리 화면입니다.")


# =================================================
# 1️⃣ Token 사전 체크
# =================================================
token = st.query_params.get("token")

if not token:
    st.error("❌ 관리자 토큰이 없습니다.")
    st.info("정상적인 관리자 링크로 접속해 주세요.")
    st.stop()

# query_params가 list로 들어오는 경우 대비
if isinstance(token, list):
    token = token[0]


# =================================================
# 2️⃣ Token 검증
# =================================================
try:
    admin = verify_token(token)
except Exception as e:
    st.error("❌ 관리자 인증 실패")
    st.code(str(e))
    st.stop()

if admin.get("role") != "admin":
    st.error("❌ 관리자 권한이 없습니다.")
    st.stop()


# =================================================
# 3️⃣ 인증 성공 UI
# =================================================
kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")

st.success(f"관리자 로그인 성공: {admin['name']}")
st.caption(f"기준 시각(KST): {kst_now}")

st.divider()


# =================================================
# 4️⃣ D1 Query Helper
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
# 5️⃣ KPI 요약
# =================================================
st.subheader("📊 발행 요약")

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
else:
    st.info("발행 데이터가 없습니다.")
