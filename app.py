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

# ===== Path constants =====
ASSETS_DIR = TEMPLATES_DIR / "assets"
FONT_DIR = ASSETS_DIR / "fonts"

# ===== Brand colors (Mirae Asset) =====
MA_ORANGE = colors.HexColor("#F58220")  # PANTONE 158C
MA_BLUE   = colors.HexColor("#043B72")  # PANTONE 295C

GRAY_900 = colors.HexColor("#111827")
GRAY_700 = colors.HexColor("#374151")
GRAY_500 = colors.HexColor("#6B7280")
GRAY_200 = colors.HexColor("#E5E7EB")
GRAY_100 = colors.HexColor("#F3F4F6")
GRAY_50  = colors.HexColor("#F9FAFB")


def register_korean_fonts():
    regular = FONT_DIR / "NotoSansKR-Regular.ttf"
    bold = FONT_DIR / "NotoSansKR-Bold.ttf"

    names = set(pdfmetrics.getRegisteredFontNames())

    if "NotoSansKR" not in names:
        pdfmetrics.registerFont(TTFont("NotoSansKR", str(regular)))

    if bold.exists() and "NotoSansKR-Bold" not in names:
        pdfmetrics.registerFont(TTFont("NotoSansKR-Bold", str(bold)))


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
    org = str(payload.get("org", "")).strip()

    return {
        "name": name,
        "phone": phone_digits,
        "email": payload.get("email", None),
        "org": org,
    }


