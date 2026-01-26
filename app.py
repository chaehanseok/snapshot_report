# =========================================================
# 최종 통합본 (유병률 기반 + 1인당 진료비 조건필터 + 현재/미래/신규부각 연동)
# - Top15 기준: 총진료비(연평균) / 유병률(10만명당) / 1인당 진료비(기간평균)
# - 조건필터: (모든 기준에서) 최소 유병률 + 최소 1인당 진료비 적용
# - 현재 연령대 / 이후 연령대(미래 위험) / 신규 부각 질병(현재 Top15에 없음) 모두 동일 조건을 따라감
# =========================================================

import base64, json, hmac, hashlib, time, re
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd

import os
import sys
import subprocess
import requests
from io import BytesIO

import streamlit as st
import streamlit.components.v1 as components
from jinja2 import Environment, FileSystemLoader, select_autoescape
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib import font_manager as fm
from datetime import date, timedelta
from datetime import datetime
from zoneinfo import ZoneInfo
import boto3
import copy
from utils.r2 import generate_presigned_pdf_url
from utils.auth import verify_token
from utils.ui_common import inject_global_css, cleanup_token_timer_overlay, inject_base_css_only

# =========================================================
# Playwright runtime config (Streamlit Cloud-safe)
# =========================================================
PW_DIR = Path("/tmp/pw-browsers")
PW_DIR.mkdir(parents=True, exist_ok=True)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PW_DIR)


@st.cache_resource(show_spinner=False)
def ensure_playwright_chromium() -> bool:
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

def format_token_exp(exp: int | None) -> str:
    if not exp:
        return "만료 정보 없음"

    dt = datetime.fromtimestamp(exp, tz=ZoneInfo("Asia/Seoul"))
    return dt.strftime("%Y-%m-%d %H:%M")


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
    elif age_band.startswith("60"):
        a = "60"
    else:
        a = "70"
    g = "M" if gender == "남성" else "F"
    return f"{a}_{g}"


# =========================================================
# D1 query (Cloudflare D1 REST API)
# =========================================================
def d1_query(sql: str, params: list) -> list[dict]:
    account_id = st.secrets["CF_ACCOUNT_ID"]
    api_token = st.secrets["CF_API_TOKEN"]
    db_id = st.secrets["D1_DATABASE_ID"]

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}/query"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    payload = {"sql": sql, "params": params}

    r = requests.post(url, headers=headers, json=payload, timeout=30)

    if not r.ok:
        st.error("D1 ERROR RESPONSE:")
        st.code(r.text)
        r.raise_for_status()

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
        return (2010, 2024)
    return (int(row[0].get("min_year") or 2010), int(row[0].get("max_year") or 2024))

def get_today_report_issue_count() -> int:
    """
    KST 기준 오늘 발행된 report_issue 건수 조회
    - #### 시퀀스 생성용
    """
    today_kst = today_kst_date_str()

    sql = """
    SELECT COUNT(*) AS cnt
    FROM report_issue
    WHERE date(created_at) = ?;
    """
    rows = d1_query(sql, [today_kst])   # ⭐⭐⭐ 여기 핵심
    if not rows:
        return 0
    return int(rows[0].get("cnt", 0))

def insert_report_issue(
    *,
    fc_id: str,
    fc_name: str,
    customer_name: str | None,
    customer_gender: str,
    customer_age_band: str,
    start_year: int,
    end_year: int,
    sort_key: str,
    min_prev_100k: float,
    min_cpp_manwon: int,
    pdf_r2_key: str,
    pdf_filename: str,
    compliance_code: str,
    segments_version: str,
):
    sql = """
    INSERT INTO report_issue (
        fc_id, fc_name,
        customer_name, customer_gender, customer_age_band,
        start_year, end_year, sort_key,
        min_prev_100k, min_cpp_manwon,
        pdf_r2_key, pdf_filename,
        compliance_code, segments_version
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """
    params = [
        fc_id, fc_name,
        customer_name, customer_gender, customer_age_band,
        start_year, end_year, sort_key,
        min_prev_100k, min_cpp_manwon,
        pdf_r2_key, pdf_filename,
        compliance_code, segments_version,
    ]
    d1_query(sql, params)

def make_dummy_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%Dummy PDF for test\n%%EOF"

# =========================================================
# matplotlib font fix (Korean)
# =========================================================
@st.cache_resource(show_spinner=False)
def configure_matplotlib_korean_font() -> str:
    reg = FONT_DIR / "NotoSansKR-Regular.ttf"
    bold = FONT_DIR / "NotoSansKR-Bold.ttf"

    def is_valid_ttf(p: Path) -> bool:
        try:
            return p.exists() and p.is_file() and p.stat().st_size > 100_000
        except Exception:
            return False

    loaded_font_name = None
    for p in [reg, bold]:
        if not is_valid_ttf(p):
            continue
        try:
            fm.fontManager.addfont(str(p))
            loaded_font_name = fm.FontProperties(fname=str(p)).get_name()
            break
        except Exception:
            continue

    if loaded_font_name:
        matplotlib.rcParams["font.family"] = loaded_font_name
    else:
        matplotlib.rcParams["font.family"] = "DejaVu Sans"

    matplotlib.rcParams["axes.unicode_minus"] = False
    return matplotlib.rcParams["font.family"]


# =========================================================
# Units / Formatting helpers
# =========================================================
def chewon_to_eok(x: float | int) -> float:
    # 천원 -> 억원 (천원/100000 = 억원)
    return float(x or 0) / 100000.0


def chewon_to_man(x: float | int) -> float:
    # 천원 -> 만원 (천원/10 = 만원)
    return float(x or 0) / 10.0


def manwon_to_chewon(m: int) -> int:
    # 만원 -> 천원
    return int(m) * 10

