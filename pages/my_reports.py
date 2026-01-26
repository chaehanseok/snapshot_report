import streamlit as st
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.r2 import generate_presigned_pdf_url
from utils.auth import verify_token

def to_kst(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", ""))
    return dt.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


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

# def insert_view_once_per_day(compliance_code: str, fc_code: str):
#     exists = d1_query(
#         """
#         SELECT 1
#         FROM report_issue_event
#         WHERE
#           compliance_code = ?
#           AND event_type = 'view'
#           AND actor_type = 'fc'
#           AND actor_id = ?
#           AND DATE(created_at, '+9 hours') = DATE('now', '+9 hours')
#         LIMIT 1;
#         """,
#         [compliance_code, fc_code],
#     )

#     if not exists:
#         d1_query(
#             """
#             INSERT INTO report_issue_event
#             (compliance_code, event_type, actor_type, actor_id)
#             VALUES (?, 'view', 'fc', ?);
#             """,
#             [compliance_code, fc_code],
#         )


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
  start_year,
  end_year,
  sort_key,
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
# 1️⃣ 안내려받은 리포트 조회
# =================================================

sql_pending = """
SELECT
  i.compliance_code,
  i.customer_name,
  i.customer_age_band,
  i.created_at,
  i.pdf_r2_key,
  i.pdf_filename
FROM report_issue i
WHERE i.fc_id = ?
AND EXISTS (
    SELECT 1
    FROM report_issue_event e
    WHERE
      e.compliance_code = i.compliance_code
      AND e.event_type = 'view'
      AND e.actor_type = 'fc'
      AND e.actor_id = ?
)
AND NOT EXISTS (
    SELECT 1
    FROM report_issue_event e
    WHERE
      e.compliance_code = i.compliance_code
      AND e.event_type = 'download'
      AND e.actor_type = 'fc'
      AND e.actor_id = ?
)
ORDER BY i.created_at DESC;
"""

pending_rows = d1_query(
    sql_pending,
    [fc["fc_code"], fc["fc_code"], fc["fc_code"]],
)

# ✅ 여기 추가
pending_codes = {
    p["compliance_code"] for p in pending_rows
}

# =================================================
# 2️⃣ 발행 목록 표시
# =================================================
bucket = st.secrets["R2_BUCKET_NAME"]
endpoint = st.secrets["R2_ENDPOINT"]

if pending_rows:
    st.subheader("⚠️ 아직 다운로드하지 않은 리포트")

    for r in pending_rows:
        pdf_url = generate_presigned_pdf_url(r["pdf_r2_key"])

        with st.container(border=True):
            c1, c2, c3, c4, c5, c6 = st.columns([3, 2, 1, 2, 2, 1.5])

            c1.markdown(f"**{r['compliance_code']}**")

            c2.write(r["customer_name"] or "-")
            c3.write(r["customer_age_band"])
            c4.write(to_kst(r["created_at"]))
            c5.caption(
                f"{status}\n"
                f"📊 통계기간: {r['start_year']} ~ {r['end_year']} | "
                f"🔢 정렬기준: {r['sort_key']}"
            )
            with c6:
                st.download_button(
                    "⬇ 지금 다운로드",
                    data=requests.get(pdf_url, timeout=30).content,
                    file_name=r["pdf_filename"],
                    mime="application/pdf",
                    use_container_width=True,
                    key=f"pending_dl_{r['compliance_code']}",
                    on_click=lambda code=r["compliance_code"]: d1_query(
                        """
                        INSERT INTO report_issue_event
                        (compliance_code, event_type, actor_type, actor_id)
                        VALUES (?, 'download', 'fc', ?);
                        """,
                        [code, fc["fc_code"]],
                    ),
                )
st.divider()

for r in rows:
    pdf_url = generate_presigned_pdf_url(r2_key=r["pdf_r2_key"])

    # 🔹 다운로드 상태 판단
    downloaded = r["compliance_code"] not in pending_codes
    status = "⬇ 다운로드 완료" if downloaded else "⬇ 다운로드 필요"

    with st.container(border=True):
        c1, c2, c3, c4, c5, c6 = st.columns([3, 2, 1, 2, 2, 1.5])

        # 🔹 심의번호 + 상태
        c1.markdown(f"**{r['compliance_code']}**")
        c1.caption(status)  # ⬇ 다운로드 완료 / ⬇ 미다운로드

        c2.write(r["customer_name"] or "-")
        c3.write(r["customer_age_band"])
        c4.write(to_kst(r["created_at"]))
        c5.caption(
            f"{status}\n"
            f"📊 통계기간: {r['start_year']} ~ {r['end_year']} | "
            f"🔢 정렬기준: {r['sort_key']}"
        ) 

        # ⬇ 다운로드 (유일한 액션)
        with c6:
            st.download_button(
                label="⬇ PDF 다운로드",
                data=requests.get(pdf_url, timeout=30).content,
                file_name=r["pdf_filename"],
                mime="application/pdf",
                use_container_width=True,
                key=f"dl_{r['compliance_code']}",
                on_click=lambda code=r["compliance_code"]: d1_query(
                    """
                    INSERT INTO report_issue_event
                    (compliance_code, event_type, actor_type, actor_id)
                    VALUES (?, 'download', 'fc', ?);
                    """,
                    [code, fc["fc_code"]],
                ),
            )

