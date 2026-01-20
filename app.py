import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, List, Optional

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ----------------------------
# Config
# ----------------------------
APP_VERSION = "1.0.0"
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
CONTENT_DIR = BASE_DIR / "content" / "v1"

SEGMENTS_PATH = CONTENT_DIR / "segments.json"
STATS_PATH = CONTENT_DIR / "stats_phrases.json"
CSS_PATH = TEMPLATES_DIR / "style.css"
HTML_TEMPLATE = "pamphlet_v1.html"

BRAND_NAME = "미래에셋금융서비스"
BRAND_SUBTITLE = "통계 기반 보장 점검 안내"

# Optional logo (put a png into templates/assets/logo.png if you want)
LOGO_PATH = TEMPLATES_DIR / "assets" / "logo.png"

# HMAC secret for gateway token validation
SECRET = st.secrets.get("GATEWAY_SECRET", "")

# ----------------------------
# Token helpers
# ----------------------------
def b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))

def verify_token(token: str) -> Dict[str, Any]:
    if not SECRET:
        raise ValueError("GATEWAY_SECRET not configured in Streamlit secrets.")

    payload_b64, sig_b64 = token.split(".", 1)
    payload_raw = b64url_decode(payload_b64)
    sig = b64url_decode(sig_b64)

    expected = hmac.new(SECRET.encode("utf-8"), payload_raw, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Invalid signature")

    payload = json.loads(payload_raw.decode("utf-8"))
    now = int(time.time())
    exp = int(payload.get("exp", 0))
    if now > exp:
        raise ValueError("Token expired")

    name = str(payload.get("name", "")).strip()
    phone = str(payload.get("phone", "")).strip()
    if not name or not phone:
        raise ValueError("Missing planner fields")

    phone_digits = re.sub(r"\D", "", phone)
    return {"name": name, "phone": phone_digits, "email": payload.get("email", None)}

# ----------------------------
# Content loaders
# ----------------------------
@st.cache_data(show_spinner=False)
def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

def segment_key(age_band: str, gender: str) -> str:
    # age_band: "20대"/"30대"/"40대"/"50대"/"60대 이상" -> 20/30/40/50
    if age_band.startswith("20"): a = "20"
    elif age_band.startswith("30"): a = "30"
    elif age_band.startswith("40"): a = "40"
    else: a = "50"
    g = "M" if gender == "남성" else "F"
    return f"{a}_{g}"

# ----------------------------
# Rendering (HTML)
# ----------------------------
def file_to_data_uri(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{data}"

def render_html(context: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"])
    )
    template = env.get_template(HTML_TEMPLATE)
    return template.render(**context)

# ----------------------------
# PDF generation (WeasyPrint if available)
# ----------------------------
def html_to_pdf_bytes(html: str, css_file: Path) -> bytes:
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except Exception:
        raise RuntimeError("WeasyPrint not installed. Install weasyprint to enable HTML→PDF.")

    # base_url is needed for relative asset loading
    return HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(stylesheets=[CSS(filename=str(css_file))])

# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="보장 점검 유인 팜플렛", layout="centered")

# Read token
token = st.query_params.get("token")
if not token:
    st.error("유효한 접속 정보가 없습니다. M.POST 게이트웨이 링크로 접속해 주세요.")
    st.stop()

# Verify planner
try:
    planner = verify_token(token)
except Exception as e:
    st.error(f"접속 검증 실패: {e}")
    st.stop()

segments_db = load_json(SEGMENTS_PATH)
stats_db = load_json(STATS_PATH)

st.success("설계사 인증 완료")
st.write(f"설계사: **{planner['name']}**")
st.write(f"연락처: **{planner['phone']}**")
st.divider()

# Customer input
customer_name = st.text_input("고객 성명", value="")
gender = st.selectbox("성별", ["남성", "여성"])
age_band = st.selectbox("연령대", ["20대", "30대", "40대", "50대", "60대 이상"])

# Build key and load segment/cards
key = segment_key(age_band, gender)

segment = segments_db["segments"].get(key)
cards = stats_db["cards"].get(key)

if not segment or not cards:
    st.error(f"콘텐츠 세트가 없습니다: {key}")
    st.stop()

# Controlled adjustments (선택형/제한형으로 유지)
st.subheader("문구 조정(선택/제한)")
summary_lines = segment["summary_lines"][:]
gap_questions = segment["gap_questions"][:]
cta_text = segment["cta"]

# 제한된 수정: 요약 3줄은 텍스트 입력(추후 드롭다운으로 바꾸기 권장)
summary_lines[0] = st.text_input("요약 1", value=summary_lines[0])
summary_lines[1] = st.text_input("요약 2", value=summary_lines[1])
summary_lines[2] = st.text_input("요약 3", value=summary_lines[2])

# 질문 2개는 선택형 느낌으로 텍스트 수정 허용(추후 옵션화 권장)
gap_questions[0] = st.text_input("점검 질문 1", value=gap_questions[0])
gap_questions[1] = st.text_input("점검 질문 2", value=gap_questions[1])

# CTA는 제한 수정
cta_text = st.text_area("CTA 문구", value=cta_text, height=90)

# Structure rows (고정)
structure_rows = [
    {"area": "진단비", "reason": "진단 직후 초기 자금 여력(목돈) 점검"},
    {"area": "치료비", "reason": "치료 과정의 반복 비용·통원/수술 부담 점검"},
    {"area": "생활·소득", "reason": "치료로 인한 소득 공백·가계 영향 점검"}
]

# Build context
context = {
    "css_path": str(CSS_PATH),  # used by template link tag (for preview we inline below)
    "logo_data_uri": file_to_data_uri(LOGO_PATH),
    "brand_name": BRAND_NAME,
    "brand_subtitle": BRAND_SUBTITLE,
    "version": APP_VERSION,
    "customer": {
        "name": customer_name.strip() or "고객",
        "gender": gender,
        "age_band": age_band
    },
    "planner": {
        "name": planner["name"],
        "phone": planner["phone"],
        "email": planner.get("email", None),
        "company": "미래에셋금융서비스"
    },
    "segment": {
        "headline": segment["headline"].replace("{customer_name}", (customer_name.strip() or "고객")),
        "summary_lines": summary_lines,
        "gap_questions": gap_questions,
        "cta": cta_text
    },
    "stats": {
        "base_year": stats_db.get("base_year", "2024"),
        "source": stats_db.get("source", "공식 보건의료 통계(요약)"),
        "cards": cards
    },
    "structure_rows": structure_rows,
    "footer": stats_db.get("footer", {
        "disclaimer": "본 자료는 동일 연령·성별 집단의 통계 기반 참고 자료이며, 개인별 진단·보장 수준은 상이할 수 있습니다. 정확한 확인은 종합 보장분석을 통해 가능합니다.",
        "legal_note": "본 자료는 편의를 위해 제공되며 법적 효력을 갖지 않습니다."
    })
}

# Render HTML
html = render_html(context)

# Inline CSS for Streamlit preview (so <link> isn't needed)
css_text = CSS_PATH.read_text(encoding="utf-8")
html_with_inline_css = html.replace(
    f'<link rel="stylesheet" href="{context["css_path"]}" />',
    f"<style>\n{css_text}\n</style>"
)

st.subheader("미리보기")
components.html(html_with_inline_css, height=980, scrolling=True)

st.divider()
st.subheader("확정 및 PDF 출력")

if st.button("확정 후 PDF 생성"):
    if not customer_name.strip():
        st.warning("고객 성명을 입력해 주세요.")
        st.stop()

    # Re-render with correct customer name guaranteed
    context["customer"]["name"] = customer_name.strip()
    context["segment"]["headline"] = segment["headline"].replace("{customer_name}", customer_name.strip())

    html_final = render_html(context)

    try:
        pdf_bytes = html_to_pdf_bytes(html_final, CSS_PATH)
        filename = f"보장점검안내_{customer_name.strip()}_{age_band}_{gender}.pdf"
        st.download_button(
            "PDF 다운로드",
            data=pdf_bytes,
            file_name=filename,
            mime="application/pdf"
        )
    except Exception as e:
        st.error(
            "PDF 생성 모듈(WeasyPrint)이 설치되어 있지 않거나 환경 제약이 있습니다.\n\n"
            f"오류: {e}\n\n"
            "대안: WeasyPrint 설치 후 재시도하거나, ReportLab 방식으로 PDF 생성 로직을 사용하세요."
        )