def get_r2_client():
    """
    Cloudflare R2 S3-compatible client
    """
    return boto3.client(
        "s3",
        endpoint_url=st.secrets["R2_ENDPOINT"],
        aws_access_key_id=st.secrets["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def upload_pdf_to_r2(
    pdf_bytes: bytes,
    compliance_code: str,
) -> tuple[str, str]:
    r2 = get_r2_client()
    bucket = st.secrets["R2_BUCKET_NAME"]

    now = datetime.now()
    year = now.strftime("%Y")
    mmdd = now.strftime("%m%d")

    filename = f"{compliance_code}.pdf"
    r2_key = f"report/{year}/{mmdd}/{filename}"

    r2.put_object(
        Bucket=bucket,
        Key=r2_key,
        Body=pdf_bytes,
        ContentType="application/pdf",
    )

    return r2_key, filename

def generate_compliance_code(
    *,
    service_name: str,
    version: str,
) -> str:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    year = now_kst.strftime("%Y")
    mmdd = now_kst.strftime("%m%d")

    seq = get_next_daily_seq()      # ⭐ 여기
    seq_str = f"{seq:04d}"

    return f"{year}-{service_name}-v{version}-{mmdd}{seq_str}"

def publish_report(
    *,
    pdf_bytes: bytes,
    compliance_code: str,   # ⭐ 외부에서 받음
    segments_version: str,
    fc_id: str,
    fc_name: str,
    customer_name: str | None,
    customer_gender: str,
    customer_age_band: str,
    start_year: int,
    end_year: int,
    sort_key: str,
    min_prev_100k: float,
    min_cpp_manwon: int,
) -> str:
    """
    리포트 공식 발행 처리 (동시성 안전)
    - 발행번호 생성
    - PDF R2 업로드
    - report_issue 감사 메타 DB 기록
    """

    last_error = None

    for attempt in range(5):  # ⭐ 최대 5번 재시도
        # compliance_code = generate_compliance_code(
        #     service_name="보장점검",
        #     version=segments_version,
        # )

        try:
            # 1️⃣ PDF → R2 업로드
            pdf_r2_key, pdf_filename = upload_pdf_to_r2(
                pdf_bytes=pdf_bytes,
                compliance_code=compliance_code,
            )

            # 2️⃣ DB INSERT
            insert_report_issue(
                fc_id=fc_id,
                fc_name=fc_name,
                customer_name=customer_name,
                customer_gender=customer_gender,
                customer_age_band=customer_age_band,
                start_year=start_year,
                end_year=end_year,
                sort_key=sort_key,
                min_prev_100k=min_prev_100k,
                min_cpp_manwon=min_cpp_manwon,
                pdf_r2_key=pdf_r2_key,
                pdf_filename=pdf_filename,
                compliance_code=compliance_code,
                segments_version=segments_version,
            )

            # ✅ 성공
            return compliance_code

        except Exception as e:
            last_error = e

            # UNIQUE 충돌 → 정상 재시도
            if "UNIQUE constraint failed" in str(e):
                time.sleep(0.05)  # 아주 짧은 대기
                continue

            # 그 외 에러는 즉시 실패
            raise

    # 5번 다 실패
    raise RuntimeError(f"발행번호 생성 실패 (동시성 충돌): {last_error}")

def get_next_daily_seq() -> int:
    """
    KST 기준 일자별 발행 시퀀스를 DB에서 원자적으로 증가시켜 반환
    """
    today = today_kst_date_str()  # 'YYYY-MM-DD'

    sql = """
    INSERT INTO daily_issue_seq (issue_date, seq)
    VALUES (?, 1)
    ON CONFLICT(issue_date)
    DO UPDATE SET seq = seq + 1
    RETURNING seq;
    """

    rows = d1_query(sql, [today])
    return int(rows[0]["seq"])

# =========================================================
# Chart (Top15 combo: bar 1 + line 2)  [유병률 기반]
# =========================================================
def build_top10_combo_chart_data_uri(
    rows: list[dict],
    title: str,
    basis: str,
    start_year: int,
    end_year: int,
    compact: bool = False,   # ✅ 추가
) -> str:
    """
    Top15 콤보 차트 (막대 1 + 보조선 2) - 유병률 버전

    rows 필드(필수):
      - disease_code
      - disease_name_ko
      - total_cost           (기간합, 천원)
      - prevalence_per_100k  (기간집계, 10만명당)
      - cost_per_patient     (기간평균, 천원)

    표기 정책:
      - 총진료비: 연평균(기간합 ÷ years) → 억원
      - 유병률: 10만명당 그대로
      - 1인당: 천원 → 만원
      - 막대 = basis(선택 기준)
      - 보조선 = 나머지 2개 (top/bottom)
      - 메인 막대축 숫자 숨김(값 라벨로 표시)
      - 보조축 숫자는 표시
      - 상단 중복 라인 제거, 하단 축은 표에 거의 붙게
    """
    if not rows:
        return ""

    try:
        configure_matplotlib_korean_font()
    except Exception:
        pass

    MIRAE_BLUE = "#003A70"
    MIRAE_ORANGE = "#F58220"

    years = max(1, int(end_year) - int(start_year) + 1)

    labels: list[str] = []
    cost_avg_eok: list[float] = []
    prev_100k: list[float] = []
    cpp_man: list[float] = []

    for r in rows:
        code = (r.get("disease_code") or "").strip()
        name_raw = (r.get("disease_name_ko") or "").strip() or code or "질병"

        # ⭐ 여기서 표시용 truncate 적용
        name_display = truncate_korean(name_raw, 15)

        labels.append(f"{name_display} ({code})" if code else name_display)

        total_cost_chewon = float(r.get("total_cost") or 0)
        prevalence = float(r.get("prevalence_per_100k") or 0)
        cpp_chewon = float(r.get("cost_per_patient") or 0)

        cost_avg_eok.append(chewon_to_eok(total_cost_chewon / years))
        prev_100k.append(prevalence)
        cpp_man.append(chewon_to_man(cpp_chewon))

    # Top1이 위로 보이도록 reverse
    labels = labels[::-1]
    cost_avg_eok = cost_avg_eok[::-1]
    prev_100k = prev_100k[::-1]
    cpp_man = cpp_man[::-1]
    y = list(range(len(labels)))

    # basis 선택
    if basis == "total_cost":
        bar_vals = cost_avg_eok
        main_unit = "억"
        aux1 = ("유병률", prev_100k, " /10만", MIRAE_ORANGE, "top")
        aux2 = ("1인당", cpp_man, "만", MIRAE_BLUE, "bottom")
    elif basis == "prevalence_per_100k":
        bar_vals = prev_100k
        main_unit = "/10만"
        aux1 = ("연평균 총 진료비", cost_avg_eok, "억", MIRAE_ORANGE, "top")
        aux2 = ("1인당", cpp_man, "만", MIRAE_BLUE, "bottom")
    else:  # cost_per_patient
        bar_vals = cpp_man
        main_unit = "만"
        aux1 = ("유병률", prev_100k, " /10만", MIRAE_ORANGE, "top")
        aux2 = ("연평균 총 진료비", cost_avg_eok, "억", MIRAE_BLUE, "bottom")

    plt.close("all")
    if compact:
        # 2페이지용 (차트 + 테이블 + 문구 공존)
        fig, ax = plt.subplots(figsize=(12.5, 7.4), dpi=300)
    else:
        # 1페이지용 (차트 단독 중심)
        fig, ax = plt.subplots(figsize=(12.5, 10.0), dpi=300)

    ax.barh(y, bar_vals)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=13)

    ax.set_xlabel("")
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    ax.grid(axis="x", linestyle="--", alpha=0.25)
    ax.spines["top"].set_visible(False)

    def fmt_axis(unit: str):
        if unit.strip() == "억":
            return FuncFormatter(lambda v, p: f"{v:,.0f}")
        if unit.strip() == "만":
            return FuncFormatter(lambda v, p: f"{v:,.0f}")
        # /10만
        return FuncFormatter(lambda v, p: f"{v:,.0f}")

    ax_top = ax.twiny()
    ax_bottom = ax.twiny()

    ax_top.spines["top"].set_position(("axes", 1.02))
    ax_bottom.spines["bottom"].set_position(("axes", -0.0001))
    ax_bottom.xaxis.set_ticks_position("bottom")
    ax_bottom.xaxis.set_label_position("bottom")

    for a in (ax_top, ax_bottom):
        a.spines["left"].set_visible(False)
        a.spines["right"].set_visible(False)
    ax_top.spines["bottom"].set_visible(False)
    ax_bottom.spines["top"].set_visible(False)

    ax_top.tick_params(axis="x", top=True, labeltop=True, direction="out", pad=2, labelsize=11)
    ax_bottom.tick_params(axis="x", bottom=True, labelbottom=True, direction="out", pad=2, labelsize=11)

    aux_top = aux1 if aux1[4] == "top" else aux2
    aux_bot = aux2 if aux2[4] == "bottom" else aux1

    top_label, top_vals, top_unit, top_color, _ = aux_top
    bot_label, bot_vals, bot_unit, bot_color, _ = aux_bot

    ax_top.set_xlim(0, (max(top_vals) * 1.25) if max(top_vals) > 0 else 1)
    ax_bottom.set_xlim(0, (max(bot_vals) * 1.25) if max(bot_vals) > 0 else 1)

    ax_top.set_xlabel(f"{top_label}({top_unit.strip()})")
    ax_bottom.set_xlabel(f"{bot_label}({bot_unit.strip()})")
    ax_top.xaxis.set_major_formatter(fmt_axis(top_unit))
    ax_bottom.xaxis.set_major_formatter(fmt_axis(bot_unit))

    ax_top.xaxis.label.set_size(12)
    ax_bottom.xaxis.label.set_size(12)

    h_top, = ax_top.plot(top_vals, y, marker="o", linewidth=2.4, color=top_color, label=f"{top_label}({top_unit.strip()})")
    h_bot, = ax_bottom.plot(bot_vals, y, marker="o", linewidth=2.4, color=bot_color, label=f"{bot_label}({bot_unit.strip()})")

    ax.set_xlim(0, (max(bar_vals) * 1.12) if max(bar_vals) > 0 else 1)

    # 값 라벨: 메인 + (보조2개)
    for i in range(len(labels)):
        main_txt = f"{bar_vals[i]:,.1f}{main_unit}"
        top_txt = f"{top_vals[i]:,.1f}{top_unit.strip()}"
        bot_txt = f"{bot_vals[i]:,.1f}{bot_unit.strip()}"
        ax.text(bar_vals[i], i, f"  {main_txt} ({top_txt} · {bot_txt})", va="center", fontsize=12.5)

    fig.suptitle(title, fontsize=16, fontweight="bold")
    ax.legend(handles=[h_top, h_bot], loc="lower right", frameon=True,fontsize=10.5)

    fig.tight_layout(rect=[0, 0.02, 1, 0.95])

    buf = BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    png_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{png_b64}"


