import math
import re
import textwrap
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from typing import Dict, List, Tuple

import google.generativeai as genai

import gspread
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="배우 다차원 화제성 지표 - 드라마", layout="wide")

RAW_REQUIRED_COLUMNS = [
    "인물명",
    "프로그램명",
    "드라마화제성",
    "배우화제성",
    "랭크인주차",
    "랭크인배우수",
    "작품내랭킹",
]

DEFAULT_WEIGHTS = {"production": 0.4, "stability": 0.3, "contribution": 0.3}

DEFAULT_AXIS_THRESHOLDS = {
    "S": 0.98,
    "A++": 0.96,
    "A+": 0.90,
    "A": 0.85,
    "B++": 0.70,
    "B+": 0.50,
    "B": 0.30,
    "C++": 0.15,
    "C+": 0.05,
}

GRADE_ORDER = [
    "S", "A++", "A+", "A",
    "B++", "B+", "B",
    "C++", "C+", "C",
]

TIER_BASE_ORDER = ["S", "A", "B", "C"]

TIER_COLORS = {
    "S": "#7a3cff",
    "A": "#2456ff",
    "B": "#0d9a85",
    "C": "#ef6a5b",
}
GRADE_BG = {
    "S": "#7a3cff", "A++": "#1747d1", "A+": "#3f74ff", "A": "#9ab7ff",
    "B++": "#0b8f7c", "B+": "#24baa1", "B": "#b7efe3",
    "C++": "#ef7d1a", "C+": "#f45b49", "C": "#ffd0ca",
}

VISIBLE_COLUMNS = [
    "#", "배우", "성별", "연령대", "합산등급", "폭발력등급", "안정성등급", "기여도등급",
    "합산점수", "폭발백분율", "안정백분율", "기여백분율", "배우화제성", "출연작품수"
]

ACTOR_META_REQUIRED_COLUMNS = ["배우명", "남녀", "출생연도"]
AGE_GROUP_ORDER = ["20대", "30대", "40대", "50대"]
CURRENT_YEAR = 2026

ACTOR_COMBO_PROMPT_FILE = "actor_combo_prompt_updated.md"
DEFAULT_ACTOR_COMBO_PROMPT = r"""역할: 너는 드라마 캐스팅 전략과 마케팅 구조를 함께 해석하는 콘텐츠/편성 전략 분석가다.

목표: 입력된 배우들의 다차원 화제성 등급 정보를 바탕으로, 개별 배우의 단순 나열이 아니라 "배우 조합 구조"를 분석해 캐스팅 관점에서의 강점, 보완점, 시너지 구조를 실무형 코멘트로 정리한다.

출력 형식: 반드시 HTML만 출력한다. Markdown, 코드블록, 일반 본문 설명은 금지한다.

=== [입력 데이터 정의] ===
입력에는 아래 정보가 포함된다.
- 메인 배우 목록
- 서브 배우 목록
- 각 배우의 합산등급
- 폭발력등급
- 안정성등급
- 기여도등급
- 합산점수
- 생산/안정/기여 백분율
- 역할군 내 상대위치 정보(있을 경우)
- 필요 시 성별, 연령대, 출연작품수

각 항목의 의미는 아래와 같이 해석한다.
- 폭발력: 화제성 파급력과 절대 규모
- 안정성: 화제 흐름의 지속성과 변동성 완화 정도
- 기여도: 작품 내 존재감과 중심축 역할의 강도
- 합산등급: 전체 밸런스를 종합한 위치
- 역할군 내 상대위치: 같은 선택 그룹 안에서의 상대적 강약, 그리고 해당 역할군에 기대되는 일반적 수준 대비 위치

=== [매우 중요한 해석 원칙: 절대평가 + 상대평가 동시 적용] ===
1. 절대 등급만 보지 말고 반드시 역할군 내부의 상대적 위치를 함께 해석한다.
2. 특히 메인 배우는 일반적으로 상위 티어 중심으로 구성되는 경우가 많으므로, 같은 S 티어라도 A++와 A의의 차이를 분명히 읽어야 한다.
3. 예를 들어 메인 3인 중 2명이 A++이고 1명이 A라면, A 등급 배우를 "전체 기준으로 높다"에서 멈추지 말고 "메인 조합 내부에서는 상대적으로 낮은 축"으로 해석해야 한다.
4. 즉 어떤 배우가 절대적으로는 상위권이라도, 같은 조합 안에서 비교하면 견인력·안정성·기여도의 상대적 무게가 다를 수 있다는 점을 반영한다.
5. 서브 배우도 마찬가지로 절대 등급과 함께, 서브 조합 내에서 누가 중심 보완축인지, 누가 균형형인지, 누가 상대적으로 약한 축인지를 해석한다.
6. 다만 상대적으로 낮다고 해서 부정적으로 단정하지 말고, "조합 내 역할상 보완축", "전면 견인보다는 보조 시너지형"처럼 기능적으로 번역한다.
7. 전체 평가는 반드시 "절대 수준"과 "조합 내부 구조"를 함께 반영해 작성한다.

=== [핵심 해석 원칙] ===
1. 개별 배우 설명보다 조합의 구조를 먼저 본다.
2. 메인 배우는 작품의 전면 인지도, 초반 진입, 화제 견인 구조 중심으로 해석한다.
3. 서브 배우는 안정감, 보완성, 밸런스, 완성도 보강 측면에서 해석한다.
4. 기여도등급이 높지 않은 배우는 "단독 견인형"으로 과장하지 않는다.
5. 폭발력은 높지만 안정성 또는 기여도가 낮으면, 폭발력은 있으나 구조적 보완이 필요한 유형으로 해석한다.
6. 안정성은 높지만 폭발력이 낮으면, 화제의 상한 확대보다는 흐름 유지형으로 해석한다.
7. 서브 배우는 주연급 표현보다 조합 보완 표현을 우선 사용한다.
8. 단순히 등급을 반복하지 말고, 등급이 어떤 조합 효과를 만드는지 번역해서 설명한다.
9. "흥행 보장", "무조건 성공" 같은 단정 표현은 금지한다.
10. 전체 평가는 강점과 보완점을 함께 제시한다.
11. 조합 안에서 상대적으로 높은 배우와 낮은 배우가 함께 있으면, 누가 전면 견인축이고 누가 보완축인지 구조적으로 설명한다.
12. 같은 축이 과도하게 몰려 있으면 장점과 한계를 동시에 적는다. 예: 폭발력은 강하지만 기여도 중심축이 약한 조합.
13. 조합이 전반적으로 균형적이면 "폭발력"보다 "안정적 운용 가능성"에 방점을 둔다.
14. 일부 배우가 절대 수준은 높지만 해당 그룹 기대치 대비 약하면, "체급은 충분하지만 조합 내 상대강도는 다소 낮다"는 식으로 완곡하게 표현한다.

=== [문체 규칙] ===
1. 한국어로 작성한다.
2. 보고서/인사이트 메모 톤으로 쓴다.
3. 추상어만 쓰지 말고 반드시 이유를 붙인다.
4. 같은 표현을 반복하지 않는다.
5. 배우를 과도하게 칭찬하거나 깎아내리지 않는다.
6. 실무자가 바로 활용할 수 있는 표현으로 쓴다.
7. 출력 전체는 너무 길지 않게, 하지만 구조가 읽히도록 작성한다.
8. 문장은 명확하게 끊고, 한 문장에 평가 포인트를 과도하게 몰아넣지 않는다.

=== [출력 형식: HTML ONLY] ===
반드시 아래 구조의 HTML만 출력한다. class명은 유지한다. 불필요한 바깥 설명은 금지한다.

<div class="actor-combo-report">
<div class="combo-section">
<h3>[메인 n인]</h3>
<ul>
<li><strong>배우명</strong>: 역할 해석 1~2문장</li>
<li><strong>배우명</strong>: 역할 해석 1~2문장</li>
</ul>
<p class="section-summary">메인 조합 전체 요약 1~2문장</p>
</div>
<div class="combo-section">
<h3>[서브 n인]</h3>
<ul>
<li><strong>배우명 또는 배우명 묶음</strong>: 역할 해석 1~2문장</li>
<li><strong>배우명 또는 배우명 묶음</strong>: 역할 해석 1~2문장</li>
</ul>
<p class="section-summary">서브 조합 전체 요약 1~2문장</p>
</div>
<div class="combo-section total">
<h3>[종합 평가]</h3>
<ul>
<li>전체 조합의 강점</li>
<li>메인/서브 상호작용 구조</li>
<li>보완 필요 포인트</li>
</ul>
</div>
</div>

=== [출력 규칙 상세] ===
1. HTML 태그 외의 텍스트는 출력하지 않는다.
2. Markdown 문법 사용 금지.
3. 코드블록 사용 금지.
4. h3 제목은 정확히 [메인 n인], [서브 n인], [종합 평가] 형식을 따른다.
5. 메인/서브 배우 수는 실제 입력 수에 맞춰 n값을 바꾼다.
6. 종합 평가는 3~5개의 li로 정리한다.
7. 각 배우 설명은 이름 뒤에 콜론을 붙여 자연문으로 쓴다.
8. 필요 시 서브 배우는 개별 설명과 묶음 설명을 혼용할 수 있다.
9. 문장 안에서 등급명을 그대로 반복하지 말고 의미를 번역해라.
10. 단, 상대평가가 핵심인 경우에는 "메인 조합 내에서는 상대적으로", "서브 축 안에서는 비교적" 같은 표현을 사용해 구조를 설명해라.

=== [금지 사항] ===
- 등급명만 기계적으로 반복하는 문장
- 입력에 없는 작품명, 사건, 평판을 지어내는 내용
- 흥행 성공 여부를 단정하는 표현
- 모호한 미사여구 위주의 문장
- 불필요한 서론/결론
- "A가 B보다 무조건 낫다" 식의 단순 우열 문장
- 상대적으로 낮은 배우를 직접적으로 깎아내리는 표현
"""


