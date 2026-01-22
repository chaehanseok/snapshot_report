import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, Optional

import os
import sys
import subprocess
import requests

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape


# =========================================================
# Playwright runtime config (Streamlit Cloud-safe)
# =========================================================
PW_DIR = Path("/tmp/pw-browsers")  # Streamlit Cloud에서 가장 안전(쓰기 가능)
PW_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PW_DIR)


@st.cache_resource(show_spinner=False)
def ensure_playwright_chromium() -> bool:
    """
    Streamlit Cloud에서는 playwright 패키지 설치만으로는 브라우저 바이너리가 없음.
    최초 1회만 chromium 다운로드 후 캐시됨.
    """
    browsers_path = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    has_chrome = any(browsers_path.glob("**/chrome-headless-shell")) or any(browsers_path.glob("**/chromium*"))
    if not has_chrome:
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    return True


# =========================================================
# Config
# =========================================================
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
LOGO_PATH = ASSETS_DIR / "ma_logo.png"

SECRET = st.secrets.get("GATEWAY_SECRET", "")


# =========================================================
# Token helpers
# =========================================================
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


# =========================================================
# Content loaders
# =========================================================
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
    elif age_band.startswith("50"):
        a = "50"
    else:
        a = "60"
    g = "M" if gender == "남성" else "F"
    return f"{a}_{g}"


# =========================================================
# D1 query (Cloudflare D1 REST API)
# =========================================================
def d1_query(sql: str, params: list) -> list[dict]:
    """
    secrets 필요:
      CF_ACCOUNT_ID
      CF_API_TOKEN
      D1_DATABASE_ID
    """
    account_id = st.secrets["CF_ACCOUNT_ID"]
    api_token = st.secrets["CF_API_TOKEN"]
    db_id = st.secrets["D1_DATABASE_ID"]

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}/query"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    payload = {"sql": sql, "params": params}

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    if not data.get("success"):
        raise RuntimeError(f"D1 query failed: {data}")

    blocks = data.get("result", [])
    if not blocks:
        return []
    return blocks[0].get("results", [])

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_year_range() -> tuple[int, int]:
    row = d1_query("SELECT MIN(year) AS min_year, MAX(year) AS max_year FROM disease_year_age_sex_metrics;", [])
    if not row:
        return (2010, 2024)  # fallback
    return (int(row[0].get("min_year") or 2010), int(row[0].get("max_year") or 2024))


# =========================================================
# Stats helpers (Top N)
# =========================================================
def format_krw_compact(n: float | int) -> str:
    n = float(n or 0)
    if n >= 1e8:
        return f"{n/1e8:.1f}억"
    if n >= 1e4:
        return f"{n/1e4:.0f}만"
    return f"{n:.0f}"


STAT_SORT_OPTIONS = {
    "총 진료비(연간)": {"key": "total_cost", "label": "연간 진료비"},
    "환자수(연간)": {"key": "patient_cnt", "label": "환자수"},
    "1인당 진료비": {"key": "cost_per_patient", "label": "1인당"},
}

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_top_cards(
    start_year: int,
    end_year: int,
    age_group: str,
    sex: str,
    sort_key: str = "total_cost",
    limit: int = 7,
) -> list[dict]:
    """
    기간(start_year~end_year) + 연령/성별에서 sort_key 기준 상위 N개 질병 카드 생성
    disease 테이블 조인해서 disease_name_ko 표시
    sort_key: total_cost | patient_cnt | cost_per_patient
    """
    if sort_key not in ("total_cost", "patient_cnt", "cost_per_patient"):
        sort_key = "total_cost"

    order_by = {
        "total_cost": "total_cost DESC",
        "patient_cnt": "patient_cnt DESC",
        "cost_per_patient": "cost_per_patient DESC",
    }[sort_key]

    sql = f"""
    WITH agg AS (
      SELECT
        m.disease_code AS disease_code,
        COALESCE(NULLIF(TRIM(d.disease_name_ko), ''), m.disease_code) AS disease_name_ko,
        SUM(m.patient_cnt) AS patient_cnt,
        SUM(m.total_cost)  AS total_cost,
        CAST(SUM(m.total_cost) AS REAL) / NULLIF(SUM(m.patient_cnt), 0) AS cost_per_patient
      FROM disease_year_age_sex_metrics m
      LEFT JOIN disease d
        ON m.disease_code = d.disease_code
      WHERE m.year BETWEEN ? AND ?
        AND m.age_group = ?
        AND m.sex = ?
      GROUP BY m.disease_code, COALESCE(NULLIF(TRIM(d.disease_name_ko), ''), m.disease_code)
    )
    SELECT * FROM agg
    ORDER BY {order_by}
    LIMIT ?;
    """

    rows = d1_query(sql, [int(start_year), int(end_year), age_group, sex, int(limit)])

    cards: list[dict] = []
    for r in rows:
        name = (r.get("disease_name_ko") or r.get("disease_code") or "").strip() or "질병"
        patient_cnt = int(r.get("patient_cnt") or 0)
        total_cost = float(r.get("total_cost") or 0)
        cpp = float(r.get("cost_per_patient") or 0)

        if sort_key == "total_cost":
            lead = f"연간 진료비 {format_krw_compact(total_cost)}"
        elif sort_key == "patient_cnt":
            lead = f"환자 {patient_cnt:,}명"
        else:
            lead = f"1인당 {format_krw_compact(cpp)}"

        cards.append(
            {
                "title": name,
                "value": f"{lead} · 진료비 {format_krw_compact(total_cost)} · 환자 {patient_cnt:,}명 · 1인당 {format_krw_compact(cpp)}",
            }
        )
    return cards


