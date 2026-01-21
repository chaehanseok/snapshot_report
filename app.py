import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, Optional

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape

from io import BytesIO

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

# 로고 파일을 여기에 두세요 (질문에서 준 로고 이미지 저장)
# 예: templates/assets/ma_logo.png
LOGO_PATH = ASSETS_DIR / "ma_logo.png"

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
    org = str(payload.get("org", "")).strip()  # 토큰에 org 넣으면 표시

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
    # ttf는 WeasyPrint/브라우저 모두 data uri로 안정적으로 처리됨
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:font/ttf;base64,{data}"

def build_css_with_embedded_fonts(css_path: Path) -> str:
    """
    미리보기와 PDF를 '완전 동일'하게 맞추기 위해:
    - CSS를 inline로 쓰되
    - 폰트 url(...)를 data:URI로 치환
    """
    css_text = css_path.read_text(encoding="utf-8")

    regular_ttf = FONT_DIR / "NotoSansKR-Regular.ttf"
    bold_ttf = FONT_DIR / "NotoSansKR-Bold.ttf"

    if not regular_ttf.exists() or not bold_ttf.exists():
        raise RuntimeError("폰트 파일이 없습니다. templates/assets/fonts/에 NotoSansKR-Regular.ttf, NotoSansKR-Bold.ttf를 넣어주세요.")

    reg_uri = font_file_to_data_uri(regular_ttf)
    bold_uri = font_file_to_data_uri(bold_ttf)

    # CSS에 있는 폰트 경로를 data URI로 강제
    css_text = css_text.replace('url("assets/fonts/NotoSansKR-Regular.ttf")', f'url("{reg_uri}")')
    css_text = css_text.replace("url('assets/fonts/NotoSansKR-Regular.ttf')", f'url("{reg_uri}")')
    css_text = css_text.replace('url("assets/fonts/NotoSansKR-Bold.ttf")', f'url("{bold_uri}")')
    css_text = css_text.replace("url('assets/fonts/NotoSansKR-Bold.ttf')", f'url("{bold_uri}")')

    return css_text

def render_html(context: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"])
    )
    template = env.get_template(HTML_TEMPLATE)
    return template.render(**context)

def build_final_html_for_both(context: Dict[str, Any]) -> str:
    """
    미리보기/WeasyPrint 모두 동일한 HTML을 사용.
    - CSS를 <style>로 주입
    - 폰트는 data-uri로 주입되어 렌더 차이 최소화
    """
    html = render_html(context)

    css_text = build_css_with_embedded_fonts(CSS_PATH)

    # 템플릿의 <link ...>를 inline <style>로 치환
    html = html.replace(
        f'<link rel="stylesheet" href="{context["css_path"]}" />',
        f"<style>\n{css_text}\n</style>"
    )
    return html

# ----------------------------
# PDF generation (WeasyPrint)
# ----------------------------
def weasyprint_pdf_bytes(html: str) -> bytes:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        raise RuntimeError("WeasyPrint not installed. requirements.txt에 weasyprint를 추가하고 배포 환경에서 설치해 주세요.")

    # base_url은 상대경로용이지만, 지금은 폰트/로고를 data-uri로 쓰므로 거의 영향 없음
    return HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf()

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
        # 리포트 표시 요구사항 반영
        "name": f"{planner['name']} FC",  # 박동혁 FC
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
# height는 충분히 크게
components.html(final_html, height=1100, scrolling=True)

st.divider()
st.subheader("확정 및 PDF 출력")

# “Chrome 차단 페이지” 뜨는 건 새 탭/새 창으로 pdf를 열려고 할 때 생기는 케이스가 많습니다.
# 여기서는 '열기' 없이 곧바로 download_button만 노출합니다.
if st.button("확정 후 PDF 생성"):
    if not customer_name.strip():
        st.warning("고객 성명을 입력해 주세요.")
        st.stop()

    # 고객명 확정 반영
    context["customer"]["name"] = customer_name.strip()
    context["segment"]["headline"] = segment["headline"].replace("{customer_name}", customer_name.strip())
    final_html = build_final_html_for_both(context)

    try:
        pdf_bytes = weasyprint_pdf_bytes(final_html)
        filename = f"보장점검안내_{customer_name.strip()}_{age_band}_{gender}.pdf"
        st.download_button(
            "PDF 다운로드",
            data=pdf_bytes,
            file_name=filename,
            mime="application/pdf"
        )
    except Exception as e:
        st.error(f"PDF 생성(WeasyPrint) 중 오류가 발생했습니다.\n\n오류: {e}")