# ----------------------------
# Content loaders
# ----------------------------
@st.cache_data(show_spinner=False)
def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def segment_key(age_band: str, gender: str) -> str:
    if age_band.startswith("20"):
        a = "20"
    elif age_band.startswith("30"):
        a = "30"
    elif age_band.startswith("40"):
        a = "40"
    else:
        a = "50"
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
# Phone formatting (3-4-4 표시, 마스킹 아님)
# ----------------------------
def format_phone_3_4_4(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11:
        return f"{d[:3]}-{d[3:7]}-{d[7:]}"
    if len(d) == 10:
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return phone


def org_display(company_name: str, org: str) -> str:
    org = (org or "").strip()
    if org:
        return f"{company_name} · {org}"
    return company_name


# ----------------------------
# PDF generation (WeasyPrint if available)
# ----------------------------
def html_to_pdf_bytes(html: str, css_file: Path) -> bytes:
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except Exception:
        raise RuntimeError("WeasyPrint not installed. Install weasyprint to enable HTML→PDF.")

    return HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(
        stylesheets=[CSS(filename=str(css_file))]
    )


def reportlab_snapshot_pdf(context: dict) -> bytes:
    """
    Coverage Snapshot Report (1-page) via ReportLab.
    - 기존 기능 유지
    - 설계사 박스 표기:
      상담 및 보장 점검 문의
      {이름} 컨설턴트
      소속 : 미래에셋금융서비스 · {org}
      연락처 : 010-1234-5678
      이메일 : xxxx@miraeasset.com  (있을 때만)
    """
    register_korean_fonts()

    def pick_font(is_bold: bool) -> str:
        if is_bold and "NotoSansKR-Bold" in pdfmetrics.getRegisteredFontNames():
            return "NotoSansKR-Bold"
        return "NotoSansKR"

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    # ===== Layout (clean 1-page) =====
    M_L = 16 * mm
    M_R = 16 * mm
    M_T = 18 * mm
    M_B = 14 * mm

    X0 = M_L
    X1 = W - M_R
    CW = X1 - X0

    GAP_SECTION_TOP = 12 * mm
    GAP_TITLE_BOTTOM = 4 * mm
    GAP_SECTION_BOTTOM = 8 * mm

    BODY_FS = 9.8
    BODY_LEADING = 14

    # ===== Helpers =====
    def font(size: float, bold: bool = False):
        c.setFont(pick_font(bold), size)

    def text(x, y, s, size=10, bold=False, color=GRAY_900):
        c.setFillColor(color)
        font(size, bold)
        c.drawString(x, y, str(s))

    def text_r(x, y, s, size=10, bold=False, color=GRAY_900):
        c.setFillColor(color)
        font(size, bold)
        c.drawRightString(x, y, str(s))

    def line(xa, ya, xb, yb, w=0.8, color=GRAY_200):
        c.setStrokeColor(color)
        c.setLineWidth(w)
        c.line(xa, ya, xb, yb)

    def round_box(x, y, w, h, r=6*mm, stroke=GRAY_200, fill=colors.white, lw=0.9):
        c.setLineWidth(lw)
        c.setStrokeColor(stroke)
        c.setFillColor(fill)
        c.roundRect(x, y, w, h, r, stroke=1, fill=1)

    def wrap_lines(s: str, max_width: float, font_size: float, bold: bool = False):
        font_name = pick_font(bold)
        c.setFont(font_name, font_size)
        words = str(s).strip().split()
        if not words:
            return [""]

        lines_ = []
        cur = words[0]
        for w in words[1:]:
            cand = cur + " " + w
            if c.stringWidth(cand, font_name, font_size) <= max_width:
                cur = cand
            else:
                lines_.append(cur)
                cur = w
        lines_.append(cur)
        return lines_

    def clamp_two_lines(s: str, max_width: float, font_size: float):
        lines_ = wrap_lines(s, max_width, font_size, bold=False)
        if len(lines_) <= 2:
            return lines_
        l1, l2 = lines_[0], lines_[1]
        ell = "…"
        font_name = pick_font(False)
        c.setFont(font_name, font_size)
        while c.stringWidth(l2 + ell, font_name, font_size) > max_width and len(l2) > 1:
            l2 = l2[:-1]
        return [l1, l2 + ell]

    def draw_bullets(x: float, y: float, items: List[str], max_width: float):
        for it in items:
            lines_ = wrap_lines(it, max_width - 8*mm, BODY_FS, bold=False)
            text(x, y, "•", size=BODY_FS, color=GRAY_900)
            text(x + 4*mm, y, lines_[0], size=BODY_FS, color=GRAY_900)
            y -= BODY_LEADING
            for ln in lines_[1:]:
                text(x + 4*mm, y, ln, size=BODY_FS, color=GRAY_900)
                y -= BODY_LEADING
            y -= 2
        return y

    # ===== Context =====
    customer = context["customer"]
    segment = context["segment"]
    stats = context["stats"]
    planner = context["planner"]
    footer = context["footer"]

    planner_name = planner.get("name", "")
    planner_phone = format_phone_3_4_4(planner.get("phone", ""))
    planner_email = (planner.get("email") or "").strip()

    # ✅ 여기서 "표시용 소속 문자열"만 만들고, 변수명은 org_display로 쓰지 않는다
    planner_org_text = org_display(BRAND_NAME, planner.get("org", ""))

    # ===== Start drawing =====
    y = H - M_T

    # Header
    text(X0, y, context.get("brand_name", BRAND_NAME), size=11, bold=True, color=MA_BLUE)
    text(X0, y - 5*mm, context.get("brand_subtitle", BRAND_SUBTITLE), size=9, color=GRAY_500)
    text_r(X1, y, f"v{context.get('version', APP_VERSION)}", size=9, color=GRAY_500)
    y -= 12 * mm
    line(X0, y, X1, y, w=0.9, color=MA_BLUE)

    # Title
    y -= GAP_SECTION_TOP
    text(X0, y, "보장 점검 안내", size=18, bold=True, color=MA_BLUE)
    y -= 7 * mm
    text(X0, y, f"{customer['name']} 고객님 · {customer['age_band']} · {customer['gender']}",
         size=11, bold=True, color=GRAY_900)
    y -= 5 * mm
    text(X0, y, f"기준연도 {stats.get('base_year','')} | {stats.get('source','')}",
         size=8.6, color=GRAY_500)
    y -= GAP_SECTION_BOTTOM

    # Section: 요약
    y -= GAP_SECTION_TOP
    text(X0, y, "요약", size=12, bold=True, color=MA_BLUE)
    y -= GAP_TITLE_BOTTOM
    y = draw_bullets(X0, y - 2*mm, segment.get("summary_lines", [])[:3], max_width=CW)
    y -= GAP_SECTION_BOTTOM

    # Section: 통계 카드
    y -= GAP_SECTION_TOP
    text(X0, y, "현황 통계 예시", size=12, bold=True, color=MA_BLUE)
    y -= GAP_TITLE_BOTTOM

    cards = stats.get("cards", [])[:3]
    card_gap = 4 * mm
    card_w = (CW - 2 * card_gap) / 3
    card_h = 32 * mm

    y_cards_top = y - 2*mm
    y_cards_bottom = y_cards_top - card_h

    for i, card in enumerate(cards):
        x = X0 + i * (card_w + card_gap)
        round_box(x, y_cards_bottom, card_w, card_h, r=6*mm, stroke=GRAY_200, fill=GRAY_50, lw=0.9)

        text(x + 4*mm, y_cards_top - 10*mm, str(card.get("title","")),
             size=9.2, bold=True, color=MA_BLUE)

        v = str(card.get("value","")).strip()
        lines2 = clamp_two_lines(v, max_width=card_w - 8*mm, font_size=9.2)
        yy = y_cards_top - 17*mm
        for ln in lines2:
            text(x + 4*mm, yy, ln, size=9.2, color=GRAY_900)
            yy -= 5.5*mm

    y = y_cards_bottom - 6*mm
    text(X0, y, "* 동일 연령·성별 집단의 통계 기반 요약이며, 개인별 상황에 따라 달라질 수 있습니다.",
         size=8.2, color=GRAY_500)
    y -= GAP_SECTION_BOTTOM

    # Section: 점검 질문
    y -= GAP_SECTION_TOP
    text(X0, y, "점검 질문", size=12, bold=True, color=MA_BLUE)
    y -= GAP_TITLE_BOTTOM

    qs = segment.get("gap_questions", [])[:2]
    for idx, q in enumerate(qs, start=1):
        for ln in wrap_lines(f"{idx}. {q}", CW, BODY_FS, bold=False):
            text(X0, y, ln, size=BODY_FS, color=GRAY_900)
            y -= BODY_LEADING
        y -= 2

    y -= GAP_SECTION_BOTTOM

    # Section: 필요 보장 구조(개요)
    y -= GAP_SECTION_TOP
    text(X0, y, "필요 보장 구조(개요)", size=12, bold=True, color=MA_BLUE)
    y -= GAP_TITLE_BOTTOM

    table_h = 30 * mm
    y_table_top = y - 2*mm
    y_table_bottom = y_table_top - table_h
    round_box(X0, y_table_bottom, CW, table_h, r=6*mm, stroke=GRAY_200, fill=colors.white, lw=0.9)

    header_h = 9 * mm
    c.setFillColor(GRAY_100)
    c.rect(X0, y_table_top - header_h, CW, header_h, stroke=0, fill=1)

    col1_w = 36 * mm
    line(X0 + col1_w, y_table_bottom, X0 + col1_w, y_table_top, w=0.7, color=GRAY_200)
    line(X0, y_table_top - header_h, X1, y_table_top - header_h, w=0.7, color=GRAY_200)

    text(X0 + 4*mm, y_table_top - 6.5*mm, "보장 영역", size=9.2, bold=True, color=GRAY_700)
    text(X0 + col1_w + 4*mm, y_table_top - 6.5*mm, "점검이 필요한 이유", size=9.2, bold=True, color=GRAY_700)

    rows = context.get("structure_rows", [])[:3]
    row_y = y_table_top - header_h - 7*mm
    row_gap = 7.5 * mm

    for r in rows:
        text(X0 + 4*mm, row_y, str(r.get("area","")), size=9.4, bold=True, color=GRAY_900)
        reason = str(r.get("reason","")).strip()
        reason_1 = clamp_two_lines(reason, max_width=CW - col1_w - 8*mm, font_size=9.1)[0]
        text(X0 + col1_w + 4*mm, row_y, reason_1, size=9.1, color=GRAY_900)
        row_y -= row_gap

    y = y_table_bottom - GAP_SECTION_BOTTOM

    # CTA (Orange only here)
    y -= GAP_SECTION_TOP
    cta_h = 24 * mm
    round_box(X0, y - cta_h, CW, cta_h, r=7*mm, stroke=MA_ORANGE, fill=colors.white, lw=1.3)

    text(X0 + 5*mm, y - 8*mm, "상세 보장분석 리포트를 받아보고 싶으신가요?",
         size=11, bold=True, color=MA_ORANGE)

    cta = str(segment.get("cta","")).strip()
    cta_lines = clamp_two_lines(cta, max_width=CW - 10*mm, font_size=9.4)
    yy = y - 14*mm
    for ln in cta_lines:
        text(X0 + 5*mm, yy, ln, size=9.4, color=GRAY_900)
        yy -= 5.5*mm

    y = y - (cta_h + 6*mm)

    # Planner box (요구 포맷 반영: 컨설턴트/소속/연락처/이메일)
    planner_h = 26 * mm
    round_box(X0, y - planner_h, CW, planner_h, r=7*mm, stroke=GRAY_200, fill=GRAY_50, lw=0.9)

    text(X0 + 5*mm, y - 7*mm, "상담 및 보장 점검 문의", size=9.4, bold=True, color=GRAY_700)

    # 이름: "{이름} 컨설턴트"
    text(X0 + 5*mm, y - 13*mm, f"{planner_name} 컨설턴트", size=10.5, bold=True, color=MA_BLUE)

    # 소속: "소속 : 미래에셋금융서비스 · {org}"
    text(X0 + 5*mm, y - 18*mm, f"소속 : {planner_org_text}", size=9.2, color=GRAY_700)

    # 연락처: 오른쪽 정렬 + 3-4-4
    text_r(X1 - 5*mm, y - 18*mm, f"연락처 : {planner_phone}", size=9.2, color=GRAY_700)

    # 이메일: 있으면 표시
    if planner_email:
        text(X0 + 5*mm, y - 22.8*mm, f"이메일 : {planner_email}", size=8.6, color=GRAY_500)

    # Footer disclaimer
    foot_y = M_B + 18*mm
    disc = str(footer.get("disclaimer","")).strip()
    legal = str(footer.get("legal_note","")).strip()

    c.setFillColor(GRAY_500)
    c.setFont(pick_font(False), 7.8)
    disc_lines = wrap_lines(disc, CW, 7.8, bold=False)[:3]
    yy = foot_y
    for ln in disc_lines:
        c.drawString(X0, yy, ln)
        yy -= 10

    c.drawString(X0, M_B + 8, legal)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="보장 점검 유인 팜플렛", layout="centered")