# =========================================================
# Utilities (rendering)
# =========================================================
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
    base_css = css_path.read_text(encoding="utf-8")
    font_css = build_embedded_font_face_css()

    bullet_fix_css = """
/* bullets: 점(•) 직접 렌더 */
.bullets{ list-style:none !important; margin:0 !important; padding-left:0 !important; }
.bullets li{ position:relative; padding-left:16px; margin:5px 0; }
.bullets li::before{ content:"•"; position:absolute; left:0; top:0; }

/* questions: counter로 번호 직접 렌더 */
.questions{ list-style:none !important; margin:0 !important; padding-left:0 !important; counter-reset:q; }
.questions li{ position:relative; padding-left:18px; margin:6px 0; }
.questions li::before{
  counter-increment:q;
  content: counter(q) ".";
  position:absolute; left:0; top:0; font-weight:700;
}
"""
    return f"{font_css}\n{base_css}\n{bullet_fix_css}"


def render_html(context: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template(HTML_TEMPLATE)
    return template.render(**context)


def inject_inline_css(html: str, css_text: str, css_path_in_template: str) -> str:
    needle = f'<link rel="stylesheet" href="{css_path_in_template}" />'
    if needle in html:
        return html.replace(needle, f"<style>\n{css_text}\n</style>")

    # fallback
    return re.sub(
        r'<link\s+rel=["\']stylesheet["\']\s+href=["\'][^"\']+["\']\s*/?>',
        f"<style>\n{css_text}\n</style>",
        html,
        count=1,
        flags=re.IGNORECASE,
    )


def build_final_html_for_both(context: Dict[str, Any]) -> str:
    html = render_html(context)
    css_text = build_css_for_both(CSS_PATH)
    html = inject_inline_css(html, css_text, str(context["css_path"]))
    return html


# =========================================================
# PDF generation (Chromium via Playwright)
# =========================================================
def chromium_pdf_bytes(html: str) -> bytes:
    from playwright.sync_api import sync_playwright

    ensure_playwright_chromium()

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1200, "height": 800})

        page.set_content(html, wait_until="load")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(150)  # 폰트/레이아웃 settle

        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"},
        )
        browser.close()
        return pdf_bytes


# =========================================================
# Streamlit UI
# =========================================================
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
if not segment:
    st.error(f"콘텐츠 세트가 없습니다: {key}")
    st.stop()

# ---------------------------------------------------------
# 통계 표시 옵션 (Top7 기준 + 기간)
# ---------------------------------------------------------
st.subheader("통계 표시 옵션")

min_year, max_year = fetch_year_range()

STAT_SORT_OPTIONS = {
    "총 진료비(기간합)": {"key": "total_cost"},
    "환자수(기간합)": {"key": "patient_cnt"},
    "1인당 진료비(기간평균)": {"key": "cost_per_patient"},
}