def render_emerging_table_html(rows: list[dict]) -> str:
    if not rows:
        return ""

    headers = [
        "질병명",
        "총진료비(연평균, 억원)",
        "1인당 진료비(만원)",
    ]

    html = '<table class="table-compact"><thead><tr>'
    for h in headers:
        html += f"<th>{h}</th>"
    html += "</tr></thead><tbody>"

    for r in rows:
        html += "<tr>"
        html += f"<td>{r.get('disease_name_ko') or r.get('disease_code')}</td>"
        html += f"<td>{chewon_to_eok((float(r.get('total_cost') or 0) / years)):,.1f}</td>"
        html += f"<td>{chewon_to_man(r.get('cost_per_patient')):,.0f}</td>"
        html += "</tr>"

    html += "</tbody></table>"
    return html

def truncate_korean(text: str, max_len: int = 15) -> str:
    if not text:
        return text
    return text if len(text) <= max_len else text[:max_len] + "..."


# =========================================================
# Sort options / Age mapping
# =========================================================
STAT_SORT_OPTIONS = {
    "총 진료비(연평균)": {"key": "total_cost"},
    "유병률(10만명당)": {"key": "prevalence_per_100k"},
    "1인당 진료비(기간평균)": {"key": "cost_per_patient"},
}

AGE_GROUP_MAP = {
    "20대": "20_29",
    "30대": "30_39",
    "40대": "40_49",
    "50대": "50_59",
    "60대": "60_69",
    "70대": "70_79",
}

AFTER_AGE_GROUPS = {
    "20대": ["30_39", "40_49", "50_59", "60_69", "70_79", "80_plus"],
    "30대": ["40_49", "50_59", "60_69", "70_79", "80_plus"],
    "40대": ["50_59", "60_69", "70_79", "80_plus"],
    "50대": ["60_69", "70_79", "80_plus"],
    "60대": ["70_79", "80_plus"],
    "70대": ["80_plus"],
}


