import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd

import os
import sys
import subprocess
import requests
from io import BytesIO
import base64

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib import font_manager as fm

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
# matplotlib font fix (Korean)
# =========================================================

@st.cache_resource(show_spinner=False)
def configure_matplotlib_korean_font() -> str:
    """
    Matplotlib 한글 폰트 설정을 '절대 안 죽게' 구성.
    - 폰트 파일이 0바이트/손상/파싱 실패하면 스킵
    - 최소 1개라도 성공하면 그 폰트를 matplotlib 기본으로 지정
    - 모두 실패하면 기본 폰트(DejaVu Sans)로 fallback
    반환: 최종 적용된 font family name
    """
    import matplotlib
    from matplotlib import font_manager as fm

    reg = FONT_DIR / "NotoSansKR-Regular.ttf"
    bold = FONT_DIR / "NotoSansKR-Bold.ttf"

    def is_valid_ttf(p: Path) -> bool:
        try:
            # 0바이트/이상치 방어: 정상 ttf면 보통 수백 KB 이상
            return p.exists() and p.is_file() and p.stat().st_size > 100_000
        except Exception:
            return False

    loaded_font_name = None

    # 후보를 순서대로 시도 (Regular 우선)
    for p in [reg, bold]:
        if not is_valid_ttf(p):
            continue
        try:
            fm.fontManager.addfont(str(p))
            # addfont 성공했으면 이 파일의 실제 폰트명을 얻어서 family로 지정
            loaded_font_name = fm.FontProperties(fname=str(p)).get_name()
            break
        except Exception:
            # 파싱 실패(FT2Font)면 그냥 스킵하고 다음 후보 시도
            continue

    # 최종 적용
    if loaded_font_name:
        matplotlib.rcParams["font.family"] = loaded_font_name
    else:
        # fallback (앱이 죽지 않는 게 우선)
        matplotlib.rcParams["font.family"] = "DejaVu Sans"

    matplotlib.rcParams["axes.unicode_minus"] = False
    return matplotlib.rcParams["font.family"]


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