sort_label = st.radio(
    "Top7 기준",
    options=list(STAT_SORT_OPTIONS.keys()),
    index=0,
    horizontal=True,
)
sort_key = STAT_SORT_OPTIONS[sort_label]["key"]

# 기본: 최신년도 1년
default_end = max_year
default_start = max_year

colA, colB = st.columns(2)
with colA:
    start_year = st.number_input(
        "시작년도",
        min_value=int(min_year),
        max_value=int(max_year),
        value=int(default_start),
        step=1,
    )
with colB:
    end_year = st.number_input(
        "종료년도",
        min_value=int(min_year),
        max_value=int(max_year),
        value=int(default_end),
        step=1,
    )

# start > end 방지 (자동 보정)
if start_year > end_year:
    start_year, end_year = end_year, start_year
    st.info(f"시작/종료년도를 자동 보정했습니다: {start_year} ~ {end_year}")

# ---------------------------------------------------------
# D1 기반 통계 미리보기 (리포트 미리보기 전에 먼저 노출)
# ---------------------------------------------------------
AGE_GROUP_MAP = {
    "20대": "20_29",
    "30대": "30_39",
    "40대": "40_49",
    "50대": "50_59",
    "60대 이상": "60_69",
}
age_group = AGE_GROUP_MAP.get(age_band, "50_59")
sex = "M" if gender == "남성" else "F"

try:
    cards = fetch_top_cards(
        start_year=int(start_year),
        end_year=int(end_year),
        age_group=age_group,
        sex=sex,
        sort_key=sort_key,
        limit=7,
    )
except Exception as e:
    st.error(f"D1 통계 조회 실패: {e}")
    cards = []

st.caption(f"통계 범위: {start_year}~{end_year} · 연령: {age_band}({age_group}) · 성별: {sex} · 기준: {sort_label}")

# 통계 “먼저” 보여주기 (표 형태)
if cards:
    st.markdown("#### 통계 미리보기 (Top7)")
    st.table([{"질병": c["title"], "요약": c["value"]} for c in cards])
else:
    st.warning("통계 데이터가 없습니다. (조건을 바꿔보세요)")


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

logo_data_uri = file_to_data_uri(LOGO_PATH, "image/png")

context = {
    "css_path": str(CSS_PATH),
    "logo_data_uri": logo_data_uri,
    "brand_name": BRAND_NAME,
    "brand_subtitle": BRAND_SUBTITLE,
    "version": APP_VERSION,
    "customer": {"name": customer_name.strip() or "고객", "gender": gender, "age_band": age_band},
    "planner": {
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
        "cta": cta_text,
    },
    "stats": {
        "base_year": f"{start_year}~{end_year}",
        "source": stats_db.get("source", "공식 보건의료 통계(요약)"),
        "cards": cards,
        "top7_basis": sort_label,
    },
    "structure_rows": structure_rows,
    "footer": stats_db.get(
        "footer",
        {
            "disclaimer": "본 자료는 동일 연령·성별 집단의 통계 기반 참고 자료이며, 개인별 진단·보장 수준은 상이할 수 있습니다. 정확한 확인은 종합 보장분석을 통해 가능합니다.",
            "legal_note": "본 자료는 편의를 위해 제공되며 법적 효력을 갖지 않습니다.",
        },
    ),
}

final_html = build_final_html_for_both(context)

st.subheader("미리보기")
components.html(final_html, height=900, scrolling=True)

st.divider()
st.subheader("확정 및 PDF 출력")

if st.button("확정 후 PDF 생성"):
    if not customer_name.strip():
        st.warning("고객 성명을 입력해 주세요.")
        st.stop()

    context["customer"]["name"] = customer_name.strip()
    context["segment"]["headline"] = segment["headline"].replace("{customer_name}", customer_name.strip())
    final_html = build_final_html_for_both(context)

    try:
        pdf_bytes = chromium_pdf_bytes(final_html)
        filename = f"보장점검안내_{customer_name.strip()}_{age_band}_{gender}.pdf"
        st.download_button("PDF 다운로드", data=pdf_bytes, file_name=filename, mime="application/pdf")
    except Exception as e:
        st.error(f"PDF 생성(Playwright) 중 오류가 발생했습니다.\n\n오류: {e}")
