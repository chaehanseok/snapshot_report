import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, List, Optional

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape

from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

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

def reportlab_snapshot_pdf(context: dict) -> bytes:
    """
    Coverage Snapshot Report (single page) generated via ReportLab.
    Uses the already-prepared context values (customer/segment/stats/planner/footer).
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    # Margins
    left = 14 * mm
    right = width - 14 * mm
    y = height - 16 * mm

    def draw_text(x, y, text, size=10, bold=False):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y, text)

    def draw_wrapped(x, y, text, max_width, size=10, leading=14):
        # very simple wrap by character count approximation (sufficient for MVP)
        c.setFont("Helvetica", size)
        # heuristic: average glyph width ~0.5*size points; convert max_width points to char count
        approx_chars = max(12, int(max_width / (size * 0.55)))
        lines = []
        s = text.strip()
        while s:
            chunk = s[:approx_chars]
            # try break at last space
            cut = chunk.rfind(" ")
            if cut > 20 and len(s) > approx_chars:
                chunk = chunk[:cut]
            lines.append(chunk)
            s = s[len(chunk):].lstrip()
        for line in lines:
            c.drawString(x, y, line)
            y -= leading
        return y

    customer = context["customer"]
    segment = context["segment"]
    stats = context["stats"]
    planner = context["planner"]
    footer = context["footer"]
    structure_rows = context.get("structure_rows", [])

    # Header
    draw_text(left, y, context.get("brand_name", "미래에셋금융서비스"), size=11, bold=True)
    draw_text(left, y - 6 * mm, context.get("brand_subtitle", "Coverage Snapshot Report (Pre-Analysis)"), size=9)
    draw_text(right - 70 * mm, y, "Coverage Snapshot Report", size=12, bold=True)
    draw_text(right - 70 * mm, y - 6 * mm, f"{customer['age_band']} · {customer['gender']}", size=9)
    y -= 14 * mm
    c.line(left, y, right, y)
    y -= 10 * mm

    # Title block
    draw_text(left, y, f"{customer['name']} 고객님을 위한 보장 점검 안내", size=14, bold=True)
    y -= 8 * mm
    draw_text(left, y, f"기준연도 {stats.get('base_year','')} · {stats.get('source','')} · v{context.get('version','')}", size=8)
    y -= 10 * mm

    # Summary
    draw_text(left, y, "요약", size=11, bold=True)
    y -= 7 * mm
    for line in segment.get("summary_lines", [])[:3]:
        y = draw_wrapped(left + 4 * mm, y, f"• {line}", max_width=(right - left - 6 * mm), size=9, leading=12)
    y -= 4 * mm

    # Stats cards (simple boxed rows)
    draw_text(left, y, "현황 통계 예시", size=11, bold=True)
    y -= 7 * mm

    cards = stats.get("cards", [])
    box_w = (right - left - 6 * mm) / 3
    box_h = 28 * mm
    x0 = left
    y0 = y - box_h

    for i, card in enumerate(cards[:3]):
        x = x0 + i * (box_w + 3 * mm)
        c.roundRect(x, y0, box_w, box_h, 4 * mm, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 3 * mm, y0 + box_h - 7 * mm, card.get("title", ""))
        c.setFont("Helvetica", 8.5)
        _y = y0 + box_h - 13 * mm
        _y = draw_wrapped(x + 3 * mm, _y, card.get("value", ""), max_width=(box_w - 6 * mm), size=8.5, leading=11)

    y = y0 - 10 * mm

    # Gap questions
    draw_text(left, y, "점검 질문", size=11, bold=True)
    y -= 7 * mm
    for idx, q in enumerate(segment.get("gap_questions", [])[:2], start=1):
        y = draw_wrapped(left + 4 * mm, y, f"{idx}. {q}", max_width=(right - left - 6 * mm), size=9, leading=12)
    y -= 4 * mm

    # Structure table (2 columns)
    draw_text(left, y, "필요 보장 구조(개요)", size=11, bold=True)
    y -= 7 * mm

    col1 = left
    col2 = left + 35 * mm
    c.rect(left, y - 22 * mm, right - left, 22 * mm, stroke=1, fill=0)
    c.line(col2, y, col2, y - 22 * mm)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(col1 + 3 * mm, y - 6 * mm, "보장 영역")
    c.drawString(col2 + 3 * mm, y - 6 * mm, "점검이 필요한 이유")
    c.line(left, y - 8 * mm, right, y - 8 * mm)

    c.setFont("Helvetica", 8.5)
    row_y = y - 14 * mm
    for r in structure_rows[:3]:
        c.drawString(col1 + 3 * mm, row_y, str(r.get("area", "")))
        draw_wrapped(col2 + 3 * mm, row_y, str(r.get("reason", "")), max_width=(right - col2 - 6 * mm), size=8.5, leading=11)
        row_y -= 7 * mm

    y = (y - 22 * mm) - 8 * mm

    # CTA box
    c.roundRect(left, y - 18 * mm, right - left, 18 * mm, 4 * mm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(left + 3 * mm, y - 6 * mm, "확인하고 싶으시면, 한 번에 점검해보시죠.")
    c.setFont("Helvetica", 8.8)
    draw_wrapped(left + 3 * mm, y - 12 * mm, segment.get("cta", ""), max_width=(right - left - 6 * mm), size=8.8, leading=11)
    y -= 24 * mm

    # Planner box
    c.roundRect(left, y - 18 * mm, right - left, 18 * mm, 4 * mm, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(left + 3 * mm, y - 6 * mm, "상담 및 보장 점검 문의")
    c.setFont("Helvetica", 9)
    c.drawString(left + 3 * mm, y - 12 * mm, f"{planner.get('name','')} 컨설턴트 | 전화: {planner.get('phone','')}")
    if planner.get("email"):
        c.setFont("Helvetica", 8.5)
        c.drawString(left + 3 * mm, y - 16 * mm, f"이메일: {planner.get('email')}")
    y -= 26 * mm

    # Footer disclaimer
    c.setFont("Helvetica", 7.5)
    y = draw_wrapped(left, 16 * mm + 10, footer.get("disclaimer", ""), max_width=(right - left), size=7.5, leading=10)
    c.setFont("Helvetica", 7.5)
    c.drawString(left, 12 * mm, footer.get("legal_note", ""))

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()

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

    # 컨텍스트에 고객명 확정 반영
    context["customer"]["name"] = customer_name.strip()
    context["segment"]["headline"] = segment["headline"].replace("{customer_name}", customer_name.strip())

    try:
        pdf_bytes = reportlab_snapshot_pdf(context)
        filename = f"보장점검안내_{customer_name.strip()}_{age_band}_{gender}.pdf"
        st.download_button(
            "PDF 다운로드",
            data=pdf_bytes,
            file_name=filename,
            mime="application/pdf"
        )
    except Exception as e:
        st.error(f"PDF 생성(ReportLab) 중 오류가 발생했습니다.\n\n오류: {e}")