# =========================================================
# D1 fetch (현재/이후): 유병률 집계 + 조건필터(유병률, 1인당) 공통 적용
# =========================================================
@st.cache_data(show_spinner=False, ttl=3600)
def fetch_top_rows(
    start_year: int,
    end_year: int,
    age_group: str,
    sex: str,
    sort_key: str = "total_cost",
    limit: int = 15,
    min_prev_100k: float | None = None,
    min_cpp_chewon: int | None = None,
) -> list[dict]:

    if sort_key not in ("total_cost", "prevalence_per_100k", "cost_per_patient"):
        sort_key = "total_cost"

    order_by = {
        "total_cost": "total_cost DESC",
        "prevalence_per_100k": "prevalence_per_100k DESC",
        "cost_per_patient": "cost_per_patient DESC",
    }[sort_key]

    having_sql = "HAVING 1=1\n"
    params: list = [int(start_year), int(end_year), age_group, sex]

    if min_prev_100k is not None and float(min_prev_100k) > 0:
        having_sql += (
            "  AND (CAST(SUM(m.patient_cnt) AS REAL) / NULLIF(SUM(m.population), 0)) * 100000.0 >= ?\n"
        )
        params.append(float(min_prev_100k))

    if min_cpp_chewon is not None and int(min_cpp_chewon) > 0:
        having_sql += (
            "  AND (CAST(SUM(m.total_cost) AS REAL) / NULLIF(SUM(m.patient_cnt), 0)) >= ?\n"
        )
        params.append(int(min_cpp_chewon))

    params.append(int(limit))

    sql = f"""
    WITH agg AS (
      SELECT
        m.disease_code AS disease_code,
        COALESCE(NULLIF(TRIM(d.disease_name_ko), ''), m.disease_code) AS disease_name_ko,

        SUM(m.total_cost)  AS total_cost,
        SUM(m.patient_cnt) AS patient_cnt,
        SUM(m.population)  AS population,

        (CAST(SUM(m.patient_cnt) AS REAL) / NULLIF(SUM(m.population), 0)) * 100000.0 AS prevalence_per_100k,
        CAST(SUM(m.total_cost) AS REAL) / NULLIF(SUM(m.patient_cnt), 0) AS cost_per_patient
      FROM disease_year_age_sex_metrics m
      LEFT JOIN disease d
        ON m.disease_code = d.disease_code
      WHERE m.year BETWEEN ? AND ?
        AND m.age_group = ?
        AND m.sex = ?
      GROUP BY m.disease_code, COALESCE(NULLIF(TRIM(d.disease_name_ko), ''), m.disease_code)
      {having_sql}
    )
    SELECT * FROM agg
    ORDER BY {order_by}
    LIMIT ?;
    """
    return d1_query(sql, params)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_top_rows_after_age(
    start_year: int,
    end_year: int,
    after_age_groups: list[str],
    sex: str,
    sort_key: str = "total_cost",
    limit: int = 15,
    min_prev_100k: float | None = None,
    min_cpp_chewon: int | None = None,
) -> list[dict]:

    if not after_age_groups:
        return []

    if sort_key not in ("total_cost", "prevalence_per_100k", "cost_per_patient"):
        sort_key = "total_cost"

    order_by = {
        "total_cost": "total_cost DESC",
        "prevalence_per_100k": "prevalence_per_100k DESC",
        "cost_per_patient": "cost_per_patient DESC",
    }[sort_key]

    placeholders = ",".join(["?"] * len(after_age_groups))
    having_sql = "HAVING 1=1\n"
    params: list = [int(start_year), int(end_year), sex, *after_age_groups]

    if min_prev_100k is not None and float(min_prev_100k) > 0:
        having_sql += (
            "  AND (CAST(SUM(m.patient_cnt) AS REAL) / NULLIF(SUM(m.population), 0)) * 100000.0 >= ?\n"
        )
        params.append(float(min_prev_100k))

    if min_cpp_chewon is not None and int(min_cpp_chewon) > 0:
        having_sql += (
            "  AND (CAST(SUM(m.total_cost) AS REAL) / NULLIF(SUM(m.patient_cnt), 0)) >= ?\n"
        )
        params.append(int(min_cpp_chewon))

    params.append(int(limit))

    sql = f"""
    WITH agg AS (
      SELECT
        m.disease_code AS disease_code,
        COALESCE(NULLIF(TRIM(d.disease_name_ko), ''), m.disease_code) AS disease_name_ko,

        SUM(m.total_cost)  AS total_cost,
        SUM(m.patient_cnt) AS patient_cnt,
        SUM(m.population)  AS population,

        (CAST(SUM(m.patient_cnt) AS REAL) / NULLIF(SUM(m.population), 0)) * 100000.0 AS prevalence_per_100k,
        CAST(SUM(m.total_cost) AS REAL) / NULLIF(SUM(m.patient_cnt), 0) AS cost_per_patient
      FROM disease_year_age_sex_metrics m
      LEFT JOIN disease d
        ON m.disease_code = d.disease_code
      WHERE m.year BETWEEN ? AND ?
        AND m.sex = ?
        AND m.age_group IN ({placeholders})
      GROUP BY m.disease_code, COALESCE(NULLIF(TRIM(d.disease_name_ko), ''), m.disease_code)
      {having_sql}
    )
    SELECT * FROM agg
    ORDER BY {order_by}
    LIMIT ?;
    """
    return d1_query(sql, params)


def pick_emerging_rows(now_rows: list[dict], after_rows: list[dict], limit: int = 5) -> list[dict]:
    now_codes = {(r.get("disease_code") or "").strip() for r in (now_rows or [])}
    emerging = [r for r in (after_rows or []) if ((r.get("disease_code") or "").strip() not in now_codes)]
    return emerging[:limit]


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
.bullets{ list-style:none !important; margin:0 !important; padding-left:0 !important; }
.bullets li{ position:relative; padding-left:16px; margin:5px 0; }
.bullets li::before{ content:"•"; position:absolute; left:0; top:0; }
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

def calc_table_height(
    row_count: int,
    row_height: int = 35,
    header_height: int = 40,
    min_height: int = 120,
    max_height: int = 600,
) -> int:
    """
    Streamlit dataframe 높이를 row 개수에 따라 자동 계산
    - row_height: 데이터 행 1줄 높이(px)
    - header_height: 헤더 높이(px)
    - min/max: 과도한 축소/확대 방지
    """
    h = header_height + row_height * max(row_count, 1)
    return max(min_height, min(h, max_height))