def build_top7_combo_chart_data_uri(
    rows: list[dict],
    title: str,
    basis: str,
) -> str:
    """
    rows: fetch_top_rows() 결과
      - disease_code
      - disease_name_ko
      - patient_cnt (명)            # 여기서는 '연평균 환자수'로 들어온다고 가정(네 로직 기준)
      - total_cost (천원)           # 여기서는 '연평균 총진료비(천원)'로 들어온다고 가정
      - cost_per_patient (천원)     # 기간평균 1인당(천원)

    basis:
      - "total_cost"        : 연평균 총진료비(천원) -> 막대(억원)
      - "patient_cnt"       : 연평균 환자수(명)     -> 막대(명)
      - "cost_per_patient"  : 기간평균 1인당(천원)   -> 막대(만원)

    정책:
      - 막대 = 선택한 기준(basis) 1개
      - 보조선 2개(항상 표시):
          ① 연평균 총진료비(억원)
          ② 1인당 진료비(만원)
        단, 막대가 이미 그 지표인 경우에는 선에서 제외(중복 방지)
      - 보조축 위치:
          * 총진료비(억원) 축: 위(top)
          * 1인당(만원) 축: 아래(bottom)  (막대축 라벨/눈금은 제거)
      - 선 색상:
          * 미래에셋 블루 / 오렌지 고정
    """
    if not rows:
        return ""

    # ===== Unit conversions (원 데이터 단위: 천원) =====
    def to_eok_from_chewon(x: float) -> float:
        # 1억 원 = 100,000천원
        return float(x or 0) / 100000.0

    def to_man_from_chewon(x: float) -> float:
        # 1만 원 = 10천원
        return float(x or 0) / 10.0

    # ===== Prepare labels/values =====
    labels = []
    codes = []
    patient_avg = []
    total_cost_eok = []
    cpp_man = []

    for r in rows:
        code = (r.get("disease_code") or "").strip()
        name = (r.get("disease_name_ko") or "").strip() or code or "질병"
        codes.append(code)
        labels.append(f"{name} ({code})" if code else name)

        patient_avg.append(float(r.get("patient_cnt") or 0))  # 연평균 환자수(명)
        total_cost_eok.append(to_eok_from_chewon(float(r.get("total_cost") or 0)))  # 연평균 총진료비(억원)
        cpp_man.append(to_man_from_chewon(float(r.get("cost_per_patient") or 0)))   # 1인당(만원)

    # Top1이 위로 오게 뒤집기
    labels = labels[::-1]
    patient_avg = patient_avg[::-1]
    total_cost_eok = total_cost_eok[::-1]
    cpp_man = cpp_man[::-1]

    y = list(range(len(labels)))

    # ===== Basis selection (bar) =====
    if basis == "patient_cnt":
        bar_vals = patient_avg
        bar_label = "환자수(연평균, 명)"
        # 보조선은 총진료비 + 1인당
        show_cost_line = True
        show_cpp_line = True

    elif basis == "total_cost":
        bar_vals = total_cost_eok
        bar_label = "총 진료비(연평균, 억원)"
        # 보조선은 1인당만(총진료비는 막대와 중복이므로 선 제외)
        show_cost_line = False
        show_cpp_line = True

    else:  # "cost_per_patient"
        bar_vals = cpp_man
        bar_label = "1인당 진료비(기간평균, 만원)"
        # 보조선은 총진료비만(1인당은 막대와 중복)
        show_cost_line = True
        show_cpp_line = False

    # ===== Styling (Mirae Asset colors) =====
    MIRAE_BLUE = "#003A70"
    MIRAE_ORANGE = "#F58220"

    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    from io import BytesIO
    import base64

    plt.close("all")
    fig, ax_bar = plt.subplots(figsize=(12.5, 5.2), dpi=200)

    # ===== Bar plot =====
    ax_bar.barh(y, bar_vals)
    ax_bar.set_yticks(y)
    ax_bar.set_yticklabels(labels)

    # 메인 지표는 타이틀/라벨로 이미 명확하니, 아래 메인축 숫자/라벨은 제거
    ax_bar.set_xlabel("")                      # 아래 축 라벨 제거
    ax_bar.tick_params(axis="x", bottom=False, labelbottom=False)  # 아래 축 눈금/숫자 제거
    ax_bar.grid(axis="x", linestyle="--", alpha=0.25)

    # ===== Axes for auxiliary lines =====
    # 1) Top axis: total cost (억원)
    ax_top = ax_bar.twiny()
    ax_top.spines["top"].set_position(("axes", 1.02))  # 살짝 위로
    ax_top.set_xlabel("연평균 총 진료비(억원)")
    ax_top.xaxis.set_major_formatter(FuncFormatter(lambda v, p: f"{v:.0f}"))
    ax_top.tick_params(axis="x", direction="out")

    # 2) Bottom axis: cpp (만원)  -> 메인 bar 축과 겹치므로 바닥에 추가축 생성
    ax_bottom = ax_bar.twiny()
    ax_bottom.spines["bottom"].set_position(("axes", -0.12))  # 아래로 분리
    ax_bottom.xaxis.set_ticks_position("bottom")
    ax_bottom.xaxis.set_label_position("bottom")
    ax_bottom.set_xlabel("1인당 진료비(만원)")
    ax_bottom.xaxis.set_major_formatter(FuncFormatter(lambda v, p: f"{v:.0f}"))
    ax_bottom.tick_params(axis="x", direction="out")

    # ===== Lines (always draw 2 candidates, but avoid duplicates with bar) =====
    handles = []
    labels_leg = []

    # 선 ① 연평균 총 진료비(억원) - 오렌지
    if show_cost_line:
        h1, = ax_top.plot(
            total_cost_eok, y,
            marker="o",
            linewidth=2.4,
            color=MIRAE_ORANGE,
            label="연평균 총 진료비(억원)"
        )
        handles.append(h1)
        labels_leg.append("연평균 총 진료비(억원)")
        # 범위 맞추기
        ax_top.set_xlim(0, max(total_cost_eok) * 1.15 if max(total_cost_eok) > 0 else 1)

    # 선 ② 1인당 진료비(만원) - 블루
    if show_cpp_line:
        h2, = ax_bottom.plot(
            cpp_man, y,
            marker="o",
            linewidth=2.4,
            color=MIRAE_BLUE,
            label="1인당 진료비(만원)"
        )
        handles.append(h2)
        labels_leg.append("1인당 진료비(만원)")
        ax_bottom.set_xlim(0, max(cpp_man) * 1.25 if max(cpp_man) > 0 else 1)

    # ===== Bar axis scaling / formatting =====
    # 막대축은 표시를 숨겼지만, 텍스트 라벨 위치 계산을 위해 xlim은 세팅
    ax_bar.set_xlim(0, max(bar_vals) * 1.12 if max(bar_vals) > 0 else 1)

    # ===== Per-row text annotation (main once + (subs)) =====
    for i in range(len(labels)):
        # 메인 값(막대)
        if basis == "patient_cnt":
            main_txt = f"{int(patient_avg[i]):,}명"
            subs = f"총 {total_cost_eok[i]:.1f}억 · 1인당 {cpp_man[i]:.1f}만"
        elif basis == "total_cost":
            main_txt = f"{total_cost_eok[i]:.1f}억"
            subs = f"환자 {int(patient_avg[i]):,}명 · 1인당 {cpp_man[i]:.1f}만"
        else:  # cpp
            main_txt = f"{cpp_man[i]:.1f}만"
            subs = f"환자 {int(patient_avg[i]):,}명 · 총 {total_cost_eok[i]:.1f}억"

        ax_bar.text(
            bar_vals[i],
            i,
            f"  {main_txt} ({subs})",
            va="center",
            fontsize=9,
        )

    # ===== Title (기준까지만) =====
    fig.suptitle(title, fontsize=14, fontweight="bold")

    # ===== Legend =====
    if handles:
        ax_bar.legend(handles=handles, labels=labels_leg, loc="lower right", frameon=True)

    fig.tight_layout(rect=[0, 0, 1, 0.95])

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    png_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{png_b64}"



