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

def download_and_rerun(code: str, fc_code: str):
    d1_query(
        """
        INSERT INTO report_issue_event
        (compliance_code, event_type, actor_type, actor_id)
        VALUES (?, 'download', 'fc', ?);
        """,
        [code, fc_code],
    )
    st.experimental_rerun()


# =================================================
# Header
# =================================================
st.title("📄 내 발행 이력")
st.caption(f"FC: {fc['name']} ({fc['fc_code']})")

kst_now = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
st.caption(f"기준 시각(KST): {kst_now}")

st.divider()

# =================================================
# 1️⃣ 조회 필터
# =================================================

st.subheader("🔎 조회 필터")

if "searched" not in st.session_state:
    st.session_state["searched"] = False

with st.form("my_reports_filter_form"):
    f1, f2, f3, f4, f5, f6 = st.columns([2, 1.5, 1.5, 1.5, 1.5, 1])

    with f1:
        f_customer = st.text_input("고객명")

    with f2:
        f_age = st.selectbox(
            "연령대",
            ["전체", "20대", "30대", "40대", "50대", "60대", "70대"],
        )

    with f3:
        f_from = st.date_input("시작일")

    with f4:
        f_to = st.date_input("종료일")

    with f5:
        f_dl = st.selectbox(
            "다운로드 상태",
            ["전체", "다운로드완료", "다운로드필요"],
        )

    with f6:
        st.markdown("<br>", unsafe_allow_html=True)
        search_clicked = st.form_submit_button("🔍 조회", use_container_width=True)

if search_clicked:
    st.session_state["searched"] = True

if not st.session_state["searched"]:
    st.info("조건을 입력한 후 [조회] 버튼을 눌러주세요.")
    st.stop()

# 날짜 검증은 버튼 이후에
if f_from and f_to and f_from > f_to:
    st.warning("종료일은 시작일 이후여야 합니다.")
    st.stop()

where = ["i.fc_id = ?"]
params = [fc["fc_code"]]

if f_customer:
    where.append("i.customer_name LIKE ?")
    params.append(f"%{f_customer}%")

if f_age != "전체":
    where.append("i.customer_age_band = ?")
    params.append(f_age)

if f_from:
    where.append("DATE(i.created_at, '+9 hours') >= ?")
    params.append(str(f_from))

if f_to:
    where.append("DATE(i.created_at, '+9 hours') <= ?")
    params.append(str(f_to))

if f_dl == "다운로드완료":
    where.append("""
        EXISTS (
            SELECT 1
            FROM report_issue_event e
            WHERE e.compliance_code = i.compliance_code
              AND e.event_type = 'download'
              AND e.actor_type = 'fc'
              AND e.actor_id = ?
        )
    """)
    params.append(fc["fc_code"])

elif f_dl == "다운로드필요":
    where.append("""
        NOT EXISTS (
            SELECT 1
            FROM report_issue_event e
            WHERE e.compliance_code = i.compliance_code
              AND e.event_type = 'download'
              AND e.actor_type = 'fc'
              AND e.actor_id = ?
        )
    """)
    params.append(fc["fc_code"])

# =================================================
# 1️⃣ 내 발행 목록 조회
# =================================================
sql = f"""
SELECT
  i.compliance_code,
  i.customer_name,
  i.customer_age_band,
  i.start_year,
  i.end_year,
  i.sort_key,
  i.created_at,
  i.pdf_r2_key,
  i.pdf_filename,

  CASE
    WHEN COUNT(e.id) > 0 THEN 1 ELSE 0
  END AS is_downloaded

FROM report_issue i
LEFT JOIN report_issue_event e
  ON i.compliance_code = e.compliance_code
 AND e.event_type = 'download'
 AND e.actor_type = 'fc'
 AND e.actor_id = ?

WHERE {' AND '.join(where)}

GROUP BY
  i.compliance_code,
  i.customer_name,
  i.customer_age_band,
  i.start_year,
  i.end_year,
  i.sort_key,
  i.created_at,
  i.pdf_r2_key,
  i.pdf_filename

ORDER BY i.created_at DESC
LIMIT 100;

"""

rows = d1_query(sql, [fc["fc_code"]] + params)

if not rows:
    st.info("아직 발행한 리포트가 없습니다.")
    st.stop()

# =================================================
# 1️⃣ 안내려받은 리포트 조회
# =================================================

pending_rows = [r for r in rows if not r["is_downloaded"]]

pending_codes = {
    r["compliance_code"] for r in pending_rows
}

st.divider()

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
                f"""
                📊 통계기간: **{r['start_year']} ~ {r['end_year']}**  
                🔢 정렬기준: **{r['sort_key']}**
                """
            )
            with c6:
                st.download_button(
                    "⬇ 지금 다운로드",
                    data=requests.get(pdf_url, timeout=30).content,
                    file_name=r["pdf_filename"],
                    mime="application/pdf",
                    use_container_width=True,
                    key=f"pending_dl_{r['compliance_code']}",
                    on_click=download_and_rerun,
                    args=(r["compliance_code"], fc["fc_code"]),
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
            f"""
            📊 통계기간: **{r['start_year']} ~ {r['end_year']}**  
            🔢 정렬기준: **{r['sort_key']}**
            """
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
                on_click=download_and_rerun,
                args=(r["compliance_code"], fc["fc_code"]),
            )

st.divider()