def render_table_html(rows: list[dict]) -> str:
    if not rows:
        return ""

    headers = [
        "질병명",
        "총진료비(연평균, 억원)",
        "유병률(10만명당)",
        "1인당 진료비(만원)",
    ]

    html = "<table><thead><tr>"
    for h in headers:
        html += f"<th>{h}</th>"
    html += "</tr></thead><tbody>"

    for r in rows:
        html += "<tr>"
        html += f"<td>{r.get('disease_name_ko') or r.get('disease_code')}</td>"
        html += f"<td>{chewon_to_eok((float(r.get('total_cost') or 0) / years)):,.1f}</td>"
        html += f"<td>{float(r.get('prevalence_per_100k') or 0):,.1f}</td>"
        html += f"<td>{chewon_to_man(r.get('cost_per_patient')):,.1f}</td>"
        html += "</tr>"

    html += "</tbody></table>"
    return html

def next_age_band_label(age_band: str) -> str:
    try:
        base = int(age_band.replace("대", ""))
        return f"{base + 10}대 이후"
    except Exception:
        return "이후 연령대"
    
def today_kst_date_str() -> str:
    """
    KST 기준 오늘 날짜를 YYYY-MM-DD 문자열로 반환
    """
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")

def insert_report_event(
    compliance_code: str,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
):
    sql = """
    INSERT INTO report_issue_event
    (compliance_code, event_type, actor_type, actor_id)
    VALUES (?, ?, ?, ?);
    """
    d1_query(sql, [compliance_code, event_type, actor_type, actor_id])

def get_auth_token() -> str | None:
    # 1) 세션 우선
    tok = st.session_state.get("auth_token")
    if tok:
        return tok

    # 2) URL 쿼리 파라미터로 복구
    tok = st.query_params.get("token")
    if isinstance(tok, list):
        tok = tok[0]

    if tok:
        st.session_state["auth_token"] = tok
        return tok

    return None


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
        page.wait_for_timeout(150)
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
st.set_page_config(page_title="보장 점검 유인 팜플렛",layout="wide", initial_sidebar_state="collapsed")

st.header("고객별 보장분석 권유 리포트 생성")

