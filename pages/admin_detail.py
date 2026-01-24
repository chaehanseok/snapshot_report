import streamlit as st
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

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
# 관리자 인증
# =================================================
def verify_admin():
    token = st.query_params.get("token")
    if not token:
        st.error("관리자 토큰이 없습니다.")
        st.stop()

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
    page_title="관리자 · 발행 상세",
    layout="wide",
)

admin = verify_admin()

code = st.query_params.get("code")
if not code:
    st.error("심의번호(code)가 전달되지 않았습니다.")
    st.stop()

st.title("📄 발행 상세")
st.caption(f"심의번호: {code}")

# 🔹 관리자 상세 열람 이벤트 기록
d1_query(
    """
    INSERT INTO report_issue_event
    (compliance_code, event_type, actor_type, actor_id)
    VALUES (?, 'admin_view', 'admin', ?);
    """,
    [code, admin.get("id")],
)

st.divider()

# =================================================
# 1️⃣ 발행 메타 조회
# =================================================
sql_issue = """
SELECT *
FROM report_issue
WHERE compliance_code = ?
LIMIT 1;
"""
rows = d1_query(sql_issue, [code])

if not rows:
    st.error("해당 발행 이력을 찾을 수 없습니다.")
    st.stop()

issue = rows[0]

# =================================================
# 2️⃣ 메타 정보 표시
# =================================================
c1, c2, c3 = st.columns(3)
c1.metric("FC", issue["fc_name"])
c2.metric("고객", issue["customer_name"] or "-")
c3.metric("연령대", issue["customer_age_band"])

c4, c5, c6 = st.columns(3)
c4.metric("통계기간", f"{issue['start_year']} ~ {issue['end_year']}")
c5.metric("정렬 기준", issue["sort_key"])
c6.metric("Segments 버전", issue["segments_version"])

created_kst = issue["created_at"]
st.caption(f"발행 시각(KST): {created_kst}")

st.divider()

# =================================================
# 3️⃣ PDF 미리보기 / 다운로드
# =================================================
bucket = st.secrets["R2_BUCKET_NAME"]
endpoint = st.secrets["R2_ENDPOINT"]
pdf_url = f"{endpoint}/{bucket}/{issue['pdf_r2_key']}"

st.subheader("📎 PDF 문서")

c1, c2 = st.columns([1, 3])

with c1:
    st.link_button("🌐 브라우저로 열기", pdf_url)

    # 🔹 PDF 미리보기 이벤트
    if st.button("👀 PDF 미리보기 기록"):
        d1_query(
            """
            INSERT INTO report_issue_event
            (compliance_code, event_type, actor_type, actor_id)
            VALUES (?, 'view', 'admin', ?);
            """,
            [code, admin.get("id")],
        )
        st.success("미리보기 이벤트 기록됨")

    st.divider()

    # 🔹 단건 다운로드
    if st.button("📥 PDF 다운로드"):
        pdf_bytes = requests.get(pdf_url, timeout=30).content

        d1_query(
            """
            INSERT INTO report_issue_event
            (compliance_code, event_type, actor_type, actor_id)
            VALUES (?, 'download', 'admin', ?);
            """,
            [code, admin.get("id")],
        )

        st.download_button(
            label="⬇️ 파일 저장",
            data=pdf_bytes,
            file_name=issue["pdf_filename"],
            mime="application/pdf",
        )

with c2:
    st.components.v1.iframe(pdf_url, height=720)

st.divider()

# =================================================
# 4️⃣ 이벤트 로그 타임라인
# =================================================
st.subheader("🕒 이벤트 로그")

sql_log = """
SELECT event_type, actor_type, actor_id, created_at
FROM report_issue_event
WHERE compliance_code = ?
ORDER BY created_at DESC;
"""
logs = d1_query(sql_log, [code])

if not logs:
    st.info("이벤트 로그가 없습니다.")
else:
    st.dataframe(
        [
            {
                "이벤트": l["event_type"],
                "주체": l["actor_type"],
                "ID": l["actor_id"] or "-",
                "시각(KST)": l["created_at"],
            }
            for l in logs
        ],
        use_container_width=True,
        hide_index=True,
    )