STAT_SORT_OPTIONS = {
    "총 진료비(연평균)": {"key": "total_cost"},
    "환자수(연평균)": {"key": "patient_cnt"},
    "1인당 진료비(기간평균)": {"key": "cost_per_patient"},
}


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_top_rows(
    start_year: int,
    end_year: int,
    age_group: str,
    sex: str,
    sort_key: str = "total_cost",
    limit: int = 7,
) -> list[dict]:
    """
    기간(start_year~end_year) + 연령/성별에서 sort_key 기준 상위 N개 질병 rows 반환
    sort_key: total_cost | patient_cnt | cost_per_patient
    반환 컬럼:
      disease_code, disease_name_ko, patient_cnt, total_cost(천원), cost_per_patient(천원)
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
    return rows


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


def build_top_table_df(rows: list[dict]) -> "pd.DataFrame":
    if not rows:
        return pd.DataFrame(columns=["disease_code", "질병", "총진료비(억원)", "환자수", "1인당(만원)"])

    data = []
    for r in rows:
        code = (r.get("disease_code") or "").strip()
        name = (r.get("disease_name_ko") or "").strip() or code

        total_cost_억원 = float(r.get("total_cost") or 0) / 100000.0
        cpp_만원 = float(r.get("cost_per_patient") or 0) / 10.0

        data.append({
            "disease_code": code,
            "질병": name,
            "총진료비(억원)": round(total_cost_억원, 1),
            "환자수": int(r.get("patient_cnt") or 0),
            "1인당(만원)": round(cpp_만원, 1),
        })

    return pd.DataFrame(data)

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

st.write("REG", (FONT_DIR / "NotoSansKR-Regular.ttf").exists(), (FONT_DIR / "NotoSansKR-Regular.ttf").stat().st_size if (FONT_DIR / "NotoSansKR-Regular.ttf").exists() else None)
st.write("BOLD", (FONT_DIR / "NotoSansKR-Bold.ttf").exists(), (FONT_DIR / "NotoSansKR-Bold.ttf").stat().st_size if (FONT_DIR / "NotoSansKR-Bold.ttf").exists() else None)


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
    top_rows = fetch_top_rows(start_year, end_year, age_group, sex, sort_key=sort_key, limit=7)
except Exception as e:
    st.error(f"D1 통계 조회 실패: {e}")
    top_rows = []

sex_display = "남성" if sex == "M" else "여성"

chart_title = (
    f"Top7 질병 통계 "
    f"({start_year}~{end_year} · {age_band} · {sex_display} · 기준: {sort_label})"
)
chart_data_uri = build_top7_combo_chart_data_uri(
    top_rows,
    title=chart_title,
    basis=sort_key,
    start_year=int(start_year),
    end_year=int(end_year),
)

st.markdown("#### 통계 미리보기 (차트)")

if chart_data_uri:
    b64 = chart_data_uri.split(",", 1)[1]
    st.image(base64.b64decode(b64))
else:
    st.warning("차트를 만들 데이터가 없습니다. 조건을 바꿔보세요.")

def krw_to_eok(n: float | int) -> float:
    """천원 → 억원"""
    return round((float(n or 0) * 1_000) / 1e8, 2)

def krw_to_man(n: float | int) -> float:
    """천원 → 만원"""
    return round((float(n or 0) * 1_000) / 1e4, 1)


with st.expander("통계 상세 (Top7 테이블)"):
    st.dataframe(
        [
            {
                "질병코드": r.get("disease_code"),
                "질병명": r.get("disease_name_ko") or r.get("disease_code"),
                "총진료비(억원)": krw_to_eok(r.get("total_cost")),
                "환자수(명)": int(r.get("patient_cnt") or 0),
                "1인당 진료비(만원)": krw_to_man(r.get("cost_per_patient")),
            }
            for r in top_rows
        ],
        use_container_width=True,
        hide_index=True,
    )


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
        "top7_basis": sort_label,
        "chart_data_uri": chart_data_uri,
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