# ✅ 기본 Pages 네비 숨김 (가장 먼저)
st.markdown(
    """
    <style>
      [data-testid="stSidebarNav"] { display: none !important; }
      [data-testid="stSidebarNavSeparator"] { display: none !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

inject_base_css_only()   

token = get_auth_token()
if not token:
    st.error("유효한 접속 정보가 없습니다. M.POST 게이트웨이 링크로 접속해 주세요.")
    st.stop()

user = verify_token(token)

now = int(time.time())
exp = user.get("exp")

token_expired = exp is not None and now > exp

if token_expired:
    st.warning("⏰ 접속 토큰이 만료되었습니다. M.POST 게이트웨이 링크로 접속해 주세요.")
    st.stop()

if user.get("role") == "admin":
    st.info("관리자 계정으로 접속했습니다.")
    # 👉 관리자 전용 UI를 보여주거나
    # 👉 이 페이지가 FC 전용이면 아래처럼 차단
    st.error("이 페이지는 FC 전용 화면입니다.")
    st.stop()

elif user.get("fc_code"):
    fc = user
    fc_code = user["fc_code"]

else:
    st.error("유효한 FC 접속 정보가 없습니다.")
    st.stop()

segments_db = load_json(SEGMENTS_PATH)

planner_org_display = org_display(BRAND_NAME, fc.get("org", ""))
planner_phone_display = format_phone_3_4_4(fc["phone"])

token_exp = user.get("exp")   # verify_token 결과에서

if token_exp:
    exp_kst = datetime.fromtimestamp(token_exp, ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    exp_text = f"\n🔐 토큰 만료 시각(KST): {exp_kst}"
else:
    exp_text = "\n🔐 토큰 만료 시각(KST): 만료 정보 없음"

st.markdown(
    f"""
    <div style="
        background-color:#e6f4ea;
        border-left:6px solid #2e7d32;
        padding:14px 16px;
        border-radius:6px;
        margin-bottom:12px;
    ">
        <div style="font-weight:600; font-size:1rem; color:#1b5e20;">
            미래에셋금융서비스 소속 인증 완료
        </div>
        <div style="margin-top:6px; font-size:0.9rem; color:#2e7d32;">
            🔐 토큰 만료 시각(KST): <b>{exp_kst}</b>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


with st.container():
    st.markdown(
        f"""
        <div style="
            background-color:#FFF3E8;
            border-left:6px solid #F58220;
            padding:16px 18px;
            border-radius:10px;
            line-height:1.5;
            margin-bottom:10px;
        ">
            <div style="font-weight:700; color:#F58220; margin-bottom:6px;">
                👤 FC 정보
            </div>
            <div><b>FC명</b> : {fc['name']}</div>
            <div><b>소속</b> : {planner_org_display}</div>
            <div><b>연락처</b> : {planner_phone_display}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


with st.container():
    # 위 여백만 살짝
    st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)

    if st.button(
        "📄 내 발행 이력",
        use_container_width=True,
        help=(
            "본인이 발행한 보장점검 리포트의\n"
            "발행 이력 및 PDF 다운로드 내역을\n"
            "확인할 수 있습니다."
        ),
    ):
        st.session_state["auth_token"] = token
        st.switch_page("pages/my_reports.py")


st.divider()


# st.write(d1_query("SELECT name FROM sqlite_master WHERE type='table';", []))
# st.write("KST 오늘 날짜:", today_kst_date_str())
# st.write("오늘 발행 건수:", get_today_report_issue_count())

# -------------------------
# 고객 기본 정보 (한 줄 정렬)
# -------------------------
st.subheader("고객 기본 정보")
c1, c2, c3 = st.columns([2, 1, 1])
with c1:
    customer_name = st.text_input("고객 성명", value="")
with c2:
    gender = st.selectbox("성별", ["남성", "여성"])
with c3:
    age_band = st.selectbox("연령대", ["20대", "30대", "40대", "50대", "60대", "70대"])

key = segment_key(age_band, gender)
segment = segments_db["segments"].get(key)
if not segment:
    st.error(f"콘텐츠 세트가 없습니다: {key}")
    st.stop()

# -------------------------
# 통계 표시 옵션
# -------------------------
st.subheader("통계 표시 옵션")
min_year, max_year = fetch_year_range()

colA, colB = st.columns(2)
with colA:
    start_year = st.number_input("시작년도", min_value=int(min_year), max_value=int(max_year), value=int(max_year), step=1)
with colB:
    end_year = st.number_input("종료년도", min_value=int(min_year), max_value=int(max_year), value=int(max_year), step=1)

if start_year > end_year:
    start_year, end_year = end_year, start_year
    st.info(f"시작/종료년도를 자동 보정했습니다: {start_year} ~ {end_year}")

sort_label = st.radio("Top15 기준", options=list(STAT_SORT_OPTIONS.keys()), index=0, horizontal=True)
sort_key = STAT_SORT_OPTIONS[sort_label]["key"]

# ✅ 조건필터: 유병률 + 1인당 진료비 (항상 노출, 둘 다 공통 적용)
st.caption("조건 필터(공통): 현재/미래/신규 부각 통계 모두 동일 기준으로 필터링됩니다.")

fc1, fc2 = st.columns(2)
with fc1:
    min_prev_100k = st.slider("최소 유병률(10만명당)", 0.0, 2000.0, 50.0, 5.0)
with fc2:
    min_cpp_manwon = st.slider("최소 1인당 진료비(만원)", 0, 5000, 100, 10)
    min_cpp_chewon = manwon_to_chewon(min_cpp_manwon)

# -------------------------
# D1 기반 통계 (현재 + 이후 + 신규부각)
# -------------------------
years = max(1, int(end_year) - int(start_year) + 1)

age_group = AGE_GROUP_MAP.get(age_band, "50_59")
sex = "M" if gender == "남성" else "F"
sex_display = "남성" if sex == "M" else "여성"

st.markdown("---")
st.markdown("#### 고객 연령대 통계 (현재)")

try:
    top_rows = fetch_top_rows(
        int(start_year), int(end_year),
        age_group, sex,
        sort_key=sort_key, limit=15,
        min_prev_100k=min_prev_100k,
        min_cpp_chewon=min_cpp_chewon,
    )
except Exception as e:
    st.error(f"D1 통계 조회 실패: {e}")
    top_rows = []

table_height = calc_table_height(len(top_rows))

chart_title = f"Top15 질병 통계 ({start_year}~{end_year} · {age_band} · {sex_display} · 기준: {sort_label})"
chart_data_uri = build_top10_combo_chart_data_uri(
    top_rows, title=chart_title, basis=sort_key,
    start_year=int(start_year), end_year=int(end_year),
)

if chart_data_uri:
    st.image(base64.b64decode(chart_data_uri.split(",", 1)[1]))
else:
    st.warning("차트를 만들 데이터가 없습니다. 조건을 바꿔보세요.")

with st.expander("통계 상세 (Top15 테이블) - 현재 연령대",expanded=False):
    st.dataframe(
        [
            {
                "질병코드": r.get("disease_code"),
                "질병명": r.get("disease_name_ko") or r.get("disease_code"),
                "총진료비(연평균, 억원)": f"{chewon_to_eok((float(r.get('total_cost') or 0) / years)):,.1f}",
                "유병률(10만명당)": f"{float(r.get('prevalence_per_100k') or 0):,.1f}",
                "1인당 진료비(만원)": f"{chewon_to_man(r.get('cost_per_patient')):,.1f}",
            }
            for r in (top_rows or [])
        ],
        use_container_width=True,
        hide_index=True,
        height=table_height,
    )

# -------------------------
# 이후 연령대(미래 위험)
# -------------------------

after_groups = AFTER_AGE_GROUPS.get(age_band, [])
next_age_label = next_age_band_label(age_band)
st.markdown(f"#### {next_age_label} 연령대 통계 (미래 위험)")

if not after_groups:
    st.info("선택한 연령대 이후의 통계가 존재하지 않습니다.")
    after_rows = []
    after_chart_uri = None
else:
    try:
        after_rows = fetch_top_rows_after_age(
            int(start_year), int(end_year),
            after_groups, sex,
            sort_key=sort_key, limit=15,
            min_prev_100k=min_prev_100k,
            min_cpp_chewon=min_cpp_chewon,
        )
    except Exception as e:
        st.error(f"D1 이후 연령대 통계 조회 실패: {e}")
        after_rows = []

if after_groups and after_rows:
    after_title = f"이후 연령대 합산 통계 ({next_age_label} · {sex_display} · 기준: {sort_label})"
    after_chart_uri = build_top10_combo_chart_data_uri(
        after_rows, title=after_title, basis=sort_key,
        start_year=int(start_year), end_year=int(end_year),compact=True,   # ⭐ 이게 핵심
    )
    st.image(base64.b64decode(after_chart_uri.split(",", 1)[1]))

    with st.expander("통계 상세 (Top15 테이블) - 이후 연령대 합산",expanded=False):
        st.dataframe(
            [
                {
                    "질병코드": r.get("disease_code"),
                    "질병명": r.get("disease_name_ko") or r.get("disease_code"),
                    "총진료비(연평균, 억원)": f"{chewon_to_eok((float(r.get('total_cost') or 0) / years)):,.1f}",
                    "유병률(10만명당)": f"{float(r.get('prevalence_per_100k') or 0):,.1f}",
                    "1인당 진료비(만원)": f"{chewon_to_man(r.get('cost_per_patient')):,.1f}",
                }
                for r in (after_rows or [])
            ],
            use_container_width=True,
            hide_index=True,
            height=table_height,
        )
else:
    if after_groups:
        st.warning("이후 연령대 합산 조건에서 Top15 데이터가 없습니다. 조건을 완화해 보세요.")

# -------------------------
# 신규 부각 질병 (현재 Top15에 없음)
# -------------------------
emerging_rows = pick_emerging_rows(top_rows, after_rows, limit=5)
emerging_height = calc_table_height(len(emerging_rows))

if emerging_rows:
    st.markdown("#### 향후 새롭게 부각되는 질병 (현재 Top15에 없음)")
    with st.expander("신규 부각 질병 상세", expanded=True):
        st.dataframe(
            [
                {
                    "질병코드": r.get("disease_code"),
                    "질병명": r.get("disease_name_ko") or r.get("disease_code"),
                    "총진료비(연평균, 억원)": f"{chewon_to_eok((float(r.get('total_cost') or 0) / years)):,.1f}",
                    "유병률(10만명당)": f"{float(r.get('prevalence_per_100k') or 0):,.1f}",
                    "1인당 진료비(만원)": f"{chewon_to_man(r.get('cost_per_patient')):,.1f}",
                }
                for r in (emerging_rows or [])
            ],
            use_container_width=True,
            hide_index=True,
            height=emerging_height,
        )
else:
    st.info("현재 Top15에 없는 ‘신규 부각 질병’이 없습니다. (현재와 이후가 유사한 패턴)")

st.divider()

# =========================================================
# 문구 커스터마이징 + HTML/PDF 미리보기/출력
# =========================================================
# st.subheader("문구 조정(표준 문구를 커스터마이징 가능합니다.)")
# summary_lines = segment["summary_lines"][:]
# gap_questions = segment["gap_questions"][:]
# cta_text = segment["cta"]

# summary_lines[0] = st.text_input("요약 1", value=summary_lines[0])
# summary_lines[1] = st.text_input("요약 2", value=summary_lines[1])
# summary_lines[2] = st.text_input("요약 3", value=summary_lines[2])

# gap_questions[0] = st.text_input("점검 질문 1", value=gap_questions[0])
# gap_questions[1] = st.text_input("점검 질문 2", value=gap_questions[1])

# cta_text = st.text_area("CTA 문구", value=cta_text, height=90)

structure_rows = [
    {"area": "진단비", "reason": "진단 직후 초기 자금 여력(목돈) 점검"},
    {"area": "치료비", "reason": "치료 과정의 반복 비용·통원/수술 부담 점검"},
    {"area": "생활·소득", "reason": "치료로 인한 소득 공백·가계 영향 점검"},
]

# =========================================================
# Fixed Legal Footer (Compliance Approved)
# =========================================================

LEGAL_FOOTER = {
    "disclaimer": (
        "본 자료는 동일 연령·성별 집단의 통계 기반 참고 자료이며, "
        "개인별 진단·보장 수준은 상이할 수 있습니다. "
        "정확한 확인은 종합 보장분석을 통해 가능합니다."
    ),
    "legal_note": "본 자료는 편의를 위해 제공되며 법적 효력을 갖지 않습니다.",
}

logo_data_uri = file_to_data_uri(LOGO_PATH, "image/png")

today = date.today()
expire = today + timedelta(days=30)

context = {
    "css_path": str(CSS_PATH),
    "logo_data_uri": logo_data_uri,
    "brand_name": BRAND_NAME,
    "brand_subtitle": BRAND_SUBTITLE,
    "version": APP_VERSION,
    "after_section_title": f"{next_age_label} 연령대 통계 (미래 위험)",
    "customer": {
        "name": customer_name.strip() or "고객",
        "gender": gender,
        "age_band": age_band,
    },

    "planner": {
        "name": f"{fc['name']} FC",
        "phone": fc["phone"],
        "email": fc.get("email", None),
        "org": fc.get("org", "").strip(),
        "company": BRAND_NAME,
        "phone_display": planner_phone_display,
        "org_display": planner_org_display,
    },

    "segment": {
        "headline": segment["headline"].replace(
            "{customer_name}", (customer_name.strip() or "고객")
        ),
        # "summary_lines": summary_lines,
        # "gap_questions": gap_questions,
        # "cta": cta_text,
        "summary_lines": segment["summary_lines"],
        "gap_questions": segment["gap_questions"],
        "cta": segment["cta"],
    },

    # =========================
    # PAGE 1 통계
    # =========================
    "stats": {
        "base_year": f"{start_year}~{end_year}",
        # 🔹 상단 기준 문구 (헤더에 표시)
        "source": (
            "통계 - 건강보험심사평가원, "
            "대상질병 - 미래에셋생명 대표질병코드"
        ),

        "top7_basis": sort_label,
        "chart_data_uri": chart_data_uri,   # 현재 연령대 차트
        # 🔹 1페이지 하단 footer용 설명
        "footnotes": [
            "진료비 : 건강보험 요양급여비용 (비급여 및 그 외 부수비용(휴업 손해 등) 제외)",
            "총 진료비 : 해당 기간, 해당 연령대에 발생한 전체 진료비",
            f"본 통계는 최소 유병률(10만명당) {min_prev_100k}(명), "
            f"최소 1인당 진료비 {min_cpp_manwon}(만원)을 기준으로 산출하였습니다."
        ],
    },
    "current_table": render_table_html(top_rows),

    # =========================
    # PAGE 2 통계 (⭐ 핵심 추가)
    # =========================
    "after_chart_data_uri": after_chart_uri if after_rows else None,
    "after_table": render_table_html(after_rows) if after_rows else "",
    "emerging_table": render_emerging_table_html(emerging_rows),

    "structure_rows": structure_rows,

    "footer": LEGAL_FOOTER,
    "compliance_code": "심의번호 발행 예정",  # ⭐ 여기 추가
}

final_html = build_final_html_for_both(context)

st.subheader("미리보기")
components.html(final_html, height=900, scrolling=True)

st.divider()
st.subheader("심사요청 (자동) 및 PDF 출력")

# if st.button("심사요청"):
#     if not customer_name.strip():
#         st.warning("고객 성명을 입력해 주세요.")
#         st.stop()
    
#     # 1️⃣ 발행번호 생성 (D1 시퀀스)
#     compliance_code = generate_compliance_code(
#         service_name="보장점검",
#         version=APP_VERSION,
#     )

#     # 2️⃣ PDF 전용 context 복사 (⭐ 반드시 deepcopy)
#     pdf_context = copy.deepcopy(context)
#     pdf_context["customer"]["name"] = customer_name.strip()
#     pdf_context["segment"]["headline"] = segment["headline"].replace(
#         "{customer_name}", customer_name.strip()
#     )
#     pdf_context["compliance_code"] = (
#         f"{compliance_code} ({today:%Y.%m.%d}~{expire:%Y.%m.%d})"
#     )

#     # 3️⃣ PDF HTML 생성
#     pdf_html = build_final_html_for_both(pdf_context)

#     try:
#         # 4️⃣ PDF 생성
#         pdf_bytes = chromium_pdf_bytes(pdf_html)

#         # 5️⃣ 발행 기록
#         publish_report(
#             pdf_bytes=pdf_bytes,
#             compliance_code=compliance_code,
#             segments_version=APP_VERSION,
#             fc_id=fc["fc_code"],
#             fc_name=fc["name"],
#             customer_name=customer_name.strip(),
#             customer_gender=gender,
#             customer_age_band=age_band,
#             start_year=start_year,
#             end_year=end_year,
#             sort_key=sort_key,
#             min_prev_100k=min_prev_100k,
#             min_cpp_manwon=min_cpp_manwon,
#         )

#         # 2️⃣ 발행 이벤트 기록 (⭐ 여기!)
#         insert_report_event(
#             compliance_code=compliance_code,
#             event_type="issue",
#             actor_type="fc",
#             actor_id=fc["fc_code"],
#         )

#         st.success(f"✅ 발행 완료 · 심의번호: {compliance_code}")

#         st.download_button(
#             label="📄 심사완료된 PDF 다운로드",
#             data=pdf_bytes,
#             file_name=f"{compliance_code}.pdf",
#             mime="application/pdf",
#             on_click=lambda: insert_report_event(
#                 compliance_code=compliance_code,
#                 event_type="download",
#                 actor_type="fc",
#                 actor_id=fc["fc_code"],
#                 )
#             )

#     except Exception as e:
#         st.error(f"발행 중 오류 발생:\n{e}")


# =========================================================
# 발행 상태 관리 (자동 리셋 포함)
# =========================================================
def current_issue_fingerprint():
    return {
        "customer_name": customer_name.strip(),
        "gender": gender,
        "age_band": age_band,
        "start_year": start_year,
        "end_year": end_year,
        "sort_key": sort_key,
        "min_prev_100k": min_prev_100k,
        "min_cpp_manwon": min_cpp_manwon,
    }

# 세션 상태 초기화
for k, v in {
    "issuing": False,
    "issued": False,
    "downloaded": False,
    "issued_pdf_bytes": None,
    "issued_compliance_code": None,
    "last_issue_fingerprint": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ✅ 조건 변경 감지 → 자동 리셋
current_fp = current_issue_fingerprint()
if (
    st.session_state["issued"]
    and st.session_state["last_issue_fingerprint"] != current_fp
):
    st.session_state["issuing"] = False
    st.session_state["issued"] = False
    st.session_state["downloaded"] = False
    st.session_state["issued_pdf_bytes"] = None
    st.session_state["issued_compliance_code"] = None
    st.session_state["last_issue_fingerprint"] = None

st.subheader("심사요청 (자동) 및 PDF 출력")

btn_col, loading_col = st.columns([1, 3], vertical_alignment="center")

with btn_col:
    issue_clicked = st.button(
        "심사요청",
        use_container_width=True,
        disabled=(
            st.session_state["issuing"]
            or st.session_state["issued"]
        ),
    )

with loading_col:
    if st.session_state["issuing"]:
        st.markdown(
            """
            <div style="display:flex; align-items:center; gap:8px;">
                <div class="loader"></div>
                <span style="color:#666; font-size:0.95rem;">
                    PDF 생성 및 심사 요청 처리 중입니다…
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif st.session_state["issued"]:
        st.markdown(
            f"""
            <span style="color:#2e7d32; font-size:0.95rem;">
                ✅ 발행 완료 · 심의번호: <b>{st.session_state["issued_compliance_code"]}</b>
            </span>
            """,
            unsafe_allow_html=True,
        )

# 로딩 애니메이션 CSS
st.markdown(
    """
    <style>
    .loader {
        width: 18px;
        height: 18px;
        border: 3px solid #f3f3f3;
        border-top: 3px solid #F58220;
        border-radius: 50%;
        animation: spin 1s linear infinite;
    }
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================================================
# 심사요청 실행
# =========================================================
if issue_clicked:
    if not customer_name.strip():
        st.warning("고객 성명을 입력해 주세요.")
        st.stop()

    st.session_state["issuing"] = True

    try:
        # 1️⃣ 발행번호
        compliance_code = generate_compliance_code(
            service_name="보장점검",
            version=APP_VERSION,
        )

        # 2️⃣ PDF context
        pdf_context = copy.deepcopy(context)
        pdf_context["customer"]["name"] = customer_name.strip()
        pdf_context["segment"]["headline"] = segment["headline"].replace(
            "{customer_name}", customer_name.strip()
        )
        pdf_context["compliance_code"] = (
            f"{compliance_code} ({today:%Y.%m.%d}~{expire:%Y.%m.%d})"
        )

        # 3️⃣ PDF 생성
        pdf_html = build_final_html_for_both(pdf_context)
        pdf_bytes = chromium_pdf_bytes(pdf_html)

        # 4️⃣ 발행 기록
        publish_report(
            pdf_bytes=pdf_bytes,
            compliance_code=compliance_code,
            segments_version=APP_VERSION,
            fc_id=fc["fc_code"],
            fc_name=fc["name"],
            customer_name=customer_name.strip(),
            customer_gender=gender,
            customer_age_band=age_band,
            start_year=start_year,
            end_year=end_year,
            sort_key=sort_key,
            min_prev_100k=min_prev_100k,
            min_cpp_manwon=min_cpp_manwon,
        )

        insert_report_event(
            compliance_code=compliance_code,
            event_type="issue",
            actor_type="fc",
            actor_id=fc["fc_code"],
        )

        # 5️⃣ 상태 저장
        st.session_state["issued"] = True
        st.session_state["downloaded"] = False
        st.session_state["issued_pdf_bytes"] = pdf_bytes
        st.session_state["issued_compliance_code"] = compliance_code
        st.session_state["last_issue_fingerprint"] = current_fp

        st.success(f"✅ 발행 완료 · 심의번호: {compliance_code}")

    except Exception as e:
        st.error(f"발행 중 오류 발생:\n{e}")

    finally:
        st.session_state["issuing"] = False
        st.rerun()

# =========================================================
# PDF 다운로드
# =========================================================
if st.session_state["issued"]:
    st.download_button(
        label="📄 심사완료된 PDF 다운로드",
        data=st.session_state["issued_pdf_bytes"],
        file_name=f"{st.session_state['issued_compliance_code']}.pdf",
        mime="application/pdf",
        disabled=st.session_state["downloaded"],
        on_click=lambda: (
            insert_report_event(
                compliance_code=st.session_state["issued_compliance_code"],
                event_type="download",
                actor_type="fc",
                actor_id=fc["fc_code"],
            ),
            st.session_state.update({"downloaded": True})
        ),
    )
