import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, Optional

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape

from io import BytesIO

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
PW_DIR = BASE_DIR / ".pw-browsers"

# 빌드/런타임 사용자 달라도 동일 위치를 보게 고정
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PW_DIR)

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

ASSETS_DIR = TEMPLATES_DIR / "assets"
FONT_DIR = ASSETS_DIR / "fonts"

# 예: templates/assets/ma_logo.png
LOGO_PATH = ASSETS_DIR / "ma_logo.png"

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
# Utilities
# ----------------------------
def format_phone_3_4_4(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 11:
        return f"{d[:3]}-{d[3:7]}-{d[7:]}"
    if len(d) == 10:
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return phone

def org_display(company: str, org: str) -> str:
    org = (org or "").strip()
    return f"{company} · {org}" if org else company

def file_to_data_uri(path: Path, mime: str) -> Optional[str]:
    if not path.exists():
        return None
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{data}"

def font_file_to_data_uri(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:font/ttf;base64,{data}"


def build_embedded_font_face_css() -> str:
    """
    폰트를 '치환'하지 않고, 항상 최상단에 강제 선언해서
    미리보기/WeasyPrint의 폰트 일치도를 극대화한다.
    """
    regular_ttf = FONT_DIR / "NotoSansKR-Regular.ttf"
    bold_ttf = FONT_DIR / "NotoSansKR-Bold.ttf"

    if not regular_ttf.exists() or not bold_ttf.exists():
        raise RuntimeError(
            "폰트 파일이 없습니다. templates/assets/fonts/에 "
            "NotoSansKR-Regular.ttf, NotoSansKR-Bold.ttf를 넣어주세요."
        )

    reg_uri = font_file_to_data_uri(regular_ttf)
    bold_uri = font_file_to_data_uri(bold_ttf)

    return f"""
/* ===== Embedded Fonts (Data URI) ===== */
@font-face {{
  font-family: "NotoSansKR";
  src: url("{reg_uri}") format("truetype");
  font-weight: 400;
  font-style: normal;
}}
@font-face {{
  font-family: "NotoSansKR";
  src: url("{bold_uri}") format("truetype");
  font-weight: 700;
  font-style: normal;
}}
"""


def build_css_for_both(css_path: Path) -> str:
    """
    - style.css 원문 + (1) 임베딩 폰트 강제 + (2) bullet/number 커스텀 고정
    """
    base_css = css_path.read_text(encoding="utf-8")

    # (A) 폰트 선언을 맨 위로 강제
    font_css = build_embedded_font_face_css()

    # (B) WeasyPrint에서 list marker 누락을 피하려고 커스텀 마커로 확정
    #     (원 CSS에서 bullets/questions를 list-style로 쓰더라도, 마지막 override로 고정됨)
    bullet_fix_css = """
/* ===== List marker stabilization (Browser + WeasyPrint) ===== */
/* bullets: 점(•)을 직접 찍어서 PDF에서 안 보이는 문제 회피 */
.bullets{
  list-style:none !important;
  margin:0 !important;
  padding-left:0 !important;
}
.bullets li{
  position:relative;
  padding-left:16px;
  margin:5px 0;
}
.bullets li::before{
  content:"•";
  position:absolute;
  left:0;
  top:0;
}

/* questions: counter로 번호를 직접 찍어서 렌더 불일치 최소화 */
.questions{
  list-style:none !important;
  margin:0 !important;
  padding-left:0 !important;
  counter-reset:q;
}
.questions li{
  position:relative;
  padding-left:18px;
  margin:6px 0;
}
.questions li::before{
  counter-increment:q;
  content: counter(q) ".";
  position:absolute;
  left:0;
  top:0;
  font-weight:700;
}

/* preview-viewport이 비정상 구조일 때 빈 여백 생기는 것 방지용 기본값 */
.preview-viewport{
  display:block;
}
"""

    return f"{font_css}\n{base_css}\n{bullet_fix_css}"


def render_html(context: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"])
    )
    template = env.get_template(HTML_TEMPLATE)
    return template.render(**context)


def fix_preview_wrapper_structure(html: str) -> str:
    """
    템플릿이 아래처럼 잘못된 경우를 자동으로 보정:
      <div class="preview-viewport"></div>
        <div class="page">...

    -> 아래처럼 바꿔서 스케일 스크립트가 정상 동작하게 함:
      <div class="preview-viewport">
        <div class="page">...
    """
    # 케이스1) 빈 preview-viewport를 닫아버린 경우
    html = re.sub(
        r'<div class="preview-viewport">\s*</div>\s*<div class="page">',
        r'<div class="preview-viewport">\n<div class="page">',
        html,
        flags=re.IGNORECASE
    )

    # 케이스2) preview-viewport가 아예 없고 page만 있는 경우: page를 감싸준다(가능한 범위에서)
    if 'class="preview-viewport"' not in html and 'class="page"' in html:
        html = html.replace('<div class="page">', '<div class="preview-viewport">\n<div class="page">', 1)
        # body 닫기 직전에 viewport 닫기 추가(간단 보정)
        html = html.replace("</body>", "\n</div>\n</body>", 1)

    return html


def inject_inline_css(html: str, css_text: str, css_path_in_template: str) -> str:
    """
    템플릿의 <link rel="stylesheet" ...>를 <style>로 치환.
    """
    needle = f'<link rel="stylesheet" href="{css_path_in_template}" />'
    if needle in html:
        return html.replace(needle, f"<style>\n{css_text}\n</style>")
    # 혹시 템플릿에서 href가 다르면, link 태그를 더 넓게 잡아서 제거/주입
    html = re.sub(
        r'<link\s+rel=["\']stylesheet["\']\s+href=["\'][^"\']+["\']\s*/?>',
        f"<style>\n{css_text}\n</style>",
        html,
        count=1,
        flags=re.IGNORECASE
    )
    return html


def build_final_html_for_both(context: Dict[str, Any]) -> str:
    """
    미리보기/WeasyPrint 모두 같은 HTML을 쓰고,
    CSS도 동일(인라인)하게 주입하여 결과를 최대한 일치시킨다.
    """
    html = render_html(context)
    html = fix_preview_wrapper_structure(html)

    css_text = build_css_for_both(CSS_PATH)
    html = inject_inline_css(html, css_text, str(context["css_path"]))

    return html


# ----------------------------
# PDF generation (WeasyPrint)
# ----------------------------
def weasyprint_pdf_bytes(html: str) -> bytes:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        raise RuntimeError("WeasyPrint not installed. requirements.txt에 weasyprint를 추가하고 배포 환경에서 설치해 주세요.")

    # JS는 WeasyPrint에서 무시되므로(정상), preview용 scale 스크립트가 PDF를 망치지 않는다.
    return HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf()


def chromium_pdf_bytes(html: str) -> bytes:
    # requirements: playwright
    # 그리고 배포 환경에서: playwright install chromium
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # HTML 주입
        page.set_content(html, wait_until="load")

        # A4 PDF 생성 (배경 포함)
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"},
        )

        browser.close()
        return pdf_bytes


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

planner_org_display = org_display(BRAND_NAME, planner.get("org", ""))
planner_phone_display = format_phone_3_4_4(planner["phone"])

st.success("설계사 인증 완료")
st.write(f"FC명 : **{planner['name']}**")
st.write(f"소속 : **{planner_org_display}**")
st.write(f"연락처 : **{planner_phone_display}**")
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

logo_data_uri = file_to_data_uri(LOGO_PATH, "image/png")  # ma_logo.png 기준

context = {
    "css_path": str(CSS_PATH),
    "logo_data_uri": logo_data_uri,
    "brand_name": BRAND_NAME,
    "brand_subtitle": BRAND_SUBTITLE,
    "version": APP_VERSION,
    "customer": {
        "name": customer_name.strip() or "고객",
        "gender": gender,
        "age_band": age_band
    },
    "planner": {
        # 요구사항 반영: "박동혁 FC" 형태
        "name": f"{planner['name']} FC",
        "phone": planner["phone"],
        "email": planner.get("email", None),
        "org": planner.get("org", "").strip(),
        "company": BRAND_NAME,
        "phone_display": planner_phone_display,
        "org_display": planner_org_display,
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

final_html = build_final_html_for_both(context)

st.subheader("미리보기")
# 스케일 스크립트가 viewport 높이를 잡아주므로, scrolling=False가 더 깔끔한 경우가 많음
components.html(
    final_html,
    height=900,          # 처음엔 작게
    scrolling=True,
)

st.divider()
st.subheader("확정 및 PDF 출력")

if st.button("확정 후 PDF 생성"):
    if not customer_name.strip():
        st.warning("고객 성명을 입력해 주세요.")
        st.stop()

    # 고객명 확정 반영
    context["customer"]["name"] = customer_name.strip()
    context["segment"]["headline"] = segment["headline"].replace("{customer_name}", customer_name.strip())
    final_html = build_final_html_for_both(context)

    try:
        pdf_bytes = chromium_pdf_bytes(final_html)
        filename = f"보장점검안내_{customer_name.strip()}_{age_band}_{gender}.pdf"

        # 새 탭 열지 않고 바로 다운로드만 제공 (Chrome 차단 회피)
        st.download_button("PDF 다운로드", data=pdf_bytes, file_name=filename, mime="application/pdf")
        
    except Exception as e:
        st.error(f"PDF 생성(WeasyPrint) 중 오류가 발생했습니다.\n\n오류: {e}")