token = st.query_params.get("token")
if not token:
    st.error("유효한 접속 정보가 없습니다. M.POST 게이트웨이 링크로 접속해 주세요.")
    st.stop()

try:
    planner = verify_token(token)
except Exception as e:
    st.error(f"접속 검증 실패: {e}")
    st.stop()

segments_db = load_json(SEGMENTS_PATH)
stats_db = load_json(STATS_PATH)

planner_org_display = (
    f"{BRAND_NAME} · {planner.get('org').strip()}"
    if planner.get("org")
    else BRAND_NAME
)

st.success("미래에셋금융서비스 FC 인증 완료")
st.write(f"FC명 : **{planner['name']}**")
st.write(f"소속 : **{planner_org_display}**")
st.write(f"연락처 : **{format_phone_3_4_4(planner['phone'])}**")
st.divider()

customer_name = st.text_input("고객 성명", value="")
gender = st.selectbox("성별", ["남성", "여성"])
age_band = st.selectbox("연령대", ["20대", "30대", "40대", "50대", "60대 이상"])

key = segment_key(age_band, gender)

segment = segments_db["segments"].get(key)
cards = stats_db["cards"].get(key)

if not segment or not cards:
    st.error(f"콘텐츠 세트가 없습니다: {key}")
    st.stop()