def inject_css():
    st.markdown(
        """
        <style>
        .stApp {background: #f5f7fb;}
        
[data-testid="stHeader"] {display:none;}
        [data-testid="stToolbar"] {display:none;}
        [data-testid="stDecoration"] {display:none;}
        [data-testid="stSidebarNav"] {display:none;}
        .block-container {padding-top: 0.55rem; padding-bottom: 2rem; max-width: 1500px;}
        h1, h2, h3 {letter-spacing: -0.02em;}
        .page-title {font-size: 2.05rem; font-weight: 900; color:#1f2937; margin-bottom: 1.2rem;}
        .section-title {font-size: 1.35rem; font-weight: 900; color:#232b3a; margin: 0.2rem 0 0.95rem 0.15rem;}
        .spacer-lg {height: 24px;}
        .spacer-md {height: 14px;}
        .card {
            background: linear-gradient(180deg, #ffffff 0%, #fafcff 100%);
            border: 1px solid #e7ebf3;
            border-radius: 22px;
            padding: 20px 22px;
            box-shadow: 0 10px 28px rgba(31,41,55,0.05);
            height: 100%;
        }
        .metric-label {font-size: 0.94rem; color:#6b7280; font-weight:800; margin-bottom:10px;}
        .metric-value {font-size: 2rem; font-weight:900; color:#111827; line-height:1.1;}
        .metric-sub {font-size: 0.82rem; color:#7b8495; margin-top: 8px;}
        .mini-card {
            background:#fff; border:1px solid #e7ebf3; border-radius:18px; padding:14px 16px;
            box-shadow: 0 8px 22px rgba(31,41,55,0.04); min-height: 116px;
        }
        .tiny-card {
            background:#fff; border:1px solid #e7ebf3; border-radius:16px; padding:12px 14px;
            box-shadow: 0 6px 18px rgba(31,41,55,0.04); min-height: 88px;
        }
        .chip {display:inline-block; padding:5px 10px; border-radius:999px; font-size:0.78rem; font-weight:800;}
        .actor-name {font-size:1.05rem; font-weight:900; color:#111827; margin:8px 0 6px 0;}
        .actor-link {color:#111827; text-decoration:none;}
        .actor-link:hover {color:#1f64f0; text-decoration:underline;}
        .actor-sub {font-size:0.84rem; color:#6b7280; line-height:1.55;}
        .rep-card {
            background:#fff; border:1px solid #e7ebf3; border-radius:20px; padding:16px 18px;
            box-shadow: 0 8px 22px rgba(31,41,55,0.04); min-height: 190px;
        }
        .rep-title {font-size:1rem; font-weight:900; margin-bottom:12px; color:#222b3d;}
        .rep-line {font-size:0.92rem; color:#374151; line-height:1.95;}
        .work-card {
            background: linear-gradient(180deg, #ffffff 0%, #fafcff 100%);
            border:1px solid #e7ebf3; border-radius:20px; padding:18px 18px;
            box-shadow: 0 8px 22px rgba(31,41,55,0.04); min-height: 160px;
        }
        .work-title {font-size:1rem; font-weight:900; color:#111827; margin-bottom:12px;}
        .work-k {font-size:0.8rem; color:#778196; margin-bottom:4px;}
        .work-v {font-size:1.15rem; font-weight:900; color:#111827; margin-bottom:10px;}
        .subtle {color:#6b7280; font-size:0.9rem;}
        .select-hint {font-size:0.88rem; color:#6b7280; margin-top:-0.2rem; margin-bottom:0.9rem;}
        .summary-card {
            background: linear-gradient(180deg, #ffffff 0%, #fafcff 100%);
            border: 1px solid #e7ebf3;
            border-radius: 22px;
            padding: 18px 18px;
            box-shadow: 0 8px 22px rgba(31,41,55,0.04);
            min-height: 126px;
            height: 100%;
        }
        .summary-card.accent {
            border: 2px solid #2158d9;
            box-shadow: 0 10px 28px rgba(33,88,217,0.08);
        }
        .summary-title {
            font-size: 0.96rem;
            font-weight: 800;
            color: #667085;
            margin-bottom: 10px;
        }
        .summary-big {
            font-size: 2.1rem;
            line-height: 1.05;
            font-weight: 900;
            color: #10213a;
            letter-spacing: -0.04em;
        }
        .summary-mid {
            font-size: 0.98rem;
            font-weight: 800;
            color: #10213a;
            margin-top: 4px;
        }
        .summary-sub {
            font-size: 0.88rem;
            color: #7b8495;
            line-height: 1.65;
            margin-top: 10px;
        }

        .stTextInput > div > div, .stSelectbox > div > div, .stRadio > div, .stMultiSelect > div > div {
            border-radius: 14px !important;
        }
        .stDataFrame {border-radius: 18px; overflow: hidden;}
        
        section[data-testid="stSidebar"] {
            background:#f7f8fb;
            border-right:1px solid #e5e7eb;
            min-width: 280px !important;
            max-width: 280px !important;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-top:1.2rem;
            padding-left:0.5rem;
            padding-right:0.5rem;
        }
        div[data-testid="stSidebarUserContent"] .stRadio,
        div[data-testid="stSidebarUserContent"] .stRadio > div {
            width: 100%;
            gap: 0 !important;
        }
        div[data-testid="stSidebarUserContent"] .stRadio label[data-baseweb="radio"] {
            display:flex !important;
            width:100% !important;
            margin:0 !important;
            padding:0 !important;
            border-top:1px solid #d9dee8;
            cursor:pointer !important;
            min-height:auto !important;
        }
        div[data-testid="stSidebarUserContent"] .stRadio label[data-baseweb="radio"]:last-of-type {
            border-bottom:1px solid #d9dee8;
        }
        div[data-testid="stSidebarUserContent"] .stRadio label[data-baseweb="radio"] > div:first-child {
            display:none !important;
        }
        div[data-testid="stSidebarUserContent"] .stRadio label[data-baseweb="radio"] p {
            display:block !important;
            width:100% !important;
            margin:0 !important;
            padding:1rem 0.75rem !important;
            text-align:center !important;
            font-size:1.05rem !important;
            font-weight:800 !important;
            line-height:1.25 !important;
            color:#334155 !important;
            white-space:nowrap !important;
            word-break:keep-all !important;
            letter-spacing:-0.02em !important;
            box-sizing:border-box !important;
        }
        div[data-testid="stSidebarUserContent"] .stRadio label[data-baseweb="radio"]:has(input:checked) p {
            background:#1f64f0 !important;
            color:#ffffff !important;
        }
        .sidebar-footnote {color:#8b919c; font-size:0.9rem; margin-top:2rem;}
        
        .summary-card .actor-sub { line-height: 1.8; }
        .overview-section-box {
            background: linear-gradient(180deg, #ffffff 0%, #fafcff 100%);
            border: 1px solid #e7ebf3;
            border-radius: 24px;
            padding: 18px 20px 20px 20px;
            box-shadow: 0 10px 28px rgba(31,41,55,0.05);
            margin-top: 14px;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: linear-gradient(180deg, #ffffff 0%, #fafcff 100%);
            border: 1px solid #e7ebf3;
            border-radius: 24px;
            box-shadow: 0 10px 28px rgba(31,41,55,0.05);
            padding: 8px 10px;
        }
        .overview-section-title {
            font-size: 1.18rem;
            font-weight: 900;
            color:#1f2937;
            margin: 0 0 14px 0.15rem;
        }
        .overview-section-sub {
            font-size: 0.84rem;
            color:#7b8495;
            margin: -6px 0 14px 0.15rem;
        }
        .overview-parent-title {
            font-size: 1.22rem;
            font-weight: 950;
            color:#172033;
            margin: 0.1rem 0 0.25rem 0.1rem;
        }
        .overview-parent-sub {
            font-size: 0.88rem;
            color:#7b8495;
            margin: 0 0 1rem 0.1rem;
        }
        .overview-child-title {
            font-size: 1rem;
            font-weight: 900;
            color:#23314a;
            margin: 0.2rem 0 0.9rem 0.1rem;
            padding-top: 0.2rem;
        }
        .overview-line-section {
            padding: 0.4rem 0 1.1rem 0;
            border-top: 1px solid #dbe4f3;
        }
        .detail-line-section {
            padding: 0.35rem 0 1.15rem 0;
            border-top: 1px solid #dbe4f3;
            margin-top: 0.25rem;
        }
        .detail-line-section:first-of-type {
            margin-top: 0;
        }
        .detail-section-title {
            font-size: 1.18rem;
            font-weight: 900;
            color:#1f2937;
            margin: 0.35rem 0 0.95rem 0.1rem;
        }
        .formula-box {
            background:#f8fafc;
            border:1px solid #e2e8f0;
            border-radius:12px;
            padding:16px;
            margin:16px 0 10px 0;
            color:#334155;
            font-size:0.94rem;
            line-height:1.7;
            font-family:inherit;
        }

        .actor-combo-toolbar {margin: 0.2rem 0 1rem 0;}
        .actor-combo-toolbar .hint {font-size:0.88rem; color:#6b7280; margin-top:4px;}
        .actor-combo-box {
            background: linear-gradient(180deg, #ffffff 0%, #fafcff 100%);
            border: 1px solid #e7ebf3;
            border-radius: 22px;
            padding: 18px 18px;
            box-shadow: 0 8px 22px rgba(31,41,55,0.04);
        }
        .actor-combo-report {display:flex; flex-direction:column; gap:14px;}
        .actor-combo-report .combo-section {
            background:#fff; border:1px solid #e7ebf3; border-radius:18px; padding:16px 18px;
            box-shadow: 0 6px 18px rgba(31,41,55,0.04);
        }
        .actor-combo-report .combo-section.total {
            border: 1.5px solid #bfd1ff;
            box-shadow: 0 8px 22px rgba(36,86,255,0.08);
        }
        .actor-combo-report h3 {font-size:1.02rem; font-weight:900; color:#172033; margin:0 0 10px 0;}
        .actor-combo-report ul {margin:0; padding-left:1.2rem;}
        .actor-combo-report li {margin:0 0 10px 0; color:#374151; line-height:1.75;}
        .actor-combo-report .section-summary {
            margin: 10px 0 0 0; padding:12px 14px; border-radius:14px;
            background:#f7faff; color:#24324a; font-size:0.92rem; line-height:1.75; font-weight:700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_secret_section(name: str) -> Dict:
    try:
        return dict(st.secrets[name])
    except Exception:
        return {}


def get_gspread_client():
    sa = get_secret_section("gcp_service_account")
    if not sa:
        st.error("Secrets에 [gcp_service_account] 설정이 없습니다.")
        st.stop()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_info(sa, scopes=scopes)
    return gspread.authorize(creds)


def normalize_gender(value: str) -> str:
    s = str(value).strip()
    if s in ["남", "남자", "남성", "M", "Male", "male"]:
        return "남"
    if s in ["녀", "여", "여자", "여성", "F", "Female", "female"]:
        return "여"
    return "미상"


def derive_age_group(birth_year) -> str:
    if pd.isna(birth_year):
        return "미상"
    try:
        age = CURRENT_YEAR - int(birth_year) + 1
    except Exception:
        return "미상"
    if age < 20:
        return "20대 미만"
    if age < 30:
        return "20대"
    if age < 40:
        return "30대"
    if age < 50:
        return "40대"
    return "50대"


def sort_age_groups(values: List[str]) -> List[str]:
    order = {k: i for i, k in enumerate(AGE_GROUP_ORDER + ["20대 미만", "미상"])}
    return sorted(values, key=lambda x: (order.get(x, 999), str(x)))


@st.cache_data(ttl=600)
def load_actor_meta_from_gsheet() -> pd.DataFrame:
    data_cfg = get_secret_section("data")
    spreadsheet_id = data_cfg.get("spreadsheet_id", "").strip()
    actor_sheet = data_cfg.get("actor_list_sheet", "배우리스트").strip() or "배우리스트"
    if not spreadsheet_id:
        return pd.DataFrame(columns=["배우", "성별", "출생연도", "연령", "연령대"])

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(actor_sheet)
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return pd.DataFrame(columns=["배우", "성별", "출생연도", "연령", "연령대"])

    header = [(c or "").strip() or f"unnamed_{i+1}" for i, c in enumerate(values[0])]
    rows = []
    max_len = len(header)
    for row in values[1:]:
        row = list(row)
        if len(row) < max_len:
            row += [""] * (max_len - len(row))
        rows.append(row[:max_len])

    df = pd.DataFrame(rows, columns=header)
    missing = [c for c in ACTOR_META_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        st.warning(f"배우리스트 탭에 필요한 컬럼이 없습니다: {missing}")
        return pd.DataFrame(columns=["배우", "성별", "출생연도", "연령", "연령대"])

    meta = df[ACTOR_META_REQUIRED_COLUMNS].copy()
    meta["배우"] = meta["배우명"].astype(str).str.strip()
    meta["성별"] = meta["남녀"].apply(normalize_gender)
    meta["출생연도"] = pd.to_numeric(meta["출생연도"], errors="coerce")
    meta["연령"] = meta["출생연도"].apply(lambda x: CURRENT_YEAR - int(x) + 1 if pd.notna(x) else np.nan)
    meta["연령대"] = meta["출생연도"].apply(derive_age_group)
    meta = meta[meta["배우"].notna() & (meta["배우"] != "")].copy()
    meta = meta.drop_duplicates(subset=["배우"], keep="first")
    return meta[["배우", "성별", "출생연도", "연령", "연령대"]]




@st.cache_data(ttl=600)
def merge_actor_meta(result_df: pd.DataFrame, actor_meta_df: pd.DataFrame) -> pd.DataFrame:
    merged = result_df.copy()
    if actor_meta_df is None or actor_meta_df.empty:
        merged["성별"] = "미상"
        merged["출생연도"] = np.nan
        merged["연령"] = np.nan
        merged["연령대"] = "미상"
        return merged

    meta = actor_meta_df.copy()
    meta["배우"] = meta["배우"].astype(str).str.strip()
    merged["배우"] = merged["배우"].astype(str).str.strip()
    merged = merged.merge(meta, on="배우", how="left")
    merged["성별"] = merged["성별"].fillna("미상")
    merged["연령대"] = merged["연령대"].fillna("미상")
    return merged

@st.cache_data(ttl=600)
def load_raw_from_gsheet() -> pd.DataFrame:
    data_cfg = get_secret_section("data")
    spreadsheet_id = data_cfg.get("spreadsheet_id", "").strip()
    raw_sheet = data_cfg.get("raw_sheet", "RAW").strip() or "RAW"
    if not spreadsheet_id:
        st.error("Secrets의 [data].spreadsheet_id 값이 없습니다.")
        st.stop()

    gc = get_gspread_client()
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(raw_sheet)
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return pd.DataFrame(columns=RAW_REQUIRED_COLUMNS)

    header = [(c or "").strip() or f"unnamed_{i+1}" for i, c in enumerate(values[0])]
    rows = []
    max_len = len(header)
    for row in values[1:]:
        row = list(row)
        if len(row) < max_len:
            row += [""] * (max_len - len(row))
        rows.append(row[:max_len])

    df = pd.DataFrame(rows, columns=header)
    missing = [c for c in RAW_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        st.error(f"RAW 시트에 필요한 컬럼이 없습니다: {missing}")
        st.stop()


    # ===== 기간 데이터만 'source' 시트에서 별도로 가져오기 =====
    try:
        ws_source = sh.worksheet("source")
        source_values = ws_source.get_all_values()
        # 공백이 아닌 기간 데이터만 중복 없이 리스트로 추출
        period_values = list(set([str(row[0]).strip() for row in source_values[1:] if len(row) > 0 and str(row[0]).strip() != ""]))
    except Exception:
        period_values = []

    keep_cols = [c for c in [
        "인물명", "프로그램명", "드라마화제성", "배우화제성", "랭크인주차", "랭크인배우수", "작품내랭킹", "점유율"
    ] if c in df.columns]
    df = df[keep_cols].copy()
    df["__period_raw"] = "||".join(period_values)

    for col in ["인물명", "프로그램명"]:
        df[col] = df[col].astype(str).str.strip()

    for col in ["드라마화제성", "배우화제성", "랭크인주차", "랭크인배우수", "작품내랭킹", "점유율"]:
        if col in df.columns:
            df[col] = (
                df[col].astype(str).str.replace(",", "", regex=False).str.strip().replace({"": np.nan, "None": np.nan, "nan": np.nan})
            )
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df[df["인물명"].notna() & (df["인물명"] != "")].copy()
    df = df[df["프로그램명"].notna() & (df["프로그램명"] != "")].copy()
    return df


def percentrank_inc_min(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").astype(float)
    if len(s) <= 1:
        return pd.Series(np.ones(len(s)), index=s.index)
    return (s.rank(method="min") - 1) / (len(s) - 1)


def axis_grade(p: float, thresholds: Dict[str, float]) -> str:
    if pd.isna(p):
        return ""
    for grade in GRADE_ORDER[:-1]:
        if p >= thresholds[grade]:
            return grade
    return "C"


def major_tier(grade: str) -> str:
    g = str(grade)
    if g == "S":
        return "S"
    if g.startswith("A"):
        return "A"
    if g.startswith("B"):
        return "B"
    return "C"


@st.cache_data(ttl=600)
def build_result_table(raw_df: pd.DataFrame) -> pd.DataFrame:
    weights = DEFAULT_WEIGHTS.copy()
    weights.update(get_secret_section("weights"))
    thresholds = DEFAULT_AXIS_THRESHOLDS.copy()

    df = raw_df.copy()
    df["r1"] = (df["작품내랭킹"] == 1).astype(float)
    df["r2"] = (df["작품내랭킹"] == 2).astype(float) * 0.5
    df["r3"] = (df["작품내랭킹"] == 3).astype(float) * 0.3

    if "주평균 드라마화제성" not in df.columns:
        df["주평균 드라마화제성"] = df["드라마화제성"] / df["랭크인주차"].replace(0, np.nan)
    if "주평균 배우화제성" not in df.columns:
        df["주평균 배우화제성"] = df["배우화제성"] / df["랭크인주차"].replace(0, np.nan)

    grouped = df.groupby("인물명", sort=False)
    result = pd.DataFrame(index=grouped.size().index)
    result["배우"] = result.index
    result["드라마화제성"] = grouped["드라마화제성"].sum()
    result["배우화제성"] = grouped["배우화제성"].sum()
    result["출연작품수"] = grouped.size().astype(float)
    result["랭크주차"] = grouped["랭크인주차"].sum()
    result["출연작"] = grouped["프로그램명"].apply(lambda s: ", ".join(pd.unique(s.dropna().astype(str))))

    result["대표작 성과"] = grouped["주평균 배우화제성"].max()
    result["최하작 성과"] = grouped["주평균 배우화제성"].min()
    result["주평균 배우화제성합"] = grouped["주평균 배우화제성"].sum()
    result["작품 주평균"] = result["주평균 배우화제성합"] / result["출연작품수"].replace(0, np.nan)
    result["최하제외평균"] = np.where(
        result["출연작품수"] <= 2,
        result["작품 주평균"],
        (result["주평균 배우화제성합"] - result["최하작 성과"]) / (result["출연작품수"] - 1),
    )
    result["대표작 성과백분위"] = percentrank_inc_min(result["대표작 성과"])
    result["최하제외평균백분위"] = percentrank_inc_min(result["최하제외평균"])
    result["폭발력지수"] = np.where(
        result["출연작품수"] == 1,
        0.5 * result["대표작 성과백분위"] * 0.75 + 0.5 * result["최하제외평균백분위"],
        0.5 * result["대표작 성과백분위"] + 0.5 * result["최하제외평균백분위"],
    )

    result["작품평균"] = result["배우화제성"] / result["출연작품수"].replace(0, np.nan)
    global_avg = result["작품평균"].mean()
    result["보정 작품평균"] = (result["배우화제성"] + 3 * global_avg) / (result["출연작품수"] + 3)
    result["히트 분산지수"] = 1 - grouped["배우화제성"].max() / result["배우화제성"].replace(0, np.nan)

    r_min, r_max = result["히트 분산지수"].min(), result["히트 분산지수"].max()
    if pd.isna(r_min) or pd.isna(r_max) or math.isclose(r_min, r_max):
        result["히트 분산정규화"] = 0.0
    else:
        result["히트 분산정규화"] = (result["히트 분산지수"] - r_min) / (r_max - r_min)

    result["화제성 기여도"] = result["배우화제성"] / result["드라마화제성"].replace(0, np.nan)
    result["1위배율"] = grouped["r1"].sum() / result["출연작품수"].replace(0, np.nan)
    result["2위배율"] = grouped["r2"].sum() / result["출연작품수"].replace(0, np.nan)
    result["3위배율"] = grouped["r3"].sum() / result["출연작품수"].replace(0, np.nan)

    p_min, p_max = result["보정 작품평균"].min(), result["보정 작품평균"].max()
    if pd.isna(p_min) or pd.isna(p_max) or math.isclose(p_min, p_max):
        p_norm = pd.Series(0.0, index=result.index)
    else:
        p_norm = (result["보정 작품평균"] - p_min) / (p_max - p_min)

    result["꾸준함지수"] = np.minimum(
        1.0,
        0.25 * p_norm
        + 0.55 * (0.7 * result["히트 분산정규화"] + 0.3 * (result["출연작품수"] / (result["출연작품수"] + 2)))
        + 0.2 * (result["대표작 성과백분위"] ** 3),
    )

    base_contribution = 0.5 * p_norm + 0.5 * (
        result["화제성 기여도"] * (result["1위배율"] + result["2위배율"] + result["3위배율"])
    )
    result["작품체급백분위"] = percentrank_inc_min(result["드라마화제성"])
    result["작품체급보정"] = 0.45 + 0.55 * result["작품체급백분위"]
    result["보정기여도"] = base_contribution * result["작품체급보정"]

    result["폭발백분율"] = percentrank_inc_min(result["폭발력지수"])
    result["안정백분율"] = percentrank_inc_min(result["꾸준함지수"])
    result["기여백분율"] = percentrank_inc_min(result["보정기여도"])

    result["폭발력등급"] = result["폭발백분율"].apply(lambda x: axis_grade(x, thresholds))
    result["안정성등급"] = result["안정백분율"].apply(lambda x: axis_grade(x, thresholds))
    result["기여도등급"] = result["기여백분율"].apply(lambda x: axis_grade(x, thresholds))

    explosion_weight = float(weights.get("explosion", weights.get("production", 0.4)))
    result["합산점수"] = 100 * (
        result["폭발백분율"] * explosion_weight
        + result["안정백분율"] * float(weights.get("stability", 0.3))
        + result["기여백분율"] * float(weights.get("contribution", 0.3))
    )
    result["합산백분율"] = result["합산점수"] / 100
    result["합산등급"] = result["합산백분율"].apply(lambda x: axis_grade(x, thresholds))
    result["대분류티어"] = result["합산등급"].apply(major_tier)

    for col, pct_col in [("폭발", "폭발백분율"), ("안정", "안정백분율"), ("기여", "기여백분율")]:
        result[f"{col}_대분류내점수"] = (
            result.groupby("대분류티어")[pct_col]
            .transform(lambda s: percentrank_inc_min(s) * 100)
        )
        result[f"{col}_전체점수"] = result[pct_col] * 100

    result = result.sort_values(["합산점수", "배우화제성"], ascending=[False, False]).reset_index(drop=True)
    result.insert(0, "#", np.arange(1, len(result) + 1))
    return result


def format_percent_0(x: float) -> str:
    return "" if pd.isna(x) else f"{x * 100:.0f}%"


def format_int(x: float) -> str:
    return "" if pd.isna(x) else f"{x:,.0f}"


def format_score(x: float) -> str:
    return "" if pd.isna(x) else f"{x:,.2f}"


def metric_card(label: str, value: str, sub: str = ""):
    st.markdown(
        f"""
        <div class='card'>
            <div class='metric-label'>{label}</div>
            <div class='metric-value'>{value}</div>
            <div class='metric-sub'>{sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def actor_detail_href(actor_name: str) -> str:
    return f"?page={quote('배우 상세보기')}&actor={quote(str(actor_name))}"


def actor_link_html(actor_name: str, font_size: str = None, extra_style: str = "") -> str:
    style = ""
    if font_size:
        style += f"font-size:{font_size};"
    if extra_style:
        style += extra_style
    return f"<a class='actor-link actor-name' href='{actor_detail_href(actor_name)}' target='_self' style='{style}'>{actor_name}</a>"


def top10_card(rank: int, name: str, tier: str, score: float):
    bg = GRADE_BG.get(tier, "#64748b")
    st.markdown(
        f"""
        <div class='tiny-card'>
            {chip_html(f"{rank}위 · {tier}", tier)}
            {actor_link_html(name, font_size='0.98rem', extra_style='margin-top:10px; display:block;')}
            <div class='actor-sub'>합산점수 {format_score(score)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def top3_card(rank: int, name: str, tier: str, score: float, subtitle: str = ""):
    rank_sizes = {1: "1.18rem", 2: "1.08rem", 3: "1.02rem"}
    st.markdown(
        f"""
        <div class='tiny-card' style='min-height:148px; padding:18px 18px;'>
            {chip_html(f"{rank}위 · {tier}", tier)}
            {actor_link_html(name, font_size=rank_sizes.get(rank, '1rem'), extra_style='margin-top:12px; display:block;')}
            <div class='actor-sub'>합산점수 {format_score(score)}</div>
            {f"<div class='actor-sub' style='margin-top:4px;'>{subtitle}</div>" if subtitle else ""}
        </div>
        """,
        unsafe_allow_html=True,
    )


def rank_list_card(rows: pd.DataFrame, start_rank: int = 4):
    if rows.empty:
        st.info("표시할 배우가 없습니다.")
        return
    lines = []
    for i, (_, r) in enumerate(rows.iterrows(), start=start_rank):
        lines.append(
            f"<div style='display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 0; border-bottom:1px solid #edf1f7;'>"
            f"<div style='min-width:0;'>"
            f"<div style='font-size:0.92rem; font-weight:900; color:#111827;'>{i}위 <a class='actor-link' href='{actor_detail_href(r['배우'])}' target='_self' style='font-weight:900; color:#111827; text-decoration:none;'>{r['배우']}</a></div>"
            f"<div style='font-size:0.8rem; color:#6b7280; margin-top:3px;'>{r['합산등급']}</div>"
            f"</div>"
            f"<div style='font-size:0.92rem; font-weight:900; color:#111827; white-space:nowrap;'>{format_score(r['합산점수'])}</div>"
            f"</div>"
        )
    html = textwrap.dedent(f"""
    <div class='card' style='padding:14px 22px 12px 22px; min-height:470px;'>
        {''.join(lines)}
    </div>
    """).strip()
    st.markdown(html, unsafe_allow_html=True)


def build_overview_demo_figures(result_df: pd.DataFrame):
    heat_df = result_df.copy()
    heat_df = heat_df[heat_df["성별"].isin(["남", "여"]) & heat_df["연령대"].isin(AGE_GROUP_ORDER)].copy()

    x_labels = [f"남{age.replace('대', '')}" for age in AGE_GROUP_ORDER] + [f"여{age.replace('대', '')}" for age in AGE_GROUP_ORDER]
    y_labels = GRADE_ORDER

    if heat_df.empty:
        z = np.zeros((len(y_labels), len(x_labels)))
    else:
        heat_df["성연령"] = heat_df["성별"] + heat_df["연령대"].str.replace("대", "", regex=False)
        pivot = pd.crosstab(heat_df["합산등급"], heat_df["성연령"], normalize="index") * 100
        pivot = pivot.reindex(index=y_labels, columns=x_labels, fill_value=0)
        z = pivot.values

    text_vals = [[f"{v:.0f}%" if v > 0 else "-" for v in row] for row in z]

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=x_labels,
        y=y_labels,
        text=text_vals,
        texttemplate="%{text}",
        textfont={"size": 11},
        colorscale=[
            [0.0, "#f8fbff"],
            [0.2, "#dbe8ff"],
            [0.45, "#9ab7ff"],
            [0.7, "#4f7cff"],
            [1.0, "#1f4de3"],
        ],
        zmin=0,
        zmax=max(10, float(np.nanmax(z)) if np.size(z) else 10),
        colorbar=dict(title="등급 내 비중(%)", thickness=12, len=0.78),
        hovertemplate="등급 %{y}<br>%{x}<br>해당 등급 내 비중 %{z:.1f}%<extra></extra>",
        xgap=4,
        ygap=4,
    ))
    fig.update_layout(
        title="등급 내 성·연령 구성 비중",
        height=420,
        margin=dict(l=20, r=20, t=58, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_title="",
        yaxis_title="",
    )
    fig.update_xaxes(side="top", tickfont=dict(size=11), showgrid=False)
    fig.update_yaxes(autorange="reversed", tickfont=dict(size=11), showgrid=False)
    return fig


def render_highlight_rank_section(title: str, sub_df: pd.DataFrame, subtitle_builder=None, compact=False):
    st.markdown("<div class='spacer-md'></div>", unsafe_allow_html=True)
    if title:
        st.markdown(f"<div class='section-title'>{title}</div>", unsafe_allow_html=True)
    rows = sub_df.sort_values(['합산점수', '배우화제성'], ascending=[False, False]).head(10).reset_index(drop=True)
    if rows.empty:
        st.info('표시할 배우가 없습니다.')
        return

    top_rows = rows.head(3)
    rest_rows = rows.iloc[3:]

    c1, c2 = st.columns([1.1, 1.9] if compact else [1.05, 2.15])

    with c1:
        for i, (_, r) in enumerate(top_rows.iterrows(), start=1):
            subtitle = subtitle_builder(r) if subtitle_builder else ''
            top3_card(i, r['배우'], r['합산등급'], r['합산점수'], subtitle=subtitle)
            if i < len(top_rows):
                st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

    with c2:
        rank_list_card(rest_rows, start_rank=4)


def representative_card(title: str, badge: str, rows: pd.DataFrame):
    lines = "".join([
        f"<div class='rep-line'><b>{r['배우']}</b> ({format_score(r['합산점수'])})</div>" for _, r in rows.iterrows()
    ])
    st.markdown(
        f"""
        <div class='rep-card'>
            <div class='rep-title'>{title}</div>
            {chip_html(badge, badge)}
            <div style='height:10px;'></div>
            {lines if lines else "<div class='actor-sub'>해당 배우 없음</div>"}
        </div>
        """,
        unsafe_allow_html=True,
    )


def work_card(program: str, drama_score: float, actor_score: float):
    st.markdown(
        f"""
        <div class='work-card'>
            <div class='work-title'>{program}</div>
            <div class='work-k'>드라마화제성</div>
            <div class='work-v'>{format_int(drama_score)}</div>
            <div class='work-k'>배우화제성</div>
            <div class='work-v'>{format_int(actor_score)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )



def summary_grade_card(title: str, grade: str, accent: bool = False) -> str:
    bg = GRADE_BG.get(str(grade), "#64748b")
    fg = grade_text_color(str(grade))
    card_class = "summary-card accent" if accent else "summary-card"
    return f"""
    <div class='{card_class}'>
        <div class='summary-title'>{title}</div>
        <div class='summary-big' style='color:{bg};'>{grade}</div>
    </div>
    """


def actor_summary_card(row: pd.Series):
    c1, c2, c3, c4, c5 = st.columns([1.45, 1.1, 0.95, 0.95, 0.95])

    with c1:
        st.markdown(
            f"""
            <div class='summary-card'>
                <div class='summary-title'>배우 개요</div>
                <div class='summary-big' style='font-size:1.9rem;'><a class='actor-link' href='{actor_detail_href(row['배우'])}' target='_self' style='color:inherit; text-decoration:none;'>{row['배우']}</a></div>
                <div class='summary-sub'>
                    합산점수 <b>{format_score(row['합산점수'])}</b><br>
                    배우화제성 <b>{format_int(row['배우화제성'])}</b><br>
                    출연작품수 <b>{format_int(row['출연작품수'])}</b><br>
                    성별 <b>{row.get('성별', '미상')}</b> · 연령대 <b>{row.get('연령대', '미상')}</b>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c2:
        st.markdown(summary_grade_card("종합등급", row["합산등급"], accent=True), unsafe_allow_html=True)

    with c3:
        st.markdown(summary_grade_card("폭발력 등급", row["폭발력등급"]), unsafe_allow_html=True)

    with c4:
        st.markdown(summary_grade_card("안정성 등급", row["안정성등급"]), unsafe_allow_html=True)

    with c5:
        st.markdown(summary_grade_card("기여도 등급", row["기여도등급"]), unsafe_allow_html=True)


def build_actor_program_summary(raw_df: pd.DataFrame, actor_name: str) -> pd.DataFrame:
    actor_raw = raw_df[raw_df["인물명"] == actor_name].copy()
    if actor_raw.empty:
        return pd.DataFrame(columns=["프로그램명", "드라마화제성", "배우화제성"])
    agg = (
        actor_raw.groupby("프로그램명", as_index=False)[["드라마화제성", "배우화제성"]]
        .sum()
        .sort_values(["배우화제성", "드라마화제성"], ascending=[False, False])
    )
    return agg


def make_triangle_chart(values: List[float], title: str, line_color: str = "#356AE6", fill_color: str = "rgba(53,106,230,0.22)"):
    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=values + [values[0]],
            theta=["폭발력", "안정성", "기여도", "폭발력"],
            fill="toself",
            line=dict(color=line_color, width=3),
            fillcolor=fill_color,
            marker=dict(size=8, color=line_color),
            name=title,
        )
    )
    fig.update_layout(
        title=title,
        showlegend=False,
        height=360,
        margin=dict(l=20, r=20, t=55, b=20),
        polar=dict(
            radialaxis=dict(visible=True, range=[0, 100], tickfont=dict(size=10), gridcolor="#dbe4f3"),
            angularaxis=dict(tickfont=dict(size=13, color="#374151")),
            bgcolor="rgba(0,0,0,0)",
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig




def grade_text_color(grade: str) -> str:
    grade = str(grade)
    return "#374151" if grade.endswith("C") else "#ffffff"


def chip_html(label: str, grade: str) -> str:
    bg = GRADE_BG.get(str(grade), "#64748b")
    fg = grade_text_color(str(grade))
    return f"<span class='chip' style='background:{bg}; color:{fg};'>{label}</span>"


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = str(hex_color).strip().lstrip("#")
    if len(hex_color) != 6:
        return f"rgba(100,116,139,{alpha})"
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def build_overview_sunburst_figure(result_df: pd.DataFrame):
    df = result_df.copy()
    df = df[df["성별"].isin(["남", "여"]) & df["연령대"].isin(AGE_GROUP_ORDER)].copy()
    if df.empty:
        fig = go.Figure()
        fig.update_layout(height=520, margin=dict(l=10, r=10, t=10, b=10))
        return fig

    labels = ["전체"]
    parents = [""]
    values = [len(df)]
    ids = ["전체"]
    colors = ["#eef2f7"]
    custom = [("전체", "", "", len(df))]

    for grade in GRADE_ORDER:
        grade_df = df[df["합산등급"] == grade]
        if grade_df.empty:
            continue
        grade_id = f"grade::{grade}"
        grade_color = GRADE_BG.get(grade, "#64748b")
        labels.append(grade)
        parents.append("전체")
        values.append(len(grade_df))
        ids.append(grade_id)
        colors.append(grade_color)
        custom.append((grade, "", "", len(grade_df)))

        for gender in ["남", "여"]:
            gender_df = grade_df[grade_df["성별"] == gender]
            if gender_df.empty:
                continue
            gender_id = f"{grade_id}::{gender}"
            labels.append(gender)
            parents.append(grade_id)
            values.append(len(gender_df))
            ids.append(gender_id)
            colors.append(hex_to_rgba(grade_color, 0.82))
            custom.append((grade, gender, "", len(gender_df)))

            for age in AGE_GROUP_ORDER:
                age_df = gender_df[gender_df["연령대"] == age]
                if age_df.empty:
                    continue
                age_id = f"{gender_id}::{age}"
                labels.append(age)
                parents.append(gender_id)
                values.append(len(age_df))
                ids.append(age_id)
                colors.append(hex_to_rgba(grade_color, 0.58))
                custom.append((grade, gender, age, len(age_df)))

    customdata = np.array(custom, dtype=object)
    fig = go.Figure(go.Sunburst(
        ids=ids,
        labels=labels,
        parents=parents,
        values=values,
        branchvalues="total",
        marker=dict(colors=colors, line=dict(color="white", width=2)),
        insidetextorientation="radial",
        maxdepth=3,
        sort=False,
        customdata=customdata,
        hovertemplate=(
            "<b>%{label}</b><br>"
            "등급: %{customdata[0]}<br>"
            "성별: %{customdata[1]}<br>"
            "연령대: %{customdata[2]}<br>"
            "배우 수: %{value}명<br>"
            "전체 비중: %{percentRoot:.1%}<extra></extra>"
        ),
        textinfo="label+percent root",
    ))
    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        uniformtext=dict(minsize=10, mode="hide"),
    )
    return fig


def build_overview_sunburst_insights(result_df: pd.DataFrame) -> List[Dict[str, str]]:
    df = result_df.copy()
    df = df[df["성별"].isin(["남", "여"]) & df["연령대"].isin(AGE_GROUP_ORDER)].copy()
    if df.empty:
        return []

    total_n = len(df)
    group_counts = df.groupby(["성별", "연령대"]).size().reset_index(name="count")
    top_total = group_counts.sort_values(["count", "성별", "연령대"], ascending=[False, True, True]).iloc[0]

    s_df = df[df["합산등급"] == "S"]
    if s_df.empty:
        s_title = "S등급 핵심 집단"
        s_value = "데이터 없음"
        s_sub = "현재 S등급 배우가 없습니다."
    else:
        s_counts = s_df.groupby(["성별", "연령대"]).size().reset_index(name="count").sort_values("count", ascending=False)
        s_top = s_counts.iloc[0]
        s_title = "S등급 핵심 집단"
        s_value = f"{s_top['성별']}성 {s_top['연령대']}"
        s_sub = f"S등급 {len(s_df)}명 중 {int(s_top['count'])}명 ({s_top['count'] / len(s_df) * 100:.0f}%)"

    upper_df = df[df["합산등급"].apply(lambda x: str(x) == "S" or str(x).startswith("A"))]
    if upper_df.empty:
        upper_title = "A등급 이상 중심 집단"
        upper_value = "데이터 없음"
        upper_sub = "A등급 이상 배우가 없습니다."
    else:
        upper_counts = upper_df.groupby(["성별", "연령대"]).size().reset_index(name="count").sort_values("count", ascending=False)
        upper_top = upper_counts.iloc[0]
        upper_title = "A등급 이상 중심 집단"
        upper_value = f"{upper_top['성별']}성 {upper_top['연령대']}"
        upper_sub = f"A등급 이상 {len(upper_df)}명 중 {int(upper_top['count'])}명 ({upper_top['count'] / len(upper_df) * 100:.0f}%)"

    low_df = df[df["합산등급"].apply(lambda x: str(x).startswith("C") or str(x).startswith("B"))]
    if low_df.empty:
        low_title = "B·C등급 비중 상위 집단"
        low_value = "데이터 없음"
        low_sub = "B·C등급 배우가 없습니다."
    else:
        low_counts = low_df.groupby(["성별", "연령대"]).size().reset_index(name="count").sort_values("count", ascending=False)
        low_top = low_counts.iloc[0]
        low_title = "B·C등급 비중 상위 집단"
        low_value = f"{low_top['성별']}성 {low_top['연령대']}"
        low_sub = f"B·C등급 {len(low_df)}명 중 {int(low_top['count'])}명 ({low_top['count'] / len(low_df) * 100:.0f}%)"

    return [
        {
            "title": "전체 최다 구성 집단",
            "value": f"{top_total['성별']}성 {top_total['연령대']}",
            "sub": f"전체 {total_n}명 중 {int(top_total['count'])}명 ({top_total['count'] / total_n * 100:.0f}%)",
        },
        {"title": s_title, "value": s_value, "sub": s_sub},
        {"title": upper_title, "value": upper_value, "sub": upper_sub},
        {"title": low_title, "value": low_value, "sub": low_sub},
    ]


def overview_insight_card(title: str, value: str, sub: str) -> str:
    return f"""
    <div class='summary-card' style='min-height:132px; padding:16px 18px;'>
        <div class='summary-title' style='margin-bottom:8px;'>{title}</div>
        <div class='summary-big' style='font-size:1.4rem; line-height:1.18;'>{value}</div>
        <div class='summary-sub' style='margin-top:8px;'>{sub}</div>
    </div>
    """



def parse_week_label(label: str):
    s = str(label).strip()
    if not s:
        return None

    # 숫자만 뽑아 연/월/주를 찾고, 표시는 원문 그대로 사용
    nums = re.findall(r"\d+", s)
    if len(nums) >= 3:
        try:
            yy = int(nums[0])
            mm = int(nums[1])
            ww = int(nums[2])
            
            # 연도가 2자리(예: 26)로 들어오면 4자리(2026)로 변환, 4자리면 그대로 사용
            if yy < 100:
                yy += 2000
                
            # 정상적인 연/월/주 범위인지 체크 (2000~2100년 지원)
            if 2000 <= yy <= 2100 and 1 <= mm <= 12 and 1 <= ww <= 6:
                return (yy, mm, ww)
        except Exception:
            return None
    return None


def get_data_period_caption(raw_df: pd.DataFrame) -> str:
    if "__period_raw" not in raw_df.columns:
        return ""

    vals = raw_df["__period_raw"].dropna().astype(str).str.strip()
    vals = vals[vals != ""]
    if vals.empty:
        return ""

    # "||"로 묶어둔 전체 기간 데이터를 분리
    raw_str = vals.iloc[0]
    if "||" in raw_str:
        unique_vals = raw_str.split("||")
    else:
        unique_vals = pd.unique(vals).tolist()

    parsed = []
    for v in unique_vals:
        key = parse_week_label(v)
        if key is not None:
            parsed.append((key, v))

    if parsed:
        # 튜플 (연, 월, 주) 순으로 정렬하여 가장 빠른/늦은 날짜 추출
        parsed.sort(key=lambda x: x[0])
        return f"데이터 기준 기간 · {parsed[0][1]} ~ {parsed[-1][1]}"

    return f"데이터 기준 기간 · {unique_vals[0]} ~ {unique_vals[-1]}"


# ===== 참고사항 페이지 렌더링 =====

def detail_grade_group(grade: str) -> str:
    g = str(grade)
    if g == "S" or g.startswith("A"):
        return "S·A"
    if g.startswith("B"):
        return "B"
    return "C"


def add_detail_relative_scores(result_df: pd.DataFrame) -> pd.DataFrame:
    df = result_df.copy()
    df["상세등급군"] = df["합산등급"].apply(detail_grade_group)
    for col, pct_col in [("폭발", "폭발백분율"), ("안정", "안정백분율"), ("기여", "기여백분율")]:
        df[f"{col}_상세등급군점수"] = (
            df.groupby("상세등급군")[pct_col]
            .transform(lambda s: percentrank_inc_min(s) * 100)
        )
    return df


def make_axis_compare_chart(overall_values: List[float], relative_values: List[float], relative_label: str):
    categories = ["폭발력", "안정성", "기여도"]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=categories,
            y=overall_values,
            name="전체 기준",
            marker=dict(color="#356AE6"),
            text=[f"{v:.0f}" for v in overall_values],
            textposition="outside",
        )
    )
    fig.add_trace(
        go.Bar(
            x=categories,
            y=relative_values,
            name=relative_label,
            marker=dict(color="#7a3cff"),
            text=[f"{v:.0f}" for v in relative_values],
            textposition="outside",
        )
    )
    fig.update_layout(
        title="항목별 위치 비교",
        barmode="group",
        height=360,
        margin=dict(l=20, r=20, t=50, b=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(range=[0, 100], title="점수"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def render_reference():
    st.markdown("<div class='section-title'>참고사항</div>", unsafe_allow_html=True)

    st.markdown(
        """<div class='card'>
<div class='rep-title' style='font-size:1.1rem;'>1. 지표 구성 개요</div>
<div class='actor-sub' style='line-height: 1.75; color: #374151;'>
다차원 화제성 지표는 <b>FUNDEX 인물 화제성점수</b>를 기반으로 배우별 <b>폭발력</b>, <b>안정성</b>, <b>기여도</b>를 계산합니다.<br>
합산점수는 <span style='color:#2456ff; font-weight:900;'>폭발력 40% · 안정성 30% · 기여도 30%</span> 가중으로 산출합니다.
<hr style='border:none; border-top:1px solid #e5e7eb; margin:18px 0;'>
해당 지표는 배우 화제성을 단순 총량 순위로 보지 않고, 아래 세 축으로 나누어 다각도로 평가합니다.<br>
<ul style='margin-top:10px; padding-left:22px; color:#4b5563;'>
<li style='margin-bottom:4px;'>얼마나 크게 성과를 내는지 <b>(폭발력)</b></li>
<li style='margin-bottom:4px;'>그 성과가 얼마나 꾸준한지 <b>(안정성)</b></li>
<li style='margin-bottom:4px;'>작품 안에서 얼마나 중심적인 존재감을 보이는지 <b>(기여도)</b></li>
</ul>
</div>
</div>""",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='spacer-lg'></div>", unsafe_allow_html=True)
    st.markdown("<div class='rep-title' style='font-size:1.1rem; margin-left:0.15rem;'>2. 상세 지표 설명</div>", unsafe_allow_html=True)
    st.markdown("<div class='spacer-md'></div>", unsafe_allow_html=True)

    st.markdown(
        """<div class='summary-card' style='min-height:auto; padding:22px 26px;'>
<div class='summary-title' style='font-size:1.05rem; color:#111827; margin-bottom:12px;'>💡 폭발력</div>
<div class='actor-sub' style='line-height: 1.72;'>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>정의</span><br>
<b>한 배우가 만들어낼 수 있는 최고 수준의 화제 확장력과, 대표작 외 작품들까지 포함한 재현 가능성을 함께 반영한 지표</b><br><br>
폭발력은 단순히 <b>가장 잘된 작품 하나</b>만 보는 지표가 아닙니다. 최고 성과를 낸 대표작의 존재감은 반영하되, 나머지 작품에서도 일정 수준 이상의 성과를 만들 수 있는지를 함께 봅니다. 즉, <b>"한 번 크게 터진 배우"인지, "고점을 만들면서도 평균 체급이 받쳐주는 배우"인지</b>를 구분하려는 목적입니다.
<div class='formula-box'>
<b style='color:#0f172a; font-size:1rem;'>폭발력</b> = 폭발력지수의 전체 백분위<br><br>
<b style='color:#0f172a;'>폭발력지수</b> = 0.5 × 대표작성과백분위 + 0.5 × 최하제외평균백분위
</div>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>세부 항목</span>
<ul style='margin-top:6px; padding-left:22px; color:#475569; font-size:0.9rem; line-height:1.68;'>
<li><b>대표작성과백분위</b> : 해당 배우의 작품별 주평균 배우화제성 중 가장 높은 값을 전체 배우와 비교한 백분위입니다.</li>
<li><b>최하제외평균백분위</b> : 작품별 주평균 배우화제성에서 가장 낮은 작품을 제외하고 계산한 평균의 백분위입니다. 단발성 저점의 영향은 줄이고, 전반적 상단 체급은 살리기 위한 장치입니다.</li>
<li><b>출연작품수 1개 보정</b> : 작품이 1개뿐인 배우는 대표작 한 편만으로 과대평가될 수 있어, 대표작성과 항목에 0.75 보정을 적용합니다.</li>
<li><b>해석 포인트</b> : 대표작만 압도적인 배우는 고점은 높게 나오더라도 평균 재현력이 약하면 점수가 제한됩니다. 반대로 대표작 고점과 복수 작품 평균이 함께 높은 배우는 상위 폭발력으로 평가됩니다.</li>
</ul>
</div>
</div>""",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='spacer-md'></div>", unsafe_allow_html=True)
    st.markdown(
        """<div class='summary-card' style='min-height:auto; padding:22px 26px;'>
<div class='summary-title' style='font-size:1.05rem; color:#111827; margin-bottom:12px;'>⚖️ 안정성</div>
<div class='actor-sub' style='line-height: 1.72;'>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>정의</span><br>
<b>여러 작품에서 얼마나 꾸준히 성과를 냈는지</b> (보정 작품평균, 히트 분산 보정, 작품수 보정, 대표작 성과를 함께 반영)
<div class='formula-box'>
<b style='color:#0f172a; font-size:1rem;'>안정성</b> = 꾸준함지수의 전체 백분위<br><br>
<b style='color:#0f172a;'>꾸준함지수</b> = MIN(1,<br>
&nbsp;&nbsp;0.25 × 보정작품평균정규화<br>
&nbsp;&nbsp;+ 0.55 × (0.7 × 히트분산정규화 + 0.3 × 작품수보정)<br>
&nbsp;&nbsp;+ 0.20 × (대표작성과백분위³)<br>
)
</div>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>세부 항목</span>
<ul style='margin-top:6px; padding-left:22px; color:#475569; font-size:0.9rem; line-height:1.68;'>
<li><b>보정작품평균정규화</b> = (보정작품평균 - 최소 보정작품평균) / (최대 보정작품평균 - 최소 보정작품평균)</li>
<li><b>보정작품평균</b> = (배우화제성 + 3 × 전체 작품평균 평균) / (출연작품수 + 3)</li>
<li><b>히트분산정규화</b> = (히트분산지수 - 최소 히트분산지수) / (최대 히트분산지수 - 최소 히트분산지수)</li>
<li><b>히트분산지수</b> = 1 - (대표작성과 / 배우화제성)</li>
<li><b>작품수보정</b> = 출연작품수 / (출연작품수 + 2)</li>
<li><b>대표작성과백분위</b> = 대표작성과의 전체 백분위</li>
</ul>
</div>
</div>""",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='spacer-md'></div>", unsafe_allow_html=True)
    st.markdown(
        """<div class='summary-card' style='min-height:auto; padding:22px 26px;'>
<div class='summary-title' style='font-size:1.05rem; color:#111827; margin-bottom:12px;'>🎯 기여도</div>
<div class='actor-sub' style='line-height: 1.72;'>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>정의</span><br>
<b>작품 전체 성과 안에서 얼마나 중심적인 존재감을 보였는지</b> (작은 작품의 과대평가를 막기 위해 작품 체급 보정 추가 적용)
<div class='formula-box'>
<b style='color:#0f172a; font-size:1rem;'>기여도</b> = 최종기여도의 전체 백분위<br><br>
<b style='color:#0f172a;'>최종기여도</b> = 보정기여도 × 작품체급보정<br><br>
<b style='color:#0f172a;'>보정기여도</b> =<br>
&nbsp;&nbsp;0.5 × 보정작품평균정규화<br>
&nbsp;&nbsp;+ 0.5 × (화제성기여도 × (1위배율 + 2위배율 + 3위배율))
</div>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>세부 항목</span>
<ul style='margin-top:6px; padding-left:22px; color:#475569; font-size:0.9rem; line-height:1.68;'>
<li><b>화제성기여도</b> = 배우화제성 / 드라마화제성</li>
<li><b>1위/2위/3위배율</b> = (해당 순위 횟수 / 출연작품수)에 각각 가중치(1, 0.5, 0.3) 적용</li>
<li><b>작품체급보정</b> = 0.45 + 0.55 × 작품체급백분위</li>
<li><b>작품체급백분위</b> = 드라마화제성의 전체 백분위</li>
</ul>
</div>
</div>""",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='spacer-lg'></div>", unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(
            """<div class='card'>
<div class='rep-title'>3. 최종 합산점수</div>
<div class='actor-sub' style='line-height: 1.72;'>
<span style='color:#6b7280; font-size:0.85rem; font-weight:700;'>산식</span>
<div class='formula-box'>
<b style='color:#1f2937;'>합산점수</b> = 100 × (<br>
&nbsp;&nbsp;0.4 × 폭발력<br>
&nbsp;&nbsp;+ 0.3 × 안정성<br>
&nbsp;&nbsp;+ 0.3 × 기여도<br>
)
</div>
</div>
</div>""",
            unsafe_allow_html=True,
        )

    with c2:
        st.markdown(
            """<div class='card'>
<div class='rep-title'>4. 등급 컷 기준</div>
<div class='actor-sub' style='display:flex; justify-content:space-between; line-height: 1.85;'>
<div>
<b>S</b> : 상위 98% 이상<br>
<b>A++</b> : 상위 96% 이상<br>
<b>A+</b> : 상위 90% 이상<br>
<b>A</b> : 상위 85% 이상<br>
<b>B++</b> : 상위 70% 이상
</div>
<div>
<b>B+</b> : 상위 50% 이상<br>
<b>B</b> : 상위 30% 이상<br>
<b>C++</b> : 상위 15% 이상<br>
<b>C+</b> : 상위 5% 이상<br>
<b>C</b> : 그 미만
</div>
</div>
</div>""",
            unsafe_allow_html=True,
        )


def table_styler(df: pd.DataFrame):
    show = df[VISIBLE_COLUMNS].copy()
    show = show.rename(columns={"합산등급": "합산등급"})
    show["합산점수"] = pd.to_numeric(show["합산점수"], errors="coerce").map(format_score)
    for c in ["폭발백분율", "안정백분율", "기여백분율"]:
        show[c] = pd.to_numeric(show[c], errors="coerce").map(format_percent_0)
    for c in ["배우화제성", "출연작품수"]:
        show[c] = pd.to_numeric(show[c], errors="coerce").map(format_int)
    show["#"] = show["#"].astype(int)

    def bg_color(val):
        grade = str(val)
        color = GRADE_BG.get(grade, "#ffffff")
        text_color = grade_text_color(grade)
        return f"background-color: {color}; color: {text_color}; font-weight: 800; border-radius: 8px;"

    styler = (
        show.style
        .map(bg_color, subset=["합산등급", "폭발력등급", "안정성등급", "기여도등급"])
        .set_properties(subset=["합산등급"], **{"font-weight": "900"})
        .set_table_styles([
            {"selector": "th", "props": [("background-color", "#f1f5fb"), ("color", "#374151"), ("font-weight", "800")]},
            {"selector": "td", "props": [("padding", "8px 10px")]},
        ])
    )
    return styler


def render_overview(raw_df: pd.DataFrame, result_df: pd.DataFrame):
    st.markdown("<div class='section-title'>OVERVIEW</div>", unsafe_allow_html=True)

    period_caption = get_data_period_caption(raw_df)
    if period_caption:
        st.caption(period_caption)

    total_actors = result_df["배우"].nunique()
    total_programs = raw_df["프로그램명"].nunique()
    top1 = result_df.iloc[0]

    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("전체 배우", format_int(total_actors), "분석 대상 배우 수")
    with c2:
        metric_card("전체 작품", format_int(total_programs), "RAW 기준 프로그램 수")
    with c3:
        metric_card("현재 1위 배우", top1["배우"], f"합산점수 {format_score(top1['합산점수'])}")

    st.markdown("<div class='spacer-md'></div>", unsafe_allow_html=True)
    heatmap_fig = build_overview_demo_figures(result_df)
    with st.container(border=True):
        st.markdown("<div class='overview-section-title'>등급별 성·연령 구성</div>", unsafe_allow_html=True)
        st.markdown("<div class='overview-section-sub'>각 등급 안에서 어떤 성·연령 집단 비중이 큰지 색상 진하기로 보여줍니다.</div>", unsafe_allow_html=True)
        st.plotly_chart(heatmap_fig, use_container_width=True)

    st.markdown("<div class='overview-line-section'>", unsafe_allow_html=True)
    st.markdown("<div class='overview-parent-title'>성별별 Top 10</div>", unsafe_allow_html=True)
    st.markdown("<div class='overview-parent-sub'>성별을 상위 카테고리로 두고, 각 그룹의 상위 배우를 보여줍니다.</div>", unsafe_allow_html=True)
    gender_left, gender_right = st.columns(2)
    with gender_left:
        st.markdown("<div class='overview-child-title'>남배우 Top 10</div>", unsafe_allow_html=True)
        render_highlight_rank_section(
            "",
            result_df[result_df["성별"] == "남"],
            subtitle_builder=lambda r: f"{r.get('연령대', '미상')} · 배우화제성 {format_int(r['배우화제성'])}",
        )
    with gender_right:
        st.markdown("<div class='overview-child-title'>여배우 Top 10</div>", unsafe_allow_html=True)
        render_highlight_rank_section(
            "",
            result_df[result_df["성별"] == "여"],
            subtitle_builder=lambda r: f"{r.get('연령대', '미상')} · 배우화제성 {format_int(r['배우화제성'])}",
        )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='overview-line-section'>", unsafe_allow_html=True)
    st.markdown("<div class='overview-parent-title'>연령대별 Top 10</div>", unsafe_allow_html=True)
    st.markdown("<div class='overview-parent-sub'>연령대를 상위 카테고리로 두고, 각 그룹의 상위 배우를 보여줍니다.</div>", unsafe_allow_html=True)
    age_cols = st.columns(2)
    for i, age_group in enumerate(AGE_GROUP_ORDER):
        with age_cols[i % 2]:
            st.markdown(f"<div class='overview-child-title'>{age_group} Top 10</div>", unsafe_allow_html=True)
            render_highlight_rank_section(
                "",
                result_df[result_df["연령대"] == age_group],
                subtitle_builder=lambda r: f"{r.get('성별', '미상')} · 배우화제성 {format_int(r['배우화제성'])}",
                compact=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='spacer-lg'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>전체 배우 리스트</div>", unsafe_allow_html=True)
    st.dataframe(table_styler(result_df), use_container_width=True, hide_index=True, height=760)


def similar_grade_actors(result_df: pd.DataFrame, row: pd.Series, top_n: int = 4) -> pd.DataFrame:
    pool = result_df[
        (result_df["폭발력등급"] == row["폭발력등급"]) |
        (result_df["안정성등급"] == row["안정성등급"]) |
        (result_df["기여도등급"] == row["기여도등급"])
    ].copy()
    pool = pool[pool["배우"] != row["배우"]].copy()
    if row.get("성별", "미상") in ["남", "여"]:
        pool = pool[pool["성별"] == row["성별"]].copy()
    if pool.empty:
        return pool
    match_count = (
        (pool["폭발력등급"] == row["폭발력등급"]).astype(int)
        + (pool["안정성등급"] == row["안정성등급"]).astype(int)
        + (pool["기여도등급"] == row["기여도등급"]).astype(int)
    )
    pool["match_count"] = match_count
    pool["distance"] = np.sqrt(
        (pool["폭발백분율"] - row["폭발백분율"]) ** 2
        + (pool["안정백분율"] - row["안정백분율"]) ** 2
        + (pool["기여백분율"] - row["기여백분율"]) ** 2
    )
    return pool.sort_values(["match_count", "distance", "합산점수"], ascending=[False, True, False]).head(top_n)


def render_detail(raw_df: pd.DataFrame, result_df: pd.DataFrame):
    st.markdown("<div class='section-title'>배우 상세 보기</div>", unsafe_allow_html=True)
    st.markdown("<div class='select-hint'>배우명을 검색해서 원하는 배우를 선택해 주세요.</div>", unsafe_allow_html=True)
    names = result_df["배우"].tolist()
    query_actor = str(st.query_params.get("actor", names[0])) if names else ""
    default_index = names.index(query_actor) if query_actor in names else 0
    selected_actor = st.selectbox("배우 선택", names, index=default_index, placeholder="배우명을 검색해 선택")
    st.query_params["page"] = "배우 상세보기"
    st.query_params["actor"] = selected_actor
    row = result_df[result_df["배우"] == selected_actor].iloc[0]

    actor_summary_card(row)
    st.markdown("<div class='spacer-md'></div>", unsafe_allow_html=True)

    st.markdown("<div class='detail-line-section'>", unsafe_allow_html=True)
    st.markdown("<div class='detail-section-title'>그래프 영역</div>", unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    detail_label = f"동일 등급군 기준 · {row['상세등급군']}"
    with c1:
        fig_all = make_triangle_chart(
            [row["폭발_전체점수"], row["안정_전체점수"], row["기여_전체점수"]],
            "항목별 점수 · 전체 기준",
            line_color="#356AE6",
            fill_color="rgba(53,106,230,0.22)",
        )
        st.plotly_chart(fig_all, use_container_width=True)
    with c2:
        fig_group = make_triangle_chart(
            [row["폭발_상세등급군점수"], row["안정_상세등급군점수"], row["기여_상세등급군점수"]],
            f"항목별 점수 · {detail_label}",
            line_color="#7a3cff",
            fill_color="rgba(122,60,255,0.20)",
        )
        st.plotly_chart(fig_group, use_container_width=True)

    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='detail-line-section'>", unsafe_allow_html=True)
    st.markdown("<div class='detail-section-title'>대표출연작 영역</div>", unsafe_allow_html=True)
    actor_programs = build_actor_program_summary(raw_df, selected_actor).head(6)
    cols = st.columns(3)
    for idx, r in actor_programs.iterrows():
        with cols[idx % 3]:
            work_card(r["프로그램명"], r["드라마화제성"], r["배우화제성"])
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<div class='detail-line-section'>", unsafe_allow_html=True)
    st.markdown("<div class='detail-section-title'>유사등급 배우 영역</div>", unsafe_allow_html=True)
    sim = similar_grade_actors(result_df, row, 4)
    cols = st.columns(4)
    for i, (_, r) in enumerate(sim.iterrows()):
        with cols[i % 4]:
            st.markdown(
                f"""
                <div class='mini-card'>
                    <div>{actor_link_html(r['배우'], extra_style='display:block;')}</div>
                    <div class='actor-sub'>폭발 {r['폭발력등급']} · 안정 {r['안정성등급']} · 기여 {r['기여도등급']}</div>
                    <div class='actor-sub' style='margin-top:8px;'>합산등급 <b>{r['합산등급']}</b> · 점수 {format_score(r['합산점수'])}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)


def compare_table_rows(result_df: pd.DataFrame, names: List[str]) -> pd.DataFrame:
    cols = ["배우", "성별", "연령대", "합산등급", "폭발력등급", "안정성등급", "기여도등급", "합산점수", "배우화제성", "출연작품수"]
    df = result_df[result_df["배우"].isin(names)][cols].sort_values("합산점수", ascending=False).copy()
    return df.rename(columns={"합산등급": "합산등급"})


def render_actor_radar(result_df: pd.DataFrame, chart_names: List[str], title: str, dynamic_range: bool = False, relative_mode: bool = False):
    if not chart_names:
        st.info("조건에 맞는 배우가 없습니다.")
        return
    fig = go.Figure()
    all_values = []
    chart_df = result_df[result_df["배우"].isin(chart_names)].copy()
    if relative_mode and not chart_df.empty:
        for axis in ["폭발", "안정", "기여"]:
            chart_df[f"{axis}_비교점수"] = percentrank_inc_min(chart_df[f"{axis}백분율"]) * 100
    for name in chart_names:
        r = chart_df[chart_df["배우"] == name].iloc[0]
        if relative_mode:
            vals = [r["폭발_비교점수"], r["안정_비교점수"], r["기여_비교점수"], r["폭발_비교점수"]]
        else:
            vals = [r["폭발_전체점수"], r["안정_전체점수"], r["기여_전체점수"], r["폭발_전체점수"]]
        all_values.extend(vals[:-1])
        fig.add_trace(
            go.Scatterpolar(
                r=vals,
                theta=["폭발력", "안정성", "기여도", "폭발력"],
                mode="lines+markers",
                line=dict(width=3),
                marker=dict(size=8),
                name=name,
            )
        )
    if dynamic_range and all_values:
        rmin, rmax = min(all_values), max(all_values)
        pad = max(4, (rmax - rmin) * 0.30)
        low = max(0, math.floor((rmin - pad) / 5) * 5)
        high = min(100, math.ceil((rmax + pad) / 5) * 5)
        if high - low < 20:
            low = max(0, low - 10)
            high = min(100, high + 10)
    else:
        low, high = 0, 100
    fig.update_layout(
        title=title,
        height=430,
        margin=dict(l=20, r=20, t=50, b=20),
        polar=dict(radialaxis=dict(visible=True, range=[low, high])),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_compare(raw_df: pd.DataFrame, result_df: pd.DataFrame):
    st.markdown("<div class='section-title'>배우 모아보기</div>", unsafe_allow_html=True)
    tab_group, tab_pair = st.tabs(["그룹 모아보기", "배우 직접 선택 1:1 비교"])

    with tab_group:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            selected_gender = st.multiselect("성별", options=["남", "여", "미상"], placeholder="전체")
        with c2:
            selected_age_groups = st.multiselect(
                "연령대",
                options=sort_age_groups(result_df["연령대"].dropna().astype(str).unique().tolist()),
                placeholder="전체",
            )
        with c3:
            selected_total_grade = st.multiselect("합산등급", options=GRADE_ORDER, placeholder="전체")
        with c4:
            selected_programs = st.multiselect(
                "작품",
                options=sorted(raw_df["프로그램명"].dropna().astype(str).unique().tolist()),
                placeholder="전체",
            )

        c3, c4, c5 = st.columns(3)
        with c3:
            selected_prod_grade = st.multiselect("폭발력등급", options=GRADE_ORDER, placeholder="전체")
        with c4:
            selected_stab_grade = st.multiselect("안정성등급", options=GRADE_ORDER, placeholder="전체")
        with c5:
            selected_contrib_grade = st.multiselect("기여도등급", options=GRADE_ORDER, placeholder="전체")

        filtered = result_df.copy()
        if selected_programs:
            program_actor_names = raw_df[raw_df["프로그램명"].isin(selected_programs)]["인물명"].dropna().astype(str).unique().tolist()
            filtered = filtered[filtered["배우"].isin(program_actor_names)].copy()
        if selected_total_grade:
            filtered = filtered[filtered["합산등급"].isin(selected_total_grade)].copy()
        if selected_prod_grade:
            filtered = filtered[filtered["폭발력등급"].isin(selected_prod_grade)].copy()
        if selected_stab_grade:
            filtered = filtered[filtered["안정성등급"].isin(selected_stab_grade)].copy()
        if selected_contrib_grade:
            filtered = filtered[filtered["기여도등급"].isin(selected_contrib_grade)].copy()
        if selected_gender:
            filtered = filtered[filtered["성별"].isin(selected_gender)].copy()
        if selected_age_groups:
            filtered = filtered[filtered["연령대"].isin(selected_age_groups)].copy()

        filtered = filtered.sort_values(["합산점수", "배우화제성"], ascending=[False, False]).reset_index(drop=True)
        st.caption(f"조건 일치 배우 {len(filtered):,}명")
        if filtered.empty:
            st.info("조건에 맞는 배우가 없습니다.")
        else:
            st.dataframe(
                filtered[["배우", "성별", "연령대", "합산등급", "폭발력등급", "안정성등급", "기여도등급", "합산점수", "배우화제성", "출연작품수"]]
                .rename(columns={"합산등급": "합산등급"})
                .style.format({"합산점수": "{:.2f}", "배우화제성": "{:,.0f}", "출연작품수": "{:,.0f}"}),
                use_container_width=True,
                hide_index=True,
                height=620,
            )
            render_actor_radar(filtered, filtered["배우"].head(8).tolist(), "조건 일치 배우 비교 · 상대 위치 기준", dynamic_range=True, relative_mode=True)

    with tab_pair:
        names = result_df["배우"].tolist()
        left, right = st.columns(2)
        with left:
            actor1 = st.selectbox("배우 1", names, index=0, placeholder="배우명 검색", key="compare_actor_1")
        with right:
            default_idx = 1 if len(names) > 1 else 0
            actor2 = st.selectbox("배우 2", names, index=default_idx, placeholder="배우명 검색", key="compare_actor_2")

        selected_names = [a for a in [actor1, actor2] if a]
        comp_df = compare_table_rows(result_df, selected_names)
        st.dataframe(
            comp_df.style.format({"합산점수": "{:.2f}", "배우화제성": "{:,.0f}", "출연작품수": "{:,.0f}"}),
            use_container_width=True,
            hide_index=True,
        )
        render_actor_radar(result_df, comp_df["배우"].tolist(), "선택 배우 비교 · 항목별 점수", dynamic_range=True)


@lru_cache(maxsize=1)
def load_actor_combo_prompt() -> str:
    candidates = [
        Path(__file__).resolve().parent / ACTOR_COMBO_PROMPT_FILE,
        Path.cwd() / ACTOR_COMBO_PROMPT_FILE,
    ]
    for prompt_path in candidates:
        if prompt_path.is_file():
            try:
                txt = prompt_path.read_text(encoding="utf-8").strip()
                if txt:
                    return txt
            except Exception:
                pass
    return DEFAULT_ACTOR_COMBO_PROMPT.strip()


def get_gemini_keys() -> List[str]:
    keys = []

    try:
        if "chatbot" in st.secrets:
            chatbot_cfg = st.secrets["chatbot"]
            api_keys = chatbot_cfg.get("api_keys", {})
            gemini_keys = api_keys.get("gemini", [])
            keys.extend([str(k).strip() for k in gemini_keys if str(k).strip()])
    except Exception:
        pass

    try:
        keys.extend([str(k).strip() for k in st.secrets.get("GEMINI_API_KEYS", []) or [] if str(k).strip()])
    except Exception:
        pass

    dedup = []
    for key in keys:
        if key and key not in dedup:
            dedup.append(key)
    return dedup


def grade_rank_value(grade: str) -> int:
    try:
        return GRADE_ORDER.index(str(grade))
    except ValueError:
        return len(GRADE_ORDER)


def build_relative_position_lines(group_df: pd.DataFrame, label: str) -> List[str]:
    lines: List[str] = []
    if group_df.empty:
        return lines
    total_sorted = group_df.sort_values(["합산점수", "배우화제성"], ascending=[False, False]).reset_index(drop=True)
    if len(total_sorted) >= 2:
        top_name = total_sorted.iloc[0]["배우"]
        low_name = total_sorted.iloc[-1]["배우"]
        lines.append(f"- {label} 조합 내 합산점수 최고: {top_name}")
        if top_name != low_name:
            lines.append(f"- {label} 조합 내 합산점수 최저: {low_name}")

    for axis_label, grade_col, pct_col in [
        ("폭발력", "폭발력등급", "폭발백분율"),
        ("안정성", "안정성등급", "안정백분율"),
        ("기여도", "기여도등급", "기여백분율"),
    ]:
        axis_sorted = group_df.sort_values([pct_col, "합산점수"], ascending=[False, False]).reset_index(drop=True)
        if axis_sorted.empty:
            continue
        high_row = axis_sorted.iloc[0]
        low_row = axis_sorted.iloc[-1]
        lines.append(f"- {label} 조합 내 {axis_label} 상대상위: {high_row['배우']} ({high_row[grade_col]}, {high_row[pct_col] * 100:.1f}%)")
        if len(axis_sorted) >= 2 and high_row['배우'] != low_row['배우']:
            lines.append(f"- {label} 조합 내 {axis_label} 상대하위: {low_row['배우']} ({low_row[grade_col]}, {low_row[pct_col] * 100:.1f}%)")

    grade_counts = group_df["합산등급"].value_counts().reindex(GRADE_ORDER, fill_value=0)
    nonzero = [f"{grade} {int(cnt)}명" for grade, cnt in grade_counts.items() if cnt > 0]
    if nonzero:
        lines.append(f"- {label} 합산등급 분포: " + ", ".join(nonzero))
    return lines


def build_actor_work_summary(raw_df: pd.DataFrame, actor_name: str, top_n: int = 3) -> str:
    actor_df = raw_df[raw_df["인물명"].astype(str).str.strip() == str(actor_name).strip()].copy()
    if actor_df.empty:
        return "전작 정보 없음"

    work_df = (
        actor_df.groupby("프로그램명", as_index=False)
        .agg(
            배우화제성합=("배우화제성", "sum"),
            드라마화제성합=("드라마화제성", "sum"),
            랭크인주차합=("랭크인주차", "sum"),
            최고작품내랭킹=("작품내랭킹", "min"),
        )
        .sort_values(["배우화제성합", "랭크인주차합"], ascending=[False, False])
        .head(top_n)
    )
    items = []
    for _, row in work_df.iterrows():
        items.append(
            f"{row['프로그램명']}(배우화제성 {row['배우화제성합']:.0f}, 드라마화제성 {row['드라마화제성합']:.0f}, 랭크인주차 {row['랭크인주차합']:.0f}, 최고 작품내랭킹 {row['최고작품내랭킹']:.0f})"
        )
    return "; ".join(items) if items else "전작 정보 없음"


def build_group_context_lines(group_df: pd.DataFrame, label: str) -> List[str]:
    lines: List[str] = []
    if group_df.empty:
        return lines

    grade_counts = group_df["합산등급"].value_counts().reindex(GRADE_ORDER, fill_value=0)
    s_count = int((group_df["합산등급"] == "S").sum())
    a_count = int(group_df["합산등급"].astype(str).str.startswith("A").sum())
    b_count = int(group_df["합산등급"].astype(str).str.startswith("B").sum())
    c_count = int(group_df["합산등급"].astype(str).str.startswith("C").sum())
    lines.append(f"- {label} 체급 요약: S {s_count}명, A권 {a_count}명, B권 {b_count}명, C권 {c_count}명")

    for axis_label, pct_col in [("폭발력", "폭발백분율"), ("안정성", "안정백분율"), ("기여도", "기여백분율")]:
        lines.append(
            f"- {label} {axis_label} 평균백분율: {group_df[pct_col].mean() * 100:.1f}% | 최고 {group_df[pct_col].max() * 100:.1f}% | 최저 {group_df[pct_col].min() * 100:.1f}%"
        )

    nonzero = [f"{grade} {int(cnt)}명" for grade, cnt in grade_counts.items() if cnt > 0]
    if nonzero:
        lines.append(f"- {label} 합산등급 분포: " + ", ".join(nonzero))
    return lines


def build_actor_group_payload(raw_df: pd.DataFrame, group_df: pd.DataFrame, label: str, expectation_note: str) -> str:
    if group_df.empty:
        return ""

    ordered = group_df.sort_values(["합산점수", "배우화제성"], ascending=[False, False]).reset_index(drop=True)
    lines = [f"[{label}]", f"- 그룹 기대치: {expectation_note}"]
    group_size = len(ordered)

    for idx, row in ordered.iterrows():
        total_rank = idx + 1
        total_pos = "상위" if total_rank == 1 else ("중위" if total_rank < group_size else "하위")
        line = (
            f"- {row['배우']} | 성별 {row['성별']} | 연령대 {row['연령대']} | 합산등급 {row['합산등급']} | "
            f"폭발력등급 {row['폭발력등급']} | 안정성등급 {row['안정성등급']} | 기여도등급 {row['기여도등급']} | "
            f"합산점수 {row['합산점수']:.2f} | 폭발백분율 {row['폭발백분율'] * 100:.1f}% | "
            f"안정백분율 {row['안정백분율'] * 100:.1f}% | 기여백분율 {row['기여백분율'] * 100:.1f}% | "
            f"배우화제성 {row['배우화제성']:.0f} | 출연작품수 {row['출연작품수']:.0f} | "
            f"{label} 내부 합산위치 {total_rank}/{group_size} ({total_pos})"
        )
        lines.append(line)
        works = build_actor_work_summary(raw_df, row["배우"], top_n=3)
        lines.append(f"  · 전작요약: {works}")

    lines.append(f"[{label} 상대구조 참고]")
    lines.extend(build_relative_position_lines(ordered, label))
    lines.append(f"[{label} 종합지표 참고]")
    lines.extend(build_group_context_lines(ordered, label))
    return "\n".join(lines)


def build_actor_combo_payload(raw_df: pd.DataFrame, result_df: pd.DataFrame, main_names: List[str], sub_names: List[str]) -> Tuple[str, pd.DataFrame]:
    selected_names = [name for name in main_names + sub_names if name]
    selected_df = result_df[result_df["배우"].isin(selected_names)].copy()
    if selected_df.empty:
        return "", selected_df

    selected_df["역할구분"] = selected_df["배우"].apply(lambda x: "메인" if x in main_names else "서브")

    main_df = selected_df[selected_df["역할구분"] == "메인"].copy()
    sub_df = selected_df[selected_df["역할구분"] == "서브"].copy()

    sections = [
        "[분석 목적]",
        "- 선택된 배우 조합의 절대 수준, 조합 내부 상대 구조, 전작 기반 맥락을 함께 해석할 것.",
        "- 전체 기준 체급과 역할군 내부 상대위치를 동시에 고려할 것.",
        "- 전작 정보는 배우의 반복 패턴과 강점 방향을 참고하는 보조 정보로만 사용할 것.",
        "- 입력 데이터만으로 판단 가능한 범위 안에서 종합적으로 해석할 것.",
        f"- 총 선택 배우 수: {len(selected_df)}명",
        f"- 메인 배우 수: {len(main_df)}명",
        f"- 서브 배우 수: {len(sub_df)}명",
        "",
        build_actor_group_payload(raw_df, main_df, f"메인 {len(main_df)}인", "메인 배우는 대체로 상위 등급에서 구성되므로, 같은 A권 안에서도 A++와 A의 차이를 구분해 읽어야 함. 동시에 전작 흐름상 실제 폭발력 패턴이 반복되는지도 함께 볼 것."),
    ]
    if not sub_df.empty:
        sections.extend([
            "",
            build_actor_group_payload(raw_df, sub_df, f"서브 {len(sub_df)}인", "서브 배우는 전면 견인보다는 조합 보완 역할이 많으므로, 절대 순위보다 안정감·균형감·보완 기능을 함께 해석해야 함. 전작 흐름에서 반복적으로 드러난 보완 성격도 함께 참고할 것."),
        ])
    else:
        sections.extend([
            "",
            "[서브 배우 정보]",
            "- 이번 분석에는 서브 배우 선택이 없음.",
            "- 출력 시 서브 관련 섹션은 생성하지 말 것.",
        ])
    return "\n".join([s for s in sections if s is not None]).strip(), selected_df


def call_actor_combo_ai(system_instruction: str, user_payload: str) -> str:
    keys = get_gemini_keys()
    if not keys:
        return "<div class='actor-combo-box'>Gemini API Key가 설정되지 않았습니다.</div>"

    from google.generativeai.types import HarmBlockThreshold, HarmCategory
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    model_name = "gemini-3-flash-preview",

    try:
        chatbot_cfg = dict(st.secrets.get("chatbot", {})) if "chatbot" in st.secrets else {}
        model_name = str(chatbot_cfg.get("gemini_model") or model_name)
    except Exception:
        pass

    last_error = None
    for key in keys:
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel(
                model_name,
                generation_config={"temperature": 0.2, "max_output_tokens": 4096},
                system_instruction=system_instruction,
            )
            resp = model.generate_content(
                user_payload,
                request_options={"timeout": 180},
                safety_settings=safety_settings,
            )
            if getattr(resp, "text", None):
                return resp.text
            if c0 := (getattr(resp, "candidates", None) or [None])[0]:
                if p0 := (getattr(c0, "content", None) and getattr(c0.content, "parts", None) or [None])[0]:
                    if hasattr(p0, "text"):
                        return p0.text
            return "<div class='actor-combo-box'>AI 응답이 비어 있습니다.</div>"
        except Exception as e:
            last_error = e
            if "429" in str(e) or "quota" in str(e).lower():
                continue
    msg = str(last_error) if last_error else "알 수 없는 오류"
    return f"<div class='actor-combo-box'>AI 분석 중 오류가 발생했습니다.<br>{msg}</div>"


def render_actor_combo_ai(raw_df: pd.DataFrame, result_df: pd.DataFrame):
    st.markdown("<div class='section-title'>배우 조합 분석(AI)</div>", unsafe_allow_html=True)
    st.markdown(
        "<div class='actor-combo-toolbar'><div class='hint'>메인 배우와 서브 배우를 나누어 선택하면, 절대 등급과 조합 내부 상대구조를 함께 반영해 AI가 조합을 해석합니다.</div></div>",
        unsafe_allow_html=True,
    )

    actor_options = result_df.sort_values(["합산점수", "배우화제성"], ascending=[False, False])["배우"].tolist()
    default_main = actor_options[:2] if len(actor_options) >= 2 else actor_options[:1]

    left, right = st.columns(2)
    with left:
        main_names = st.multiselect(
            "메인 배우 선택",
            options=actor_options,
            default=default_main,
            placeholder="메인 배우를 선택하세요",
            key="actor_combo_main",
        )
    sub_options = [name for name in actor_options if name not in main_names]
    with right:
        sub_names = st.multiselect(
            "서브 배우 선택",
            options=sub_options,
            placeholder="서브 배우를 선택하세요",
            key="actor_combo_sub",
        )

    selected_names = main_names + sub_names
    if len(selected_names) != len(set(selected_names)):
        st.warning("동일 배우는 메인/서브에 중복 선택할 수 없습니다.")
        return

    selected_df = result_df[result_df["배우"].isin(selected_names)].copy()
    if not selected_df.empty:
        display_df = selected_df.copy()
        display_df["역할구분"] = display_df["배우"].apply(lambda x: "메인" if x in main_names else "서브")
        display_df = display_df.sort_values(["역할구분", "합산점수", "배우화제성"], ascending=[True, False, False])
        st.dataframe(
            display_df[["역할구분", "배우", "성별", "연령대", "합산등급", "폭발력등급", "안정성등급", "기여도등급", "합산점수", "배우화제성", "출연작품수"]]
            .rename(columns={"합산등급": "합산등급"})
            .style.format({"합산점수": "{:.2f}", "배우화제성": "{:,.0f}", "출연작품수": "{:,.0f}"}),
            use_container_width=True,
            hide_index=True,
        )

    if not main_names:
        st.info("메인 배우는 최소 1명 이상 선택해주세요.")
        return
    if len(selected_names) < 2:
        st.info("전체 배우를 2명 이상 선택하면 조합 분석을 실행할 수 있습니다.")
        return

    if st.button("AI 조합 분석 시작", type="primary", use_container_width=True):
        prompt = load_actor_combo_prompt()
        payload, payload_df = build_actor_combo_payload(raw_df, result_df, main_names, sub_names)
        if payload_df.empty:
            st.warning("선택 배우 데이터를 찾지 못했습니다.")
            return
        with st.spinner("배우 조합을 분석하는 중입니다..."):
            html = call_actor_combo_ai(prompt, payload)
        st.markdown(html, unsafe_allow_html=True)



def main():
    inject_css()
    st.markdown("<div class='page-title'>배우 다차원 화제성 지표 - 드라마</div>", unsafe_allow_html=True)

    raw_df = load_raw_from_gsheet()
    actor_meta_df = load_actor_meta_from_gsheet()
    result_df = build_result_table(raw_df)
    result_df = merge_actor_meta(result_df, actor_meta_df)
    result_df = add_detail_relative_scores(result_df)

    with st.sidebar:
        st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
        page_options = ["OVERVIEW", "배우 상세보기", "배우 모아보기", "배우 조합 분석(AI)", "참고사항"]
        query_page = str(st.query_params.get("page", "OVERVIEW"))
        default_page_index = page_options.index(query_page) if query_page in page_options else 0
        page = st.radio("", page_options, index=default_page_index, label_visibility="collapsed")
        st.query_params["page"] = page
        st.markdown("<div class='sidebar-footnote'>문의 : 미디어마케팅팀 데이터인사이트파트</div>", unsafe_allow_html=True)

    if page == "OVERVIEW":
        render_overview(raw_df, result_df)
    elif page == "배우 상세보기":
        render_detail(raw_df, result_df)
    elif page == "배우 모아보기":
        render_compare(raw_df, result_df)
    elif page == "배우 조합 분석(AI)":
        render_actor_combo_ai(raw_df, result_df)
    else:
        render_reference()


if __name__ == "__main__":
    main()
