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
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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

BASE_DIR = Path(__file__).parent
FONT_PATH = BASE_DIR / "templates" / "assets" / "fonts" / "NotoSansKR-Regular.ttf"

def register_korean_fonts():
    if "NotoSansKR" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont("NotoSansKR", str(FONT_PATH))
        )

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

def _format_phone(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11:  # 010xxxxxxxx
        return f"{d[:3]}-{d[3:7]}-{d[7:]}"
    if len(d) == 10:  # 0xx/010-xxx-xxxx
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return phone


def reportlab_snapshot_pdf(context: dict) -> bytes:
    """
    Coverage Snapshot Report (1-page) via ReportLab.
    Design goals: typographic hierarchy, consistent spacing, tidy cards, light table styling.
    """
    register_korean_fonts()

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # ===== Layout constants =====
    M_L = 14 * mm
    M_R = 14 * mm
    M_T = 14 * mm
    M_B = 12 * mm

    X0 = M_L
    X1 = W - M_R
    CW = X1 - X0

    # Spacing rules
    GAP_SECTION_TOP = 10 * mm
    GAP_TITLE_BOTTOM = 4 * mm
    GAP_SECTION_BOTTOM = 6 * mm

    # ===== Style helpers =====
    def font(name: str, size: float):
        c.setFont(name, size)

    def text(x, y, s, name="NotoSansKR", size=10, color=colors.black):
        c.setFillColor(color)
        font(name, size)
        c.drawString(x, y, s)

    def text_r(x, y, s, name="NotoSansKR", size=10, color=colors.black):
        c.setFillColor(color)
        font(name, size)
        c.drawRightString(x, y, s)

    def line(xa, ya, xb, yb, w=0.6, color=colors.HexColor("#E5E7EB")):
        c.setStrokeColor(color)
        c.setLineWidth(w)
        c.line(xa, ya, xb, yb)

    def round_box(x, y, w, h, r=4*mm, stroke=colors.HexColor("#E5E7EB"), fill=None, lw=0.8):
        c.setLineWidth(lw)
        c.setStrokeColor(stroke)
        if fill is None:
            c.setFillColor(colors.white)
            c.roundRect(x, y, w, h, r, stroke=1, fill=0)
        else:
            c.setFillColor(fill)
            c.roundRect(x, y, w, h, r, stroke=1, fill=1)

    def wrap_lines(s: str, max_width: float, font_name: str, font_size: float):
        """
        Robust-ish wrapping using ReportLab stringWidth.
        """
        font(font_name, font_size)
        words = list(s.strip().split())
        if not words:
            return [""]

        lines = []
        cur = words[0]
        for w in words[1:]:
            cand = cur + " " + w
            if c.stringWidth(cand, font_name, font_size) <= max_width:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    def draw_paragraph(x: float, y: float, s: str, max_width: float, font_name="NotoSansKR", font_size=9.5, leading=13):
        lines = wrap_lines(s, max_width, font_name, font_size)
        c.setFillColor(colors.black)
        font(font_name, font_size)
        for ln in lines:
            c.drawString(x, y, ln)
            y -= leading
        return y

    def draw_bullets(x: float, y: float, items, max_width: float, bullet="•", font_size=9.5, leading=13):
        for it in items:
            # Render bullet + wrapped continuation lines
            lines = wrap_lines(str(it).strip(), max_width - 8*mm, "NotoSansKR", font_size)
            font("NotoSansKR", font_size)
            c.setFillColor(colors.black)
            c.drawString(x, y, f"{bullet}")
            c.drawString(x + 4*mm, y, lines[0])
            y -= leading
            for ln in lines[1:]:
                c.drawString(x + 4*mm, y, ln)
                y -= leading
        return y

    def clamp_two_lines(s: str, max_width: float, font_size: float):
        """
        Keep at most 2 wrapped lines; add ellipsis if overflow.
        """
        lines = wrap_lines(s, max_width, "NotoSansKR", font_size)
        if len(lines) <= 2:
            return lines

        # Need ellipsis on second line
        l1 = lines[0]
        l2 = lines[1]
        ell = "…"
        # shrink l2 until fits with ellipsis
        font("NotoSansKR", font_size)
        while c.stringWidth(l2 + ell, "NotoSansKR", font_size) > max_width and len(l2) > 1:
            l2 = l2[:-1]
        return [l1, l2 + ell]

    # ===== Context =====
    customer = context["customer"]
    segment = context["segment"]
    stats = context["stats"]
    planner = context["planner"]
    footer = context["footer"]

    # Ensure phone is formatted
    planner_phone = _format_phone(planner.get("phone", ""))

    # ===== Start drawing =====
    y = H - M_T

    # Header bar (brand left, meta right)
    text(X0, y, context.get("brand_name", "미래에셋금융서비스"), name="NotoSansKR-Bold", size=11)
    text(X0, y - 5*mm, context.get("brand_subtitle", "Coverage Snapshot (Pre-Analysis)"), size=9, color=colors.HexColor("#6B7280"))

    meta = f"v{context.get('version','')}"
    text_r(X1, y, meta, size=9, color=colors.HexColor("#6B7280"))
    y -= 12 * mm
    line(X0, y, X1, y)

    # Title block (centered feel via left start but strong hierarchy)
    y -= GAP_SECTION_TOP
    text(X0, y, "보장 점검 안내", name="NotoSansKR-Bold", size=18)
    y -= 7 * mm
    sub = f"{customer['name']} 고객님 · {customer['age_band']} · {customer['gender']}"
    text(X0, y, sub, name="NotoSansKR-Bold", size=11, color=colors.HexColor("#111827"))
    y -= 5 * mm
    small = f"기준연도 {stats.get('base_year','')} | {stats.get('source','')} | {context.get('brand_name','')}"
    text(X0, y, small, size=8.5, color=colors.HexColor("#6B7280"))

    y -= GAP_SECTION_BOTTOM

    # === Section: 요약 ===
    y -= GAP_SECTION_TOP
    text(X0, y, "요약", name="NotoSansKR-Bold", size=12)
    y -= GAP_TITLE_BOTTOM
    y = draw_bullets(X0, y - 2*mm, segment.get("summary_lines", [])[:3], max_width=CW, font_size=9.8, leading=13)
    y -= GAP_SECTION_BOTTOM

    # === Section: 현황 통계 예시 (3 cards) ===
    y -= GAP_SECTION_TOP
    text(X0, y, "현황 통계 예시", name="NotoSansKR-Bold", size=12)
    y -= GAP_TITLE_BOTTOM

    cards = stats.get("cards", [])[:3]
    card_gap = 4 * mm
    card_w = (CW - 2 * card_gap) / 3
    card_h = 30 * mm  # fixed height for consistent aesthetics

    y_cards_top = y - 2*mm
    y_cards_bottom = y_cards_top - card_h

    for i, card in enumerate(cards):
        x = X0 + i * (card_w + card_gap)
        round_box(x, y_cards_bottom, card_w, card_h, r=4*mm, stroke=colors.HexColor("#E5E7EB"), fill=colors.HexColor("#F9FAFB"))
        # title
        text(x + 3*mm, y_cards_top - 8*mm, str(card.get("title","")), name="NotoSansKR-Bold", size=9, color=colors.HexColor("#374151"))
        # value (max 2 lines + ellipsis)
        v = str(card.get("value","")).strip()
        lines2 = clamp_two_lines(v, max_width=card_w - 6*mm, font_size=9.2)
        yy = y_cards_top - 15*mm
        for ln in lines2:
            text(x + 3*mm, yy, ln, size=9.2, color=colors.HexColor("#111827"))
            yy -= 5.2*mm

    y = y_cards_bottom - 6*mm
    text(X0, y, "* 동일 연령·성별 집단의 통계 기반 요약이며, 개인별 상황에 따라 달라질 수 있습니다.", size=8.2, color=colors.HexColor("#6B7280"))
    y -= GAP_SECTION_BOTTOM

    # === Section: 점검 질문 ===
    y -= GAP_SECTION_TOP
    text(X0, y, "점검 질문", name="NotoSansKR-Bold", size=12)
    y -= GAP_TITLE_BOTTOM

    qs = segment.get("gap_questions", [])[:2]
    for idx, q in enumerate(qs, start=1):
        q_lines = wrap_lines(f"{idx}. {q}", CW, "NotoSansKR", 9.8)
        for ln in q_lines:
            text(X0, y - 2*mm, ln, size=9.8)
            y -= 5.5*mm
        y -= 2*mm

    y -= GAP_SECTION_BOTTOM

    # === Section: 필요 보장 구조(개요) ===
    y -= GAP_SECTION_TOP
    text(X0, y, "필요 보장 구조(개요)", name="NotoSansKR-Bold", size=12)
    y -= GAP_TITLE_BOTTOM

    # Table box
    table_h = 28 * mm
    y_table_top = y - 2*mm
    y_table_bottom = y_table_top - table_h

    # outer
    round_box(X0, y_table_bottom, CW, table_h, r=4*mm, stroke=colors.HexColor("#E5E7EB"), fill=colors.white)

    # header shading
    header_h = 8 * mm
    c.setFillColor(colors.HexColor("#F3F4F6"))
    c.setStrokeColor(colors.HexColor("#E5E7EB"))
    c.rect(X0, y_table_top - header_h, CW, header_h, stroke=0, fill=1)

    col1_w = 34 * mm
    # column divider + header divider
    line(X0 + col1_w, y_table_bottom, X0 + col1_w, y_table_top, w=0.6)
    line(X0, y_table_top - header_h, X1, y_table_top - header_h, w=0.6)

    text(X0 + 3*mm, y_table_top - 6*mm, "보장 영역", name="NotoSansKR-Bold", size=9, color=colors.HexColor("#374151"))
    text(X0 + col1_w + 3*mm, y_table_top - 6*mm, "점검이 필요한 이유", name="NotoSansKR-Bold", size=9, color=colors.HexColor("#374151"))

    # rows
    rows = context.get("structure_rows", [])[:3]
    row_y = y_table_top - header_h - 6*mm
    row_gap = 6.5 * mm
    for r in rows:
        text(X0 + 3*mm, row_y, str(r.get("area","")), name="NotoSansKR-Bold", size=9.2, color=colors.HexColor("#111827"))
        # reason single-line clamp for neatness
        reason = str(r.get("reason","")).strip()
        reason_lines = clamp_two_lines(reason, max_width=CW - col1_w - 6*mm, font_size=9.0)
        rr_y = row_y
        for ln in reason_lines[:1]:  # keep it 1 line for table cleanliness
            text(X0 + col1_w + 3*mm, rr_y, ln, size=9.0, color=colors.HexColor("#111827"))
        row_y -= row_gap

    y = y_table_bottom - GAP_SECTION_BOTTOM

    # === CTA box ===
    y -= GAP_SECTION_TOP
    round_box(X0, y - 22*mm, CW, 22*mm, r=5*mm, stroke=colors.HexColor("#E5E7EB"), fill=colors.white)
    text(X0 + 4*mm, y - 7*mm, "상세 보장분석 리포트를 받아보고 싶으신가요?", name="NotoSansKR-Bold", size=11)
    cta = str(segment.get("cta","")).strip()
    # limit to 2 lines for aesthetics
    cta_lines = clamp_two_lines(cta, max_width=CW - 8*mm, font_size=9.2)
    yy = y - 13*mm
    for ln in cta_lines:
        text(X0 + 4*mm, yy, ln, size=9.2, color=colors.HexColor("#111827"))
        yy -= 5.2*mm
    y = y - 26*mm

    # === Planner box ===
    round_box(X0, y - 18*mm, CW, 18*mm, r=5*mm, stroke=colors.HexColor("#E5E7EB"), fill=colors.HexColor("#F9FAFB"))
    text(X0 + 4*mm, y - 6.5*mm, "상담 및 보장 점검 문의", name="NotoSansKR-Bold", size=9.5, color=colors.HexColor("#374151"))
    text(X0 + 4*mm, y - 12.5*mm, f"{planner.get('name','')} 컨설턴트", name="NotoSansKR-Bold", size=10.5)
    text_r(X1 - 4*mm, y - 12.5*mm, f"전화: {planner_phone}", name="NotoSansKR", size=9.8, color=colors.HexColor("#111827"))
    if planner.get("email"):
        text(X0 + 4*mm, y - 16.8*mm, f"이메일: {planner.get('email')}", size=8.7, color=colors.HexColor("#6B7280"))

    # === Footer disclaimer ===
    # keep at bottom margin area
    foot_y = M_B + 18*mm
    c.setFillColor(colors.HexColor("#6B7280"))
    font("NotoSansKR", 7.8)
    # wrap disclaimer in 2~3 lines
    disc_lines = wrap_lines(str(footer.get("disclaimer","")).strip(), CW, "NotoSansKR", 7.8)
    disc_lines = disc_lines[:3]
    yy = foot_y
    for ln in disc_lines:
        c.drawString(X0, yy, ln)
        yy -= 10
    font("NotoSansKR", 7.8)
    c.drawString(X0, M_B + 8, str(footer.get("legal_note","")).strip())

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