st.subheader("문구 조정(선택/제한)")
summary_lines = segment["summary_lines"][:]
gap_questions = segment["gap_questions"][:]
cta_text = segment["cta"]

summary_lines[0] = st.text_input("요약 1", value=summary_lines[0])
summary_lines[1] = st.text_input("요약 2", value=summary_lines[1])
summary_lines[2] = st.text_input("요약 3", value=summary_lines[2])

gap_questions[0] = st.text_input("점검 질문 1", value=gap_questions[0])
gap_questions[1] = st.text_input("점검 질문 2", value=gap_questions[1])

cta_text = st.text_area("CTA 문구", value=cta_text, height=90)

structure_rows = [
    {"area": "진단비", "reason": "진단 직후 초기 자금 여력(목돈) 점검"},
    {"area": "치료비", "reason": "치료 과정의 반복 비용·통원/수술 부담 점검"},
    {"area": "생활·소득", "reason": "치료로 인한 소득 공백·가계 영향 점검"},
]

context = {
    "css_path": str(CSS_PATH),
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
        "name": f"{planner['name']} FC",
        "org_display": planner_org_display,              # ← 회사 · 조직
        "phone_display": format_phone_3_4_4(planner["phone"]),
        "email": planner.get("email", None),
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
    }),
    "brand_colors": {
        "blue": "#043B72",
        "orange": "#F58220",
    }
}

html = render_html(context)

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

    html_final = render_html(context)

    try:
        pdf_bytes = html_to_pdf_bytes(html_final, CSS_PATH)
        filename = f"보장점검안내_{customer_name.strip()}_{age_band}_{gender}.pdf"

        st.success("PDF가 생성되었습니다. 아래 버튼으로 다운로드하세요.")
        st.download_button(
            "PDF 다운로드",
            data=pdf_bytes,
            file_name=filename,
            mime="application/pdf"
        )

    except Exception as e:
        st.error(f"PDF 생성(WeasyPrint) 중 오류가 발생했습니다.\n\n오류: {e}")


