import streamlit as st
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.r2 import generate_presigned_pdf_url
from utils.auth import verify_token

# =================================================
# Page Config (⚠️ 반드시 최상단)
# =================================================
st.set_page_config(
    page_title="내 발행 이력",
    layout="wide",
)

# =================================================
# 인증 (session_state 기반)
# =================================================
token = st.session_state.get("auth_token")

if not token:
    st.error("접속 토큰이 없습니다. 처음 화면에서 다시 접속해 주세요.")
    st.stop()

try:
    fc = verify_token(token)
except Exception as e:
    st.error(f"인증 실패: {e}")
    st.stop()

if not fc.get("fc_code"):
    st.error("FC 계정이 아닙니다.")
    st.stop()

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

# =================================================
# Header
# =================================================
st.title("📄 내 발행 이력")
st.caption(f"FC: {fc['name']} ({fc['fc_code']})")

kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
st.caption(f"기준 시각(KST): {kst_now}")

st.divider()

# =================================================
# 1️⃣ 내 발행 목록 조회
# =================================================
sql = """
SELECT
  compliance_code,
  customer_name,
  customer_age_band,
  created_at,
  pdf_r2_key,
  pdf_filename
FROM report_issue
WHERE fc_id = ?
ORDER BY created_at DESC
LIMIT 100;
"""

rows = d1_query(sql, [fc["fc_code"]])

if not rows:
    st.info("아직 발행한 리포트가 없습니다.")
    st.stop()

# =================================================
# 2️⃣ 발행 목록 표시
# =================================================
bucket = st.secrets["R2_BUCKET_NAME"]
endpoint = st.secrets["R2_ENDPOINT"]

for r in rows:
    pdf_url = generate_presigned_pdf_url(r["pdf_r2_key"])

    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])

        c1.markdown(f"**{r['compliance_code']}**")
        c2.write(r["customer_name"] or "-")
        c3.write(r["customer_age_band"])
        c4.write(r["created_at"][:16])

        with c5:
            if st.button("📥", key=f"dl_{r['compliance_code']}"):
                pdf_bytes = requests.get(pdf_url, timeout=30).content

                # 🔹 다운로드 이벤트 기록
                d1_query(
                    """
                    INSERT INTO report_issue_event
                    (compliance_code, event_type, actor_type, actor_id)
                    VALUES (?, 'download', 'fc', ?);
                    """,
                    [r["compliance_code"], fc["fc_code"]],
                )

                st.download_button(
                    label="PDF 저장",
                    data=pdf_bytes,
                    file_name=r["pdf_filename"],
                    mime="application/pdf",
                    key=f"dl_btn_{r['compliance_code']}",
                )

        with st.expander("미리보기"):
            # 🔹 미리보기 이벤트 기록
            d1_query(
                """
                INSERT INTO report_issue_event
                (compliance_code, event_type, actor_type, actor_id)
                VALUES (?, 'view', 'fc', ?);
                """,
                [r["compliance_code"], fc["fc_code"]],
            )

            st.components.v1.iframe(pdf_url, height=600)
