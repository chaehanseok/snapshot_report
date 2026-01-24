import streamlit as st

# 1️⃣ 무조건 UI부터
st.set_page_config(
    page_title="관리자 · 발행 대시보드",
    layout="wide",
)

st.title("🛠 관리자 페이지")
st.caption("관리자 전용 발행 관리 화면입니다.")

# 2️⃣ token 존재 여부만 먼저 체크 (절대 verify_token 호출 ❌)
token = st.query_params.get("token")

if not token:
    st.error("❌ 관리자 토큰이 없습니다.")
    st.info("정상적인 관리자 링크로 접속해 주세요.")
    st.stop()

# query_params가 list로 들어오는 경우 대비
if isinstance(token, list):
    token = token[0]

# 3️⃣ 이제서야 try/except로 검증
try:
    admin = verify_token(token)
except Exception as e:
    st.error("❌ 관리자 인증 실패")
    st.code(str(e))
    st.stop()

# 4️⃣ role 체크
if admin.get("role") != "admin":
    st.error("❌ 관리자 권한이 없습니다.")
    st.stop()

# 5️⃣ 여기부터 진짜 관리자 화면
st.success(f"관리자 로그인: {admin['name']}")
