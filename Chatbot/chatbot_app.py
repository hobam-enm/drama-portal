# region [Imports & Setup]
import streamlit as st
from io import BytesIO
from functools import lru_cache

import pandas as pd
import os
import re
import gc
import time
import json
import base64
import gzip
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4
import io
import threading

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.generativeai as genai
from google.generativeai import caching  
from streamlit.components.v1 import html as st_html

# Optional: browser storage bridge (used for login persistence without URL params)
try:
    from streamlit_js_eval import streamlit_js_eval  # pip: streamlit-js-eval
    _SJE_AVAILABLE = True
except Exception:
    streamlit_js_eval = None
    _SJE_AVAILABLE = False

import pymongo
from pymongo import MongoClient
import certifi

# 경로 및 GitHub 설정
BASE_DIR = "/tmp"
SESS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESS_DIR, exist_ok=True)

CHATBOT_CFG = st.secrets.get("chatbot", {}) or {}
CHATBOT_GITHUB_CFG = CHATBOT_CFG.get("github", {}) or {}
GITHUB_TOKEN = str(CHATBOT_GITHUB_CFG.get("token") or st.secrets.get("GITHUB_TOKEN", "") or "")
GITHUB_REPO = str(CHATBOT_GITHUB_CFG.get("repo") or st.secrets.get("GITHUB_REPO", "") or "")
GITHUB_BRANCH = str(CHATBOT_GITHUB_CFG.get("branch") or st.secrets.get("GITHUB_BRANCH", "main") or "main")

FIRST_TURN_PROMPT_FILE = "1차 질문 프롬프트.md"
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

from pathlib import Path

@lru_cache(maxsize=4)
def load_first_turn_system_prompt() -> str:
    # 1) 영어 파일명으로 바꿨으면 여기만 같이 바꿔
    # FIRST_TURN_PROMPT_FILE = "first_turn_prompt.md"  # <- (상수는 원래 위치에)

    repo_dir = Path(__file__).resolve().parent
    cand = [
        repo_dir / FIRST_TURN_PROMPT_FILE,
        Path.cwd() / FIRST_TURN_PROMPT_FILE,
    ]

    prompt_path = next((p for p in cand if p.is_file()), None)
    if prompt_path is None:
        st.error(f"❌ 프롬프트 파일을 못 찾음: {FIRST_TURN_PROMPT_FILE}")
        st.write("tried:", [str(p) for p in cand])
        st.write("cwd:", str(Path.cwd()))
        st.write("repo_dir:", str(repo_dir))
        st.stop()

    # 2) 여기서 open이 실패하는 “진짜 이유”를 그대로 출력 (redacted 회피)
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:
        st.error("❌ 프롬프트 파일 open 실패")
        st.write("path:", str(prompt_path))
        st.write("type:", type(e).__name__)
        st.write("error:", str(e))
        try:
            st.write("stat:", os.stat(prompt_path))
        except Exception as e2:
            st.write("stat_failed:", f"{type(e2).__name__}: {e2}")
        st.stop()

    if not txt:
        st.error(f"❌ 프롬프트 파일이 비어있음: {prompt_path}")
        st.stop()

    return txt


KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(tz=KST)

def to_iso_kst(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST).isoformat(timespec="seconds")

def kst_to_rfc3339_utc(dt_kst: datetime) -> str:
    if dt_kst.tzinfo is None:
        dt_kst = dt_kst.replace(tzinfo=KST)
    return dt_kst.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
# endregion


# region [Page Config & CSS]
st.set_page_config(
    page_title="유튜브 댓글분석: AI챗봇",
    layout="wide",
    initial_sidebar_state="expanded"
)

GLOBAL_CSS = r"""
<style>
  /* ===== App chrome ===== */
  header, footer, #MainMenu { visibility: hidden; }

  /* ===== Main padding ===== */
  .main .block-container{
    padding-top: 2rem;
    padding-bottom: 5rem;
    max-width: 1200px;
  }

  /* ===== Sidebar Layout Control ===== */
  [data-testid="stSidebar"]{
    background-color: #f9fafb;
    border-right: 1px solid #efefef;
  }
  [data-testid="stSidebarUserContent"] {
    padding: 1rem 0.8rem !important;
  }
  [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
    gap: 0rem !important;
  }
  [data-testid="stSidebar"] .element-container {
    margin-bottom: 0.5rem !important;
  }
  [data-testid="stSidebar"] [data-testid="column"] {
    padding: 0 !important;
  }

  /* Sidebar Titles */
  .ytcc-sb-title{
    font-family: 'Helvetica Neue', sans-serif;
    font-weight: 800;
    font-size: 1.25rem;
    margin-bottom: 0.8rem;
    background: linear-gradient(90deg, #4285F4, #DB4437, #F4B400, #0F9D58);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: -0.5px;
    white-space: nowrap; 
  }

  /* User Profile */
  .user-info-text {
    font-size: 0.85rem;
    font-weight: 700;
    color: #374151;
    white-space: nowrap;
  }
  .user-role-text {
    font-size: 0.75rem;
    color: #9ca3af;
    font-weight: 500;
    margin-left: 4px;
  }

  /* ===== Button Styling Strategy ===== */
  
  /* 1. Default (Secondary) Buttons: Save, Session List, etc. */
  div[data-testid="stButton"] button[kind="secondary"] {
    background-color: #f3f4f6 !important; 
    border: none !important;
    border-radius: 8px !important;
    color: #374151 !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    padding: 0.45rem 0.5rem !important;
    box-shadow: none !important;
    width: 100% !important;
    transition: all 0.15s ease !important;
    box-sizing: border-box !important;
    font-family: inherit !important;
    min-height: 2.15rem !important;
  }
  div[data-testid="stButton"] button[kind="secondary"]:hover {
    background-color: #e5e7eb !important; 
    color: #111827 !important;
  }
  div[data-testid="stButton"] button[kind="secondary"]:active {
    background-color: #d1d5db !important;
    box-shadow: none !important;
  }

  /* 2. Primary Button: New Analysis Start */
  div[data-testid="stButton"] button[kind="primary"] {
    background-color: #111827 !important; 
    border: 1px solid #1f2937 !important;
    border-radius: 8px !important;
    color: #ffffff !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    padding: 0.45rem 0.5rem !important;
    box-shadow: none !important;
    width: 100% !important;
    min-height: 2.15rem !important;
    transition: all 0.15s ease !important;
  }
  div[data-testid="stButton"] button[kind="primary"]:hover {
    background-color: #374151 !important; 
    color: #ffffff !important;
    border-color: #4b5563 !important;
  }
  div[data-testid="stButton"] button[kind="primary"]:active {
    background-color: #000000 !important;
  }

  /* Disabled State */
  button:disabled, .ytcc-cap-btn:disabled {
    background-color: #f9fafb !important;
    color: #e5e7eb !important;
    cursor: not-allowed !important;
    border-color: transparent !important;
  }

  /* ===== Session List Styling ===== */
  .session-list-container {
    margin-top: 5px !important;
    border-top: 1px solid #efefef;
    padding-top: 8px !important;
  }
  .session-header {
    font-size: 0.75rem;
    font-weight: 700;
    color: #9ca3af;
    margin-bottom: 4px !important;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  
  /* List Items */
  .sess-name div[data-testid="stButton"] button {
    background: transparent !important;
    text-align: left !important;
    padding: 0.2rem 0.3rem !important;
    color: #4b5563 !important;
    font-weight: 500 !important;
    box-shadow: none !important;
  }
  .sess-name div[data-testid="stButton"] button:hover {
    background: #f3f4f6 !important;
    color: #111827 !important;
  }
  
  /* More Menu (...) Button - Remove Border */
  .more-menu div[data-testid="stButton"] button {
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    color: #9ca3af !important;
    box-shadow: none !important;
    min-height: auto !important;
  }
  .more-menu div[data-testid="stButton"] button:hover {
    color: #4b5563 !important;
    background: transparent !important;
  }
  
  /* Login & Main Title */
  .ytcc-login-title, .ytcc-main-title {
    font-weight: 800;
    font-size: clamp(1.4rem, 2.2vw, 2.5rem); 
    background: linear-gradient(45deg, #4285F4, #9B72CB, #D96570, #F2A60C);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 0.5rem;
    white-space: nowrap !important;
  }
</style>
"""
st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
# endregion


# region [Constants & State Management]
_YT_FALLBACK, _GEM_FALLBACK = [], []
CHATBOT_API_KEYS = CHATBOT_CFG.get("api_keys", {}) or {}
YT_API_KEYS = list(CHATBOT_API_KEYS.get("youtube", st.secrets.get("YT_API_KEYS", [])) or _YT_FALLBACK)
GEMINI_API_KEYS = list(CHATBOT_API_KEYS.get("gemini", st.secrets.get("GEMINI_API_KEYS", [])) or _GEM_FALLBACK)
GEMINI_MODEL = str(CHATBOT_CFG.get("gemini_model") or "gemini-3-flash-preview")
GEMINI_TIMEOUT = int(CHATBOT_CFG.get("gemini_timeout") or 240)
GEMINI_MAX_TOKENS = int(CHATBOT_CFG.get("gemini_max_tokens") or 8192)
MAX_TOTAL_COMMENTS = int(CHATBOT_CFG.get("max_total_comments") or 120_000)
MAX_COMMENTS_PER_VID = int(CHATBOT_CFG.get("max_comments_per_vid") or 4_000)
CACHE_TTL_MINUTES = int(CHATBOT_CFG.get("cache_ttl_minutes") or 20)

# Gemini 동시 호출 제한
MAX_GEMINI_INFLIGHT = max(1, int(CHATBOT_CFG.get("max_gemini_inflight") or st.secrets.get("MAX_GEMINI_INFLIGHT", 3) or 3))
GEMINI_INFLIGHT_WAIT_SEC = int(CHATBOT_CFG.get("gemini_inflight_wait_sec") or st.secrets.get("GEMINI_INFLIGHT_WAIT_SEC", 120) or 120)

_GEMINI_SEM = threading.BoundedSemaphore(MAX_GEMINI_INFLIGHT)
_GEMINI_TLOCAL = threading.local()

class GeminiInflightSlot:
    def __init__(self, wait_sec: int = None):
        self.wait_sec = GEMINI_INFLIGHT_WAIT_SEC if wait_sec is None else int(wait_sec)
        self.acquired = False

    def __enter__(self):
        if getattr(_GEMINI_TLOCAL, "held", False):
            return self

        deadline = time.time() + max(0, self.wait_sec)
        while True:
            if _GEMINI_SEM.acquire(timeout=0.2):
                self.acquired = True
                _GEMINI_TLOCAL.held = True
                return self
            if time.time() >= deadline:
                raise TimeoutError("GEMINI_INFLIGHT_TIMEOUT")

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            _GEMINI_TLOCAL.held = False
            _GEMINI_SEM.release()
        return False


def ensure_state():
    defaults = {
        "chat": [],
        "last_schema": None,
        "last_csv": "",
        "last_df": None,
        "sample_text": "",
        "loaded_session_name": None,
        "own_ip_mode": False,
        "own_ip_toggle_prev": None,
        "current_cache": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _reset_chat_only(keep_auth: bool = True):
    auth_keys = {
        "auth_ok", "auth_user_id", "auth_role", "auth_display_name",
        "client_instance_id", "_auth_users_cache"
    }
    safe_flow_keys = {"session_to_load", "session_to_delete", 'session_to_rename'}
    keep = set()
    if keep_auth:
        keep |= auth_keys
    keep |= safe_flow_keys

    for k in list(st.session_state.keys()):
        if k in keep:
            continue
        del st.session_state[k]

    ensure_state()

ensure_state()
# endregion


# region [MongoDB Integration: Sync & Load]
# ==========================================

@st.cache_resource
def init_mongo():
    """몽고DB 클라이언트 연결"""
    try:
        if "mongo" not in st.secrets: return None
        uri = st.secrets["mongo"]["uri"]
        return MongoClient(uri, tlsCAFile=certifi.where())
    except Exception as e:
        print(f"MongoDB Init Error: {e}")
        return None

def _dt_to_utc_iso_string(dt: datetime) -> str:
    if not dt: return ""
    if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def get_total_pgc_count():
    """
    [NEW] DB에 저장된 전체 영상 개수만 빠르게 조회합니다. (메모리 부하 없음)
    """
    client = init_mongo()
    if not client: return 0
    try:
        db = client.get_database(str((st.secrets.get("mongo", {}) or {}).get("yt_dashboard_db_name") or "yt_dashboard"))
        col = db.get_collection(str((st.secrets.get("mongo", {}) or {}).get("yt_videos_coll") or "videos"))
        # count_documents({})는 데이터를 로드하지 않고 메타데이터만 확인하므로 매우 빠르고 가볍습니다.
        return col.count_documents({})
    except Exception:
        return 0

def search_pgc_data(keywords: list, start_dt: datetime, end_dt: datetime):
    """
    MongoDB에서 기간과 키워드에 매칭되는 영상 데이터만 검색하여 가져옵니다.
    """
    client = init_mongo()
    if not client: return []

    try:
        db = client.get_database(str((st.secrets.get("mongo", {}) or {}).get("yt_dashboard_db_name") or "yt_dashboard"))
        col = db.get_collection(str((st.secrets.get("mongo", {}) or {}).get("yt_videos_coll") or "videos"))
        
        date_query = {}
        if start_dt: date_query["$gte"] = _dt_to_utc_iso_string(start_dt)
        if end_dt: date_query["$lte"] = _dt_to_utc_iso_string(end_dt)
            
        keyword_conditions = []
        if keywords:
            for kw in keywords:
                if not kw.strip(): continue
                safe_kw = re.escape(re.sub(r"\s+", "", kw.strip()))
                keyword_conditions.append({"title": {"$regex": safe_kw, "$options": "i"}})
                keyword_conditions.append({"description": {"$regex": safe_kw, "$options": "i"}})
        
        final_query = {}
        if date_query: final_query["date"] = date_query
        if keyword_conditions: final_query["$or"] = keyword_conditions
            
        cursor = col.find(final_query, {"_id": 0, "id": 1, "title": 1, "date": 1})
        return list(cursor)

    except Exception as e:
        print(f"Search Error: {e}")
        return []
    
def log_search_history(user_query: str, schema: dict):
    """
    [NEW] 사용자의 검색 이력(누가, 무엇을, 언제)을 DB에 저장합니다.
    에러가 나더라도 분석 흐름을 방해하지 않도록 try-except 처리했습니다.
    """
    try:
        client = init_mongo()
        if not client: return
        
        db = client.get_database("yt_dashboard")
        col = db.get_collection("search_logs") # 'search_logs' 컬렉션 자동 생성됨
        
        user_id = st.session_state.get("auth_user_id") or "public"
        
        log_doc = {
            "user_id": user_id,
            "raw_query": user_query,              # 사용자가 입력한 원본 질문
            "keywords": schema.get("keywords", []), # AI가 추출한 핵심 키워드
            "range_start": schema.get("start_iso"),
            "range_end": schema.get("end_iso"),
            "timestamp": datetime.utcnow()        # 검색 시점 (UTC)
        }
        col.insert_one(log_doc)
    except Exception as e:
        print(f"Log Error: {e}")

# endregion


# region [PDF Export: current session -> PDF]
@lru_cache(maxsize=1)
def _pdf_font_name() -> str:
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ModuleNotFoundError:
        return "Helvetica"

    candidates = [
        ("NanumGothic", "./fonts/NanumGothic.ttf"),
        ("NanumGothic", "./NanumGothic.ttf"),
        ("NanumGothic", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        ("NanumGothicCoding", "/usr/share/fonts/truetype/nanum/NanumGothicCoding.ttf"),
        ("UnDotum", "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf"),
        ("UnBatang", "/usr/share/fonts/truetype/unfonts-core/UnBatang.ttf"),
        ("NotoSansCJKkr", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        ("NotoSansKR", "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf"),
    ]

    for name, fp in candidates:
        if os.path.exists(fp):
            try:
                if name not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(name, fp))
                return name
            except Exception:
                continue
    return "Helvetica"


def _strip_html_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.I)
    s = re.sub(r"</\s*p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<\s*li\s*>", "• ", s, flags=re.I)
    s = re.sub(r"</\s*li\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    try:
        import html as _html
        s = _html.unescape(s)
    except Exception as e:
        print(f"⚠️ [_qp_set] failed: {e}")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def build_session_pdf_bytes(session_title: str, user_label: str, chat: list) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import simpleSplit
        from reportlab.lib.colors import HexColor
    except ModuleNotFoundError:
        return b"" 

    font = _pdf_font_name()

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    margin_l, margin_r = 18 * 2.8346, 18 * 2.8346 
    margin_t, margin_b = 18 * 2.8346, 18 * 2.8346
    max_bubble_w = (w - margin_l - margin_r) * 0.78
    pad_x, pad_y = 10, 8
    line_h = 13

    y = h - margin_t

    def new_page():
        nonlocal y
        c.showPage()
        y = h - margin_t

    def draw_title():
        nonlocal y
        c.setFont(font, 16)
        c.drawString(margin_l, y, f"대화 기록: {session_title}")
        y -= 22
        c.setFont(font, 10)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        label = (user_label or "").strip()
        c.drawString(margin_l, y, f"사용자: {label}   ·   생성: {ts}")
        y -= 18
        y -= 8

    def draw_bubble(role: str, text: str):
        nonlocal y
        role = (role or "").lower()
        is_user = role == "user"

        fill = HexColor("#EAFBF2") if is_user else HexColor("#F3F4F6")
        stroke = HexColor("#CDEEDB") if is_user else HexColor("#E5E7EB")
        text_color = HexColor("#0F172A")

        t = _strip_html_to_text(text or "")
        if not t:
            t = " "

        c.setFont(font, 10.5)
        wrapped = []
        for para in t.split("\n"):
            if para.strip() == "":
                wrapped.append("")
                continue
            wrapped.extend(simpleSplit(para, font, 10.5, max_bubble_w - pad_x * 2))
        if not wrapped:
            wrapped = [" "]

        bubble_h = pad_y * 2 + line_h * len(wrapped) + 10  
        if y - bubble_h < margin_b:
            new_page()

        max_line_w = 0
        for ln in wrapped:
            try:
                max_line_w = max(max_line_w, c.stringWidth(ln, font, 10.5))
            except Exception:
                pass
        bubble_w = min(max_bubble_w, max(220, max_line_w + pad_x * 2)) 

        x = (w - margin_r - bubble_w) if is_user else margin_l

        c.setFillColor(HexColor("#64748B"))
        c.setFont(font, 9)
        who = "나" if is_user else "AI"
        c.drawString(x + pad_x, y, who)
        y -= 12

        c.setFillColor(fill)
        c.setStrokeColor(stroke)
        c.roundRect(x, y - (bubble_h - 12), bubble_w, bubble_h - 12, 10, fill=1, stroke=1)

        c.setFillColor(text_color)
        c.setFont(font, 10.5)
        tx = x + pad_x
        ty = y - pad_y - 2
        for ln in wrapped:
            c.drawString(tx, ty, ln)
            ty -= line_h

        y = y - (bubble_h - 12) - 12

    draw_title()

    for m in chat or []:
        draw_bubble(m.get("role"), m.get("content", ""))

    c.save()
    return buf.getvalue()


def _session_title_for_pdf() -> str:
    return st.session_state.get("loaded_session_name") or "현재대화"


def render_pdf_capture_button(label: str, pdf_filename_base: str) -> None:
    safe = re.sub(r'[^0-9A-Za-z가-힣 _\-\(\)\[\]]+', '', (pdf_filename_base or 'chat')).strip() or "chat"
    safe = safe.replace(" ", "_")[:80]
    btn_id = f"ytcc-cap-{uuid4().hex[:8]}"

    st_html(f"""
    <div style="width:100%;">
      <button id="{btn_id}" class="ytcc-cap-btn" type="button">{label}</button>
    </div>

    <script>
    (function(){{
      const BTN_ID = "{btn_id}";
      const FILE_BASE = "{safe}";
      const btn = document.getElementById(BTN_ID);
      if(!btn) return;

      function loadScriptOnce(src, id){{
        return new Promise((resolve, reject) => {{
          const d = window.parent.document;
          if(id && d.getElementById(id)) return resolve();
          const s = d.createElement("script");
          if(id) s.id = id;
          s.src = src;
          s.onload = () => resolve();
          s.onerror = () => reject(new Error("failed: " + src));
          d.head.appendChild(s);
        }});
      }}

      async function ensureLibs(){{
        if(!window.parent.html2canvas){{
          await loadScriptOnce("https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js", "ytcc-html2canvas");
        }}
        if(!window.parent.jspdf){{
          await loadScriptOnce("https://cdn.jsdelivr.net/npm/jspdf@2.5.1/dist/jspdf.umd.min.js", "ytcc-jspdf");
        }}
      }}

      async function captureToPdf(){{
        const doc = window.parent.document;
        const msgs = Array.from(doc.querySelectorAll('div[data-testid="stChatMessage"]'));
        if(!msgs || msgs.length === 0){{
          alert("저장할 대화가 없습니다.");
          return;
        }}

        const tmp = doc.createElement("div");
        tmp.style.position = "fixed";
        tmp.style.left = "-99999px";
        tmp.style.top = "0";
        tmp.style.background = "white";
        tmp.style.padding = "24px";
        tmp.style.borderRadius = "16px";
        tmp.style.boxSizing = "border-box";

        let maxW = 0;
        msgs.forEach(m => {{
          const r = m.getBoundingClientRect();
          if(r.width) maxW = Math.max(maxW, r.width);
        }});
        const capW = Math.max(1200, Math.min(1700, Math.ceil(maxW + 140)));
        tmp.style.width = capW + "px";

        msgs.forEach(m => {{
          const clone = m.cloneNode(true);
          clone.style.width = "100%";
          clone.style.maxWidth = "100%";
          clone.style.boxSizing = "border-box";
          clone.querySelectorAll("*").forEach(el => {{
            el.style.maxWidth = "100%";
            el.style.boxSizing = "border-box";
            el.style.overflowWrap = "anywhere";
            el.style.wordBreak = "break-word";
          }});
          tmp.appendChild(clone);
        }});

        doc.body.appendChild(tmp);

        try {{
          await ensureLibs();
          const canvas = await window.parent.html2canvas(tmp, {{
            scale: 2,
            backgroundColor: "#ffffff",
            useCORS: true,
            allowTaint: true,
            windowWidth: capW
          }});

          const imgData = canvas.toDataURL("image/png", 1.0);
          const {{ jsPDF }} = window.parent.jspdf;
          const pdf = new jsPDF("p", "mm", "a4");

          const pageW = pdf.internal.pageSize.getWidth();
          const pageH = pdf.internal.pageSize.getHeight();
          const imgW = pageW;
          const imgH = (canvas.height * imgW) / canvas.width;

          let y = 0;
          let remaining = imgH;

          while (remaining > 0) {{
            pdf.addImage(imgData, "PNG", 0, y, imgW, imgH, undefined, "FAST");
            remaining -= pageH;
            if (remaining > 0) {{
              pdf.addPage();
              y -= pageH;
            }}
          }}

          pdf.save(FILE_BASE + ".pdf");
        }} catch(e) {{
          console.error(e);
          alert("PDF 저장 중 오류가 발생했습니다.");
        }} finally {{
          try {{ tmp.remove(); }} catch(e) {{}}
        }}
      }}

      btn.addEventListener("click", () => {{
        if(btn.dataset.busy === "1") return;
        btn.dataset.busy = "1";
        const old = btn.innerText;
        btn.innerText = "저장중...";
        btn.disabled = true;
        captureToPdf().finally(() => {{
          btn.dataset.busy = "0";
          btn.innerText = old;
          btn.disabled = false;
        }});
      }});
    }})();
    </script>

    <style>
      .ytcc-cap-btn {{
        width: 100%;
        border-radius: 8px; 
        padding: 0.45rem 0.5rem;
        font-size: 0.82rem;
        font-weight: 600;
        line-height: 1.2;
        min-height: 2.15rem; 
        border: none;
        background: #f3f4f6; 
        color: #374151;
        cursor: pointer;
        box-sizing: border-box;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        transition: background-color 0.15s ease;
        display: flex;
        align-items: center;
        justify-content: center;
      }}
      .ytcc-cap-btn:hover {{
        background: #e5e7eb; 
        color: #111827;
      }}
      .ytcc-cap-btn:disabled {{
        background: #f9fafb;
        color: #e5e7eb;
        cursor: not-allowed;
      }}
    </style>
    """, height=46)

# endregion


# region [Auth: frontgate handoff]

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)

from frontgate.auth_utils import check_auth

def require_auth():
    current_user = check_auth("chatbot")
    user_id = str(current_user.get("id") or current_user.get("sub") or "")
    display_name = str(current_user.get("name") or user_id)
    role = str(current_user.get("role") or "user")

    st.session_state["current_user"] = current_user
    st.session_state["auth_ok"] = True
    st.session_state["auth_user_id"] = user_id
    st.session_state["auth_role"] = role
    st.session_state["auth_display_name"] = display_name
    return current_user
# endregion


# region [Helper Classes]
class RotatingKeys:
    def __init__(self, keys, state_key: str, use_session_state: bool = True, on_rotate=None):
        self.keys = [k.strip() for k in (keys or []) if isinstance(k, str) and k.strip()][:10]
        self.state_key = state_key
        self.use_session_state = bool(use_session_state)
        self.on_rotate = on_rotate

        # NOTE:
        # - 메인 스레드(Streamlit)는 st.session_state를 써도 되지만,
        # - ThreadPoolExecutor 워커 스레드에서는 st.session_state 접근이 불안정할 수 있음.
        #   (또한 build() 남발 시 소켓/FD가 쌓여 "Too many open files"가 나올 수 있음)
        if self.use_session_state:
            idx = st.session_state.get(state_key, 0)
        else:
            idx = 0

        self.idx = 0 if not self.keys else (idx % len(self.keys))

        if self.use_session_state:
            st.session_state[state_key] = self.idx

    def current(self):
        return self.keys[self.idx % len(self.keys)] if self.keys else None

    def rotate(self):
        if not self.keys:
            return
        self.idx = (self.idx + 1) % len(self.keys)
        if self.use_session_state:
            st.session_state[self.state_key] = self.idx
        if callable(self.on_rotate):
            self.on_rotate(self.idx, self.current())


class RotatingYouTube:
    def __init__(self, keys, state_key="yt_key_idx", use_session_state: bool = True):
        self.rot = RotatingKeys(keys, state_key, use_session_state=use_session_state)
        self.service = None
        self._build()

    def _build(self):
        key = self.rot.current()
        if not key:
            raise RuntimeError("YouTube API Key가 비어 있습니다.")
        # IMPORTANT:
        # build()를 반복 호출하면(특히 멀티스레드에서 video마다 새 인스턴스 생성 시)
        # 내부 HTTP/소켓/캐시 FD가 누적되어 [Errno 24] Too many open files로 터질 수 있음.
        # - cache_discovery=False : 디스커버리 캐시 파일 사용 방지(불필요한 FD/락 감소)
        self.service = build("youtube", "v3", developerKey=key, cache_discovery=False)

    def execute(self, factory, max_rotate: int | None = None):
        if not callable(factory):
            raise ValueError("factory must be callable")
        max_retries = max_rotate if isinstance(max_rotate, int) and max_rotate > 0 else len(self.rot.keys)
        max_retries = max(max_retries, 1)

        last_error = None
        for _ in range(max_retries):
            try:
                return factory(self.service).execute()
            except HttpError as e:
                last_error = e
                status = getattr(getattr(e, "resp", None), "status", None)
                content = getattr(e, "content", b"") or b""
                try:
                    msg = content.decode("utf-8", "ignore").lower()
                except Exception:
                    msg = str(content).lower()

                # 쿼터/레이트 제한이면 키 회전
                if status in (403, 429) and any(t in msg for t in ("quota", "rate", "limit", "exceeded")):
                    print(f"⚠️ [YouTube API] 키 제한 감지 → 키 교체 (Current idx: {self.rot.idx})")
                    self.rot.rotate()
                    self._build()
                    continue

                raise

        if last_error:
            raise last_error
        raise RuntimeError("YouTube API request failed with unknown reason.")


# ---- YouTube client getters (session/thread safe) ----
_YT_THREAD_LOCAL = threading.local()

def get_thread_youtube_client(keys):
    """ThreadPoolExecutor 워커 스레드에서 재사용할 YouTube client(=RotatingYouTube).
    - 워커 스레드마다 1개만 생성해서 build() 남발을 막음 → FD/소켓 누수 방지
    - st.session_state 접근 금지(use_session_state=False)
    """
    key_tuple = tuple(keys or [])
    rt = getattr(_YT_THREAD_LOCAL, "rt", None)
    if rt is None or getattr(_YT_THREAD_LOCAL, "key_tuple", None) != key_tuple:
        _YT_THREAD_LOCAL.key_tuple = key_tuple
        _YT_THREAD_LOCAL.rt = RotatingYouTube(list(key_tuple), state_key="yt_key_idx_thread", use_session_state=False)
    return _YT_THREAD_LOCAL.rt

def get_session_youtube_client(keys):
    """Streamlit 세션(메인 스레드)에서 재사용할 YouTube client."""
    key_tuple = tuple(keys or [])
    if ("yt_rt_session" not in st.session_state) or (st.session_state.get("yt_rt_session_keys") != key_tuple):
        st.session_state["yt_rt_session_keys"] = key_tuple
        st.session_state["yt_rt_session"] = RotatingYouTube(list(key_tuple), state_key="yt_key_idx", use_session_state=True)
    return st.session_state["yt_rt_session"]
# endregion


# region [DB & Session Management]

def db_list_sessions(user_id: str):
    """
    MongoDB에서 해당 사용자의 세션 목록을 가져옵니다.
    """
    try:
        if not _mongo_enabled():
            return []
            
        coll = _mongo_saved_sessions_coll()
        if coll is None:
            return []
            
        # updated_at 내림차순 정렬 (최신순)
        cur = coll.find({"user_id": user_id}, {"sess_name": 1, "updated_at": 1})
        rows = list(cur)
        rows.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
        
        return [d.get("sess_name") for d in rows if d.get("sess_name")]
    except Exception as e:
        print(f"List Sessions Error: {e}")
        return []

def db_delete_session(user_id: str, sess_name: str):
    """
    MongoDB에서 특정 세션을 삭제합니다.
    """
    try:
        if not _mongo_enabled():
            return
            
        coll = _mongo_saved_sessions_coll()
        if coll is None:
            return
        
        # _id가 "{user_id}/{sess_name}" 형식
        coll.delete_one({"_id": f"{user_id}/{sess_name}"})
    except Exception as e:
        print(f"Delete Session Error: {e}")

def db_rename_session(user_id: str, old_name: str, new_name: str):
    """
    MongoDB에서 세션 이름을 변경합니다. (Insert & Delete)
    """
    if not _mongo_enabled():
        return

    coll = _mongo_saved_sessions_coll()
    if coll is None:
        raise Exception("Mongo 연결 실패")

    old_id = f"{user_id}/{old_name}"
    new_id = f"{user_id}/{new_name}"

    if coll.find_one({"_id": new_id}):
        raise Exception("동일한 세션명이 이미 존재합니다.")

    doc = coll.find_one({"_id": old_id})
    if not doc:
        raise Exception("기존 세션을 찾을 수 없습니다.")

    # 새 ID 및 이름으로 업데이트
    doc["_id"] = new_id
    doc["sess_name"] = new_name
    
    now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    doc["updated_at"] = now
    
    # 새 문서 삽입 후 기존 문서 삭제 (MongoDB _id 변경 불가 제약)
    coll.insert_one(doc)
    coll.delete_one({"_id": old_id})

def _session_base_keyword() -> str:
    schema = st.session_state.get("last_schema", {}) or {}
    kw = (schema.get("keywords") or ["세션"])[0]
    kw = (kw or "").strip()
    base = re.sub(r"[^0-9A-Za-z가-힣]", "", kw)
    base = base[:12] if base else "세션"
    return base

def _next_session_number(user_id: str, base: str) -> int:
    try:
        sessions = db_list_sessions(user_id) or []
    except Exception:
        sessions = []

    pat = re.compile(rf"^{re.escape(base)}(\d+)$")
    max_n = 0
    for s in sessions:
        m = pat.match(str(s))
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except Exception:
                pass
    return max_n + 1 if max_n > 0 else 1

def _build_session_name() -> str:
    if st.session_state.get("loaded_session_name"):
        return st.session_state.loaded_session_name

    user_id = st.session_state.get('auth_user_id') or 'public'
    base = _session_base_keyword()
    n = _next_session_number(user_id, base)
    return f"{base}{n}"


def save_current_session_to_db():
    """
    현재 세션을 MongoDB에 저장합니다.
    """
    if not (_mongo_enabled() and st.session_state.get("chat") and st.session_state.get("last_csv")):
        return False, "저장할 데이터가 없거나 Mongo 설정이 누락되었습니다."

    coll = _mongo_saved_sessions_coll()
    if coll is None:
        return False, "Mongo 연결에 실패했습니다."

    sess_name = _build_session_name()
    user_id = st.session_state.get("auth_user_id") or "public"
    
    # 로컬 파일 시스템에도 캐싱 (다운로드 기능 및 빠른 로드용)
    local_dir = os.path.join(SESS_DIR, user_id, sess_name)
    os.makedirs(local_dir, exist_ok=True)

    try:
        # 1) meta (qa.json) 준비
        meta_data = {
            "chat": st.session_state.chat,
            "last_schema": st.session_state.get("last_schema"),
            "sample_text": st.session_state.get("sample_text"),
        }
        
        # 로컬 저장 (qa.json)
        meta_path = os.path.join(local_dir, "qa.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, ensure_ascii=False, indent=2)

        # 2) comments.csv 준비
        comments_src = st.session_state.last_csv
        comments_path = os.path.join(local_dir, "comments.csv")
        
        # 로컬 복사
        try:
            if comments_src != comments_path:
                with open(comments_src, "rb") as f_src:
                    with open(comments_path, "wb") as f_dst:
                        f_dst.write(f_src.read())
        except Exception:
            pass

        # DB 저장을 위해 읽기
        with open(comments_src, "rb") as f:
            comments_raw = f.read()

        # 3) videos.csv (optional) 준비
        videos_b64gz = ""
        videos_bytes = b""
        videos_path = os.path.join(local_dir, "videos.csv")
        
        if st.session_state.get("last_df") is not None:
            try:
                # 로컬 저장
                st.session_state.last_df.to_csv(videos_path, index=False, encoding="utf-8-sig")
                with open(videos_path, "rb") as f:
                    videos_bytes = f.read()
                videos_b64gz = _b64_gzip_bytes(videos_bytes)
            except Exception:
                videos_b64gz = ""

        # 4) MongoDB Update (Upsert)
        now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        doc_id = f"{user_id}/{sess_name}"
        
        update = {
            "$set": {
                "user_id": user_id,
                "sess_name": sess_name,
                "meta": meta_data,
                "comments_b64gz": _b64_gzip_bytes(comments_raw),
                "videos_b64gz": videos_b64gz,
                "comments_bytes": len(comments_raw),
                "videos_bytes": len(videos_bytes) if videos_bytes else 0,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        }
        coll.update_one({"_id": doc_id}, update, upsert=True)

        st.session_state.loaded_session_name = sess_name
        return True, sess_name

    except Exception as e:
        return False, f"저장 실패: {e}"


def load_session_from_db(sess_name: str):
    """
    MongoDB에서 세션을 불러옵니다.
    """
    user_id = st.session_state.get("auth_user_id") or "public"
    coll = _mongo_saved_sessions_coll()
    if coll is None:
        st.error("Mongo 연결에 실패했습니다.")
        return

    doc_id = f"{user_id}/{sess_name}"
    
    with st.spinner(f"세션 '{sess_name}' 불러오는 중..."):
        try:
            doc = coll.find_one({"_id": doc_id})
            if not doc:
                st.error("세션을 찾을 수 없습니다.")
                return

            meta = doc.get("meta") or {}
            if not (meta.get("chat") and doc.get("comments_b64gz")):
                st.error("세션 핵심 데이터가 손상되었거나 누락되었습니다.")
                return

            # 로컬 파일 시스템 복원
            local_dir = os.path.join(SESS_DIR, user_id, sess_name)
            os.makedirs(local_dir, exist_ok=True)

            # 1) State 복원
            _reset_chat_only(keep_auth=True)
            st.session_state.chat = meta.get("chat") or []
            st.session_state.last_schema = meta.get("last_schema") or {}
            st.session_state.sample_text = meta.get("sample_text") or ""
            st.session_state.loaded_session_name = sess_name

            # 2) comments.csv 복원
            comments_bytes = _ungzip_b64_to_bytes(doc.get("comments_b64gz") or "")
            comments_path = os.path.join(local_dir, "comments.csv")
            with open(comments_path, "wb") as f:
                f.write(comments_bytes)
            st.session_state.last_csv = comments_path

            # 3) videos.csv 복원
            videos_b64gz = doc.get("videos_b64gz") or ""
            if videos_b64gz:
                videos_bytes = _ungzip_b64_to_bytes(videos_b64gz)
                videos_path = os.path.join(local_dir, "videos.csv")
                with open(videos_path, "wb") as f:
                    f.write(videos_bytes)
                try:
                    st.session_state.last_df = pd.read_csv(videos_path)
                except Exception:
                    st.session_state.last_df = None
            else:
                st.session_state.last_df = None

        except Exception as e:
            st.error(f"세션 로드 실패: {e}")
# endregion




# region [Saved Sessions: Pending Actions Handler]
def _process_saved_session_actions():
    """Process pending session actions set by sidebar buttons (load/rename/delete).
    Must run on each rerun to execute the action exactly once."""
    if not _mongo_enabled():
        return

    user_id = st.session_state.get("auth_user_id") or "public"

    # Rename
    if st.session_state.get("session_to_rename"):
        old_name, new_name = st.session_state.session_to_rename
        st.session_state.pop("session_to_rename", None)
        new_name = (new_name or "").strip()
        if not new_name:
            st.warning("새 이름이 비어있습니다.")
        else:
            try:
                # [수정됨] github_rename_session -> db_rename_session
                db_rename_session(user_id, old_name, new_name)
                # update loaded marker if needed
                if st.session_state.get("loaded_session_name") == old_name:
                    st.session_state.loaded_session_name = new_name
            except Exception as e:
                st.error(f"세션 이름 변경 실패: {e}")
        st.rerun()

    # Delete
    if st.session_state.get("session_to_delete"):
        sess = st.session_state.session_to_delete
        st.session_state.pop("session_to_delete", None)
        try:
            # [수정됨] github_delete_folder -> db_delete_session
            db_delete_session(user_id, sess)
            # if currently loaded session deleted, clear chat (keep auth)
            if st.session_state.get("loaded_session_name") == sess:
                st.session_state.pop("loaded_session_name", None)
        except Exception as e:
            st.error(f"세션 삭제 실패: {e}")
        st.rerun()

    # Load
    if st.session_state.get("session_to_load"):
        sess = st.session_state.session_to_load
        st.session_state.pop("session_to_load", None)
        # [수정됨] load_session_from_github -> load_session_from_db
        load_session_from_db(sess)
        st.rerun()
# endregion

def serialize_comments_for_llm_from_file(csv_path: str,
                                         max_chars_per_comment=280,
                                         max_total_chars=420_000,
                                         top_n=1000,
                                         random_n=1000,
                                         dedup_key="text"):
    if not os.path.exists(csv_path):
        return "", 0, 0, {"error": "csv_not_found"}

    try:
        df_all = pd.read_csv(csv_path)
    except Exception:
        return "", 0, 0, {"error": "csv_read_failed"}

    if df_all.empty:
        return "", 0, 0, {"error": "csv_empty"}

    total_rows = len(df_all)

    unique_rows = None
    try:
        if dedup_key in df_all.columns:
            unique_rows = df_all[dedup_key].astype(str).str.strip().replace("", pd.NA).dropna().nunique()
    except Exception:
        unique_rows = None

    df_top_likes = df_all.sort_values("likeCount", ascending=False).head(top_n)
    df_remaining = df_all.drop(df_top_likes.index)

    if not df_remaining.empty:
        take_n = min(random_n, len(df_remaining))
        df_random = df_remaining.sample(n=take_n, random_state=42)
    else:
        df_random = pd.DataFrame()

    df_sample = pd.concat([df_top_likes, df_random], ignore_index=True)
    sampled_target = len(df_sample)

    lines, total_chars = [], 0
    used_top = len(df_top_likes)
    used_random = len(df_random)

    for _, r in df_sample.iterrows():
        if total_chars >= max_total_chars:
            break

        raw_text = str(r.get("text", "") or "").replace("\n", " ")
        prefix = f"[{'R' if int(r.get('isReply', 0)) == 1 else 'T'}|♥{int(r.get('likeCount', 0))}] "
        author_clean = str(r.get('author', '')).replace('\n', ' ')
        prefix += f"{author_clean}: "

        body = raw_text[:max_chars_per_comment] + '…' if len(raw_text) > max_chars_per_comment else raw_text

        line = prefix + body
        lines.append(line)
        total_chars += len(line) + 1

    meta = {
        "total_rows": total_rows,
        "unique_rows": unique_rows,
        "top_n": int(top_n),
        "random_n": int(random_n),
        "used_top": int(used_top),
        "used_random": int(used_random),
        "sampled_target": int(sampled_target),
        "llm_input_lines": int(len(lines)),
        "llm_input_chars": int(total_chars),
        "max_chars_per_comment": int(max_chars_per_comment),
        "max_total_chars": int(max_total_chars),
        "dedup_key": str(dedup_key),
    }
    return "\n".join(lines), len(lines), total_chars, meta


def tidy_answer(text: str) -> str:
    if not text:
        return ""
    
    text = re.sub(r"^```html", "", text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r"^```", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s+(?=<)", "", text, flags=re.MULTILINE)
    
    lines = text.splitlines()
    cleaned = []
    
    REMOVE_PATTERN = re.compile(r"유튜브\s*댓글\s*분석|보고서\s*작성|분석\s*결과", re.IGNORECASE)

    for line in lines:
        if not line.strip():
            cleaned.append(line)
            continue
        if REMOVE_PATTERN.search(line) and len(line) < 50:
            continue
        cleaned.append(line)

    return "\n".join(cleaned).strip()

YTB_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

def extract_video_ids_from_text(text: str) -> list:
    if not text:
        return []
    ids = set()
    for m in re.finditer(r"https?://youtu\.be/([A-Za-z0-9_-]{11})", text):
        ids.add(m.group(1))
    for m in re.finditer(r"https?://(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})", text):
        ids.add(m.group(1))
    for m in re.finditer(r"https?://(?:www\.)?youtube\.com/watch\?[^ \n]+", text):
        url = m.group(0)
        try:
            qs = dict((kv.split("=", 1) + [""])[:2] for kv in url.split("?", 1)[1].split("&"))
            v = qs.get("v", "")
            if YTB_ID_RE.fullmatch(v):
                ids.add(v)
        except Exception:
            pass
    return list(ids)

def strip_urls(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"https?://\S+", " ", s)
    return re.sub(r"\s+", " ", s).strip()
# endregion


# region [API Integrations: Gemini & YouTube]
def call_gemini_rotating(model_name, keys, system_instruction, user_payload,
                         timeout_s=240, max_tokens=8192) -> str:
    rk = RotatingKeys(keys, "gem_key_idx")
    if not rk.current():
        raise RuntimeError("Gemini API Key가 비어 있습니다.")

    real_sys_inst = None if (not system_instruction or not system_instruction.strip()) else system_instruction
    
    from google.generativeai.types import HarmCategory, HarmBlockThreshold
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    for _ in range(len(rk.keys) or 1):
        try:
            genai.configure(api_key=rk.current())
            model = genai.GenerativeModel(
                model_name,
                generation_config={"temperature": 0.2, "max_output_tokens": max_tokens},
                system_instruction=real_sys_inst 
            )
            with GeminiInflightSlot():
                resp = model.generate_content(
                    user_payload,
                    request_options={"timeout": timeout_s},
                    safety_settings=safety_settings 
                )
            
            if not resp: return "⚠️ AI 응답 없음"
            try:
                if getattr(resp, "text", None): return resp.text
            except ValueError:
                if resp.prompt_feedback: return f"⚠️ [차단] {resp.prompt_feedback}"
            
            if c0 := (getattr(resp, "candidates", None) or [None])[0]:
                if p0 := (getattr(c0, "content", None) and getattr(c0.content, "parts", None) or [None])[0]:
                    if hasattr(p0, "text"): return p0.text
            return "⚠️ [시스템] 내용 과다 또는 차단으로 답변 생성 실패"

        except Exception as e:
            if isinstance(e, TimeoutError) or "GEMINI_INFLIGHT_TIMEOUT" in str(e):
                return "⚠️ 현재 요청이 많아 AI 분석 대기열이 꽉 찼습니다. 잠시 후 다시 시도해주세요."
            msg = str(e).lower()
            if "429" in msg or "quota" in msg:
                if len(rk.keys) > 1:
                    rk.rotate()
                    continue
            print(f"Gemini API Error: {e}")
            raise e
    return ""

def call_gemini_smart_cache(model_name, keys, system_instruction, user_query, 
                            large_context_text=None, cache_key_in_session="current_cache"):
    rk = RotatingKeys(keys, "gem_key_idx")
    cached_info = st.session_state.get(cache_key_in_session, None)
    
    active_cache = None
    final_model = None
    
    from google.generativeai.types import HarmCategory, HarmBlockThreshold
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    if cached_info and not large_context_text:
        cache_name = cached_info.get("name")
        creator_key = cached_info.get("key")
        
        genai.configure(api_key=creator_key)
        try:
            active_cache = caching.CachedContent.get(cache_name)
            with GeminiInflightSlot():
                active_cache.update(ttl=timedelta(minutes=CACHE_TTL_MINUTES))
            
            final_model = genai.GenerativeModel.from_cached_content(
                cached_content=active_cache,
                generation_config={"temperature": 0.2, "max_output_tokens": GEMINI_MAX_TOKENS}
            )
        except Exception as e:
            active_cache = None
            large_context_text = st.session_state.get("sample_text_full_context", "")
            if not large_context_text:
                return "⚠️ [오류] 세션이 만료되어 복구할 데이터가 없습니다. 새로고침 해주세요."

    if not active_cache and large_context_text:
        st.session_state["sample_text_full_context"] = large_context_text

        for _ in range(len(rk.keys)):
            current_key = rk.current()
            genai.configure(api_key=current_key)
            try:
                # [수정 부분 시작] system_instruction이 빈 문자열이면 None으로 처리
                real_sys_inst = system_instruction if (system_instruction and system_instruction.strip()) else None
                # [수정 부분 끝]

                with GeminiInflightSlot():
                    active_cache = caching.CachedContent.create(
                        model=model_name,
                        display_name=f"ytcc_{uuid4().hex[:8]}",
                        system_instruction=real_sys_inst,  # 수정된 변수 사용
                        contents=[large_context_text],
                        ttl=timedelta(minutes=CACHE_TTL_MINUTES)
                    )
                
                st.session_state[cache_key_in_session] = {
                    "name": active_cache.name,
                    "key": current_key
                }
                
                final_model = genai.GenerativeModel.from_cached_content(
                    cached_content=active_cache,
                    generation_config={"temperature": 0.2, "max_output_tokens": GEMINI_MAX_TOKENS}
                )
                break
            except Exception as e:
                msg = str(e).lower()
                if "too short" in msg or "argument" in msg:
                    active_cache = None
                    break
                if "429" in msg or "quota" in msg:
                    rk.rotate()
                    continue
                raise e

    try:
        if final_model:
            with GeminiInflightSlot():
                resp = final_model.generate_content(user_query, safety_settings=safety_settings)
        else:
            full_payload = f"{system_instruction}\n\n{large_context_text or ''}\n\n{user_query}"
            return call_gemini_rotating(model_name, keys, None, full_payload)

        if resp and resp.text: return resp.text
        return "⚠️ [시스템] AI 응답 없음 (빈 내용)"
    except Exception as e:
        if isinstance(e, TimeoutError) or "GEMINI_INFLIGHT_TIMEOUT" in str(e):
            return "⚠️ 현재 요청이 많아 AI 분석 대기열이 꽉 찼습니다. 잠시 후 다시 시도해주세요."
        return f"⚠️ [시스템] 처리 중 에러: {e}"

def yt_search_videos(rt, keyword, max_results, order="viewCount",
                     published_after=None, published_before=None):
    video_ids, token = [], None
    while len(video_ids) < max_results:
        params = {
            "q": keyword, "part": "id", "type": "video", "order": order,
            "maxResults": min(50, max_results - len(video_ids))
        }
        if published_after: params["publishedAfter"] = published_after
        if published_before: params["publishedBefore"] = published_before
        if token: params["pageToken"] = token

        resp = rt.execute(lambda s: s.search().list(**params))
        video_ids.extend(it["id"]["videoId"] for it in resp.get("items", [])
                         if it["id"]["videoId"] not in video_ids)
        if not (token := resp.get("nextPageToken")):
            break
        time.sleep(0.25)
    return video_ids

def yt_video_statistics(rt, video_ids):
    rows = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        if not batch: continue

        resp = rt.execute(lambda s: s.videos().list(part="statistics,snippet,contentDetails", id=",".join(batch)))
        for item in resp.get("items", []):
            stats, snip, cont = item.get("statistics", {}), item.get("snippet", {}), item.get("contentDetails", {})
            
            # 1. Duration 변환 (ISO -> 초 -> MM:SS)
            dur = cont.get("duration", "")
            h, m, s = re.search(r"(\d+)H", dur), re.search(r"(\d+)M", dur), re.search(r"(\d+)S", dur)
            dur_sec = (int(h.group(1))*3600 if h else 0) + (int(m.group(1))*60 if m else 0) + (int(s.group(1)) if s else 0)
            
            if dur_sec >= 3600:
                dur_fmt = f"{dur_sec // 3600}:{(dur_sec % 3600) // 60:02}:{dur_sec % 60:02}"
            else:
                dur_fmt = f"{dur_sec // 60}:{dur_sec % 60:02}"

            # 2. PublishedAt 변환 (UTC -> KST 문자열)
            pub_raw = snip.get("publishedAt", "")
            pub_kst = pub_raw
            if pub_raw:
                try:
                    # 'Z'를 '+00:00'으로 바꿔서 파싱 후 KST로 변환
                    dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                    dt = dt.astimezone(KST)
                    pub_kst = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pub_kst = pub_raw

            vid_id = item.get("id")
            rows.append({
                "video_id": vid_id,
                "video_url": f"https://www.youtube.com/watch?v={vid_id}",
                "title": snip.get("title", ""),
                "channelTitle": snip.get("channelTitle", ""),
                "publishedAt": pub_kst,   # [수정됨] KST 적용
                "duration": dur_fmt,      # [수정됨] MM:SS 적용
                "shortType": "Shorts" if dur_sec <= 60 else "Clip",
                "viewCount": int(stats.get("viewCount", 0) or 0),
                "likeCount": int(stats.get("likeCount", 0) or 0),
                "commentCount": int(stats.get("commentCount", 0) or 0)
            })
        time.sleep(0.25)
    return rows

def yt_all_replies(rt, parent_id, video_id, title="", short_type="Clip", cap=None):
    replies, token = [], None
    while not (cap is not None and len(replies) >= cap):
        try:
            resp = rt.execute(lambda s: s.comments().list(part="snippet", parentId=parent_id, maxResults=100, pageToken=token, textFormat="plainText"))
        except HttpError: break

        for c in resp.get("items", []):
            sn = c["snippet"]
            replies.append({
                "video_id": video_id, "video_title": title, "shortType": short_type,
                "comment_id": c.get("id", ""), "parent_id": parent_id, "isReply": 1,
                "author": sn.get("authorDisplayName", ""), "text": sn.get("textDisplay", "") or "",
                "publishedAt": sn.get("publishedAt", ""), "likeCount": int(sn.get("likeCount", 0) or 0)
            })
        if not (token := resp.get("nextPageToken")): break
        time.sleep(0.2)
    return replies[:cap] if cap is not None else replies

def yt_all_comments_sync(rt_keys, video_id, title="", short_type="Clip",
                         include_replies=True, max_per_video=None):
    # 워커 스레드에서 YouTube client를 1개만 재사용 (build() 남발 방지)
    rt = get_thread_youtube_client(rt_keys)
    rows, token = [], None
    while not (max_per_video is not None and len(rows) >= max_per_video):
        try:
            resp = rt.execute(lambda s: s.commentThreads().list(part="snippet,replies", videoId=video_id, maxResults=100, pageToken=token, textFormat="plainText"))
        except HttpError: break

        for it in resp.get("items", []):
            top = it["snippet"]["topLevelComment"]["snippet"]
            thread_id = it["snippet"]["topLevelComment"]["id"]
            rows.append({
                "video_id": video_id, "video_title": title, "shortType": short_type,
                "comment_id": thread_id, "parent_id": "", "isReply": 0,
                "author": top.get("authorDisplayName", ""), "text": top.get("textDisplay", "") or "",
                "publishedAt": top.get("publishedAt", ""), "likeCount": int(top.get("likeCount", 0) or 0)
            })
            if include_replies and int(it["snippet"].get("totalReplyCount", 0) or 0) > 0:
                cap = None if max_per_video is None else max(0, max_per_video - len(rows))
                if cap == 0: break
                rows.extend(yt_all_replies(rt, thread_id, video_id, title, short_type, cap=cap))
        if not (token := resp.get("nextPageToken")): break
        time.sleep(0.2)
    return rows[:max_per_video] if max_per_video is not None else rows

def parallel_collect_comments_streaming(video_list, rt_keys, include_replies,
                                        max_total_comments, max_per_video, prog_bar):
    out_csv = os.path.join(BASE_DIR, f"collect_{uuid4().hex}.csv")
    wrote_header, total_written, done, total_videos = False, 0, 0, len(video_list)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(yt_all_comments_sync, rt_keys, v["video_id"], v.get("title", ""),
                      v.get("shortType", "Clip"), include_replies, max_per_video): v for v in video_list
        }
        for f in as_completed(futures):
            try:
                if comm := f.result():
                    dfc = pd.DataFrame(comm)
                    dfc.to_csv(out_csv, index=False, mode="a" if wrote_header else "w", header=not wrote_header, encoding="utf-8-sig")
                    wrote_header = True
                    total_written += len(dfc)
            except Exception: pass
            done += 1
            prog_bar.progress(min(0.90, 0.50 + (done / total_videos) * 0.40 if total_videos > 0 else 0.50), text="댓글 수집중…")
            if total_written >= max_total_comments: break
    return out_csv, total_written
# endregion


# region [UI Components]
def scroll_to_bottom():
    st_html(
        "<script> "
        "let last_message = document.querySelectorAll('.stChatMessage'); "
        "if (last_message.length > 0) { "
        "  last_message[last_message.length - 1].scrollIntoView({behavior: 'smooth'}); "
        "} "
        "</script>",
        height=0
    )

def render_metadata_and_downloads():
    if not (schema := st.session_state.get("last_schema")):
        return

    kw_main = schema.get("keywords", [])
    start_iso, end_iso = schema.get('start_iso', ''), schema.get('end_iso', '')
    try:
        start_dt_str = datetime.fromisoformat(start_iso).astimezone(KST).strftime('%Y-%m-%d %H:%M')
        end_dt_str   = datetime.fromisoformat(end_iso).astimezone(KST).strftime('%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        start_dt_str, end_dt_str = (start_iso.split('T')[0] if start_iso else ""), (end_iso.split('T')[0] if end_iso else "")

    with st.container(border=True):
        st.markdown(f"""
            <div style="font-size:14px; color:#4b5563; line-height:1.8;">
              <span style='font-weight:600;'>키워드:</span> {', '.join(kw_main) if kw_main else '(없음)'}<br>
              <span style='font-weight:600;'>기간:</span> {start_dt_str} ~ {end_dt_str} (KST)
            </div>
            """, unsafe_allow_html=True)

        csv_path, df_videos = st.session_state.get("last_csv"), st.session_state.get("last_df")
        if csv_path and os.path.exists(csv_path) and df_videos is not None and not df_videos.empty:
            with open(csv_path, "rb") as f: comment_csv_data = f.read()
            buffer = io.BytesIO()
            df_videos.to_csv(buffer, index=False, encoding="utf-8-sig")
            video_csv_data = buffer.getvalue()
            keywords_str = "_".join(kw_main).replace(" ", "_") if kw_main else "data"
            now_str = now_kst().strftime('%Y%m%d')

            col1, col2, col3, col4, _ = st.columns([1.1, 1.2, 1.2, 1.6, 5.0])
            col1.markdown("<div style='font-size:14px; color:#4b...ght:600; padding-top:5px;'>다운로드:</div>", unsafe_allow_html=True)

            with col2:
                st.download_button("전체댓글", comment_csv_data, f"comments_{keywords_str}_{now_str}.csv", "text/csv")

            with col3:
                st.download_button("영상목록", video_csv_data, f"videos_{keywords_str}_{now_str}.csv", "text/csv")

            sample_text = (st.session_state.get("sample_text") or "").strip()
            if sample_text:
                sample_bytes = sample_text.encode("utf-8-sig")
                with col4:
                    st.download_button(
                        "AI샘플(LLM입력)",
                        sample_bytes,
                        f"llm_sample_{keywords_str}_{now_str}.txt",
                        "text/plain"
                    )

                sample_cnt = st.session_state.get("sample_count")
                sample_chars = st.session_state.get("sample_chars")
                if sample_cnt is not None and sample_chars is not None:
                    st.caption(f"AI 입력 샘플: {sample_cnt:,}줄 / {sample_chars:,} chars")

def render_chat():
    for msg in st.session_state.chat:
        with st.chat_message(msg.get("role", "user")):
            content = msg.get("content", "")
            
            if isinstance(content, str) and msg.get("role") == "assistant" and ("<div" in content or "<style" in content):
                report_style = """
                <style>
                .yt-report { font-family: "Helvetica Neue", Arial, sans-serif; line-height: 1.6; color: #333; }
                .yt-report .header { border-bottom: 2px solid #eee; padding-bottom: 10px; margin-bottom: 15px; }
                .yt-report .badge { background: #f0f2f6; color: #31333F; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; margin-right: 5px; font-weight: 600; }
                .yt-report .card { background: white; border: 1px solid #ddd; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
                .yt-report h3 { font-size: 1.1em; margin-top: 0; margin-bottom: 10px; color: #000; font-weight: 700; }
                .yt-report .quote { border-left: 3px solid #ff4b4b; padding-left: 10px; color: #555; font-style: italic; margin: 5px 0; font-size: 0.95em; background: #fafafa; padding: 5px 10px; }
                .yt-report table { width: 100%; border-collapse: collapse; font-size: 0.9em; margin: 10px 0; }
                .yt-report th { text-align: left; border-bottom: 2px solid #ddd; padding: 5px; color: #555; background-color: #f9fafb; }
                .yt-report td { border-bottom: 1px solid #eee; padding: 8px 5px; vertical-align: top; }
                </style>
                """
                full_html = f"<div class='yt-report'>{report_style}{content}</div>"
                st.markdown(full_html, unsafe_allow_html=True)
                
            else:
                st.markdown(content)
# endregion


# region [Main Pipeline]
LIGHT_PROMPT = (
    "역할: 유튜브 댓글 반응 분석기의 자연어 해석가.\n"
    "목표: 한국어 입력에서 [기간(KST)]과 [키워드/옵션]만 정확히 추출.\n"
    "규칙:\n"
    "- 기간은 Asia/Seoul 기준, 상대기간의 종료는 지금.\n"
    "- '키워드'는 검색에 사용할 핵심 주제 1개로 한정.\n"
    "- 옵션: include_replies, channel_filter(any|official|unofficial), lang(ko|en|auto).\n\n"
    "출력(5줄 고정):\n"
    "- 한 줄 요약: <문장>\n"
    "- 기간(KST): <YYYY-MM-DDTHH:MM:SS+09:00> ~ <YYYY-MM-DDTHH:MM:SS+09:00>\n"
    "- 키워드: [<핵심 키워드 1개>]\n"
    "- 옵션: { include_replies: true|false, channel_filter: \"any|official|unofficial\", lang: \"ko|en|auto\" }\n"
    "- 원문: {USER_QUERY}\n\n"
    f"현재 KST: {to_iso_kst(now_kst())}\n"
    "입력:\n{USER_QUERY}"
)

def parse_light_block_to_schema(light_text: str) -> dict:
    raw = (light_text or "").strip()

    m_time = re.search(r"기간\(KST\)\s*:\s*([^~]+)~\s*([^\n]+)", raw)
    start_iso, end_iso = (m_time.group(1).strip(), m_time.group(2).strip()) if m_time else (None, None)

    m_kw = re.search(r"키워드\s*:\s*\[(.*?)\]", raw, flags=re.DOTALL)
    keywords = [p.strip() for p in re.split(r"\s*,\s*", m_kw.group(1)) if p.strip()] if m_kw else []

    m_opt = re.search(r"옵션\s*:\s*\{(.*?)\}", raw, flags=re.DOTALL)
    options = {"include_replies": False, "channel_filter": "any", "lang": "auto"}
    if m_opt:
        blob = m_opt.group(1)
        ir = re.search(r"include_replies\s*:\s*(true|false)", blob, re.I)
        if ir:
            options["include_replies"] = (ir.group(1).lower() == "true")
        cf = re.search(r"channel_filter\s*:\s*\"(any|official|unofficial)\"", blob, re.I)
        if cf:
            options["channel_filter"] = cf.group(1)
        lg = re.search(r"lang\s*:\s*\"(ko|en|auto)\"", blob, re.I)
        if lg:
            options["lang"] = lg.group(1)

    if not (start_iso and end_iso):
        end_dt = now_kst()
        start_dt = end_dt - timedelta(hours=24)
        start_iso, end_iso = to_iso_kst(start_dt), to_iso_kst(end_dt)

    if not keywords:
        tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", raw)
        keywords = [tokens[0]] if tokens else ["유튜브"]

    return {"start_iso": start_iso, "end_iso": end_iso, "keywords": keywords, "options": options, "raw": raw}


def run_pipeline_first_turn(user_query: str, extra_video_ids=None, only_these_videos: bool = False):
    extra_video_ids = list(dict.fromkeys(extra_video_ids or []))
    prog_bar = st.progress(0, text="준비 중…")

    if not GEMINI_API_KEYS: return "오류: Gemini API Key가 설정되지 않았습니다."
    prog_bar.progress(0.05, text="해석중…")
    
    light = call_gemini_rotating(GEMINI_MODEL, GEMINI_API_KEYS, "", LIGHT_PROMPT.replace("{USER_QUERY}", user_query))
    schema = parse_light_block_to_schema(light)
    st.session_state["last_schema"] = schema
    log_search_history(user_query, schema)

    prog_bar.progress(0.10, text="영상 수집중…")
    if not YT_API_KEYS: return "오류: YouTube API Key가 설정되지 않았습니다."
    
    rt = get_session_youtube_client(YT_API_KEYS)
    start_dt, end_dt = datetime.fromisoformat(schema["start_iso"]), datetime.fromisoformat(schema["end_iso"])
    kw_main = schema.get("keywords", [])

    own_mode = bool(st.session_state.get("own_ip_mode", False))
    pgc_ids = []
    
    # ===== 자사 IP 모드 - DB 직접 검색 (메모리 최적화) =====
    if own_mode:
        pgc_data = search_pgc_data(kw_main, start_dt, end_dt)
        
        if pgc_data:
            pgc_ids = [item.get("id") for item in pgc_data if item.get("id")]
            pgc_ids = list(dict.fromkeys(pgc_ids))

    if only_these_videos and extra_video_ids:
        all_ids = extra_video_ids
    else:
        all_ids = []
        
        # ===== UGC 검색어 설정 및 검색 로직 (모드 분기) =====
        strict_mode = bool(st.session_state.get("strict_search_mode", False))
        
        for base_kw in (kw_main or ["유튜브"]):
            from urllib.parse import quote
            clean_kw = base_kw.replace(" ", "")
            
            if strict_mode:
                # 좁은 검색: 해시태그가 정확히 포함된 영상만 수집
                search_kw = clean_kw if clean_kw.startswith("#") else f"#{clean_kw}"
            else:
                # 넓은 검색: 일반 키워드와 해시태그 포함 영상 모두 수집 (OR 연산자 활용)
                base_no_hash = clean_kw.lstrip("#")
                search_kw = f"{base_no_hash} | #{base_no_hash}"
                
            if search_kw:
                all_ids.extend(yt_search_videos(rt, search_kw, 100, "viewCount", kst_to_rfc3339_utc(start_dt), kst_to_rfc3339_utc(end_dt)))
        
        if extra_video_ids:
            all_ids.extend(extra_video_ids)
            
        # PGC(자사 IP) 아이디 합치기
        if own_mode and pgc_ids:
            all_ids.extend(pgc_ids)

    all_ids = list(dict.fromkeys(all_ids))
    prog_bar.progress(0.40, text="댓글 수집 준비중…")

    df_stats = pd.DataFrame(yt_video_statistics(rt, all_ids))
    
    # OST 제외 필터 (제목 기준)
    if bool(st.session_state.get("own_ip_mode", False)) and (not df_stats.empty) and ("title" in df_stats.columns):
        df_stats = df_stats[~df_stats["title"].astype(str).str.contains(r"\bOST\b", case=False, na=False)]
    
    st.session_state["last_df"] = df_stats

    csv_path, total_cnt = parallel_collect_comments_streaming(
        df_stats.to_dict('records'), YT_API_KEYS, bool(schema.get("options", {}).get("include_replies")),
        MAX_TOTAL_COMMENTS, MAX_COMMENTS_PER_VID, prog_bar
    )
    st.session_state["last_csv"] = csv_path

    if total_cnt == 0:
        prog_bar.empty()
        return "지정 조건에서 댓글을 찾을 수 없습니다. 다른 조건으로 시도해 보세요."

    prog_bar.progress(0.90, text="AI 분석중…")

    sample_text, sample_cnt, sample_chars, sample_meta = serialize_comments_for_llm_from_file(csv_path)

    st.session_state["sample_text"] = sample_text
    st.session_state["sample_count"] = sample_cnt
    st.session_state["sample_chars"] = sample_chars
    st.session_state["sample_meta"] = sample_meta

    sys = load_first_turn_system_prompt()

    used_top = sample_meta.get("used_top", 0)
    used_random = sample_meta.get("used_random", 0)
    
    analysis_scope_line = (
        f"{sample_cnt:,}개 (추출: 인기댓글 {used_top:,}개 + 랜덤 {used_random:,}개, "
    )
    st.session_state["analysis_scope_line"] = analysis_scope_line

    metrics_block = (
        "[METRICS]\n"
        f"TOTAL_COLLECTED_COMMENTS={sample_meta.get('total_rows', 'NA')}\n"
        f"UNIQUE_COMMENTS_BY_{str(sample_meta.get('dedup_key','text')).upper()}={sample_meta.get('unique_rows', 'NA')}\n"
        f"SAMPLE_RULE=top_like:{used_top}/{sample_meta.get('top_n', 1000)}, random:{used_random}/{sample_meta.get('random_n', 1000)}\n"
        f"LLM_INPUT_LINES={sample_cnt}\n"
        f"LLM_INPUT_CHARS={sample_chars}\n"
        f"ANALYSIS_COMMENT_COUNT_LINE={analysis_scope_line}\n"
    )

    large_context_text = (
        f"{metrics_block}\n"
        f"[키워드]: {', '.join(kw_main)}\n"
        f"[기간(KST)]: {schema['start_iso']} ~ {schema['end_iso']}\n\n"
        f"[댓글 샘플]:\n{sample_text}\n"
    )
    user_query_part = f"[사용자 원본 질문]: {user_query}"

    if "current_cache" in st.session_state:
        del st.session_state["current_cache"]

    answer_md_raw = call_gemini_smart_cache(
        GEMINI_MODEL, GEMINI_API_KEYS, sys, user_query_part,
        large_context_text=large_context_text,
        cache_key_in_session="current_cache"
    )

    prog_bar.progress(1.0, text="완료")
    time.sleep(0.5)
    prog_bar.empty()
    gc.collect()

    return tidy_answer(answer_md_raw)


def run_followup_turn(user_query: str):
    if not (schema := st.session_state.get("last_schema")):
        return "오류: 이전 분석 기록이 없습니다. 새 채팅 시작해주세요."

    context = "\n".join(f"[이전 {'Q' if m['role'] == 'user' else 'A'}]: {m['content']}" for m in st.session_state["chat"][-10:])

    followup_instruction = (
        "🛑 [지시사항 변경] 🛑\n"
        "지금부터는 전체 요약가가 아니라, 사용자의 질문 하나하나를 파고드는 **'심층 분석가'**로서 행동해.\n"
        "첫 질문에 대한 응답처럼 규격화된 HTML로 주지 않아도 된다.\n"
        "이전의 요약 미션은 잊어. 오직 아래 [현재 질문]에만 집중해서 답해.\n\n"
        "=== 답변 전략 ===\n"
        "1. 질문의 의도(속성/대상)를 먼저 파악해라.(파악한 의도는 답변을 위한 내부 지침으로만 활용하고, 사용자에게 보여주지 않아도 된다)\n"
        "2. 네 기억 속에 있는 [댓글 샘플]에서 그와 관련된 구체적인 증거(댓글)를 찾아라.\n"
        "3. 증거 댓글은 눈에 잘 띄도록 반드시 `<div class='quote'>댓글 내용</div>` 태그로 감싸서 출력해라.\n"
        "4. 질문과 관련 없는 TMI(다른 배우, 다른 이슈 등)는 절대 말하지 마라.\n"
        "5. 만약 관련 내용이 데이터에 없으면 '데이터에서 확인되지 않는다'고 딱 잘라 말해라.\n"
    )

    user_payload = (
        f"{followup_instruction}\n\n"
        f"{context}\n\n"
        f"[현재 질문]: {user_query}\n"
        f"[기간(KST)]: {schema.get('start_iso', '?')} ~ {schema.get('end_iso', '?')}\n"
    )

    with st.spinner("💬 답변 생성 중... "):
        response_raw = call_gemini_smart_cache(GEMINI_MODEL, GEMINI_API_KEYS, "", user_payload, large_context_text=None)
        response = tidy_answer(response_raw)

    return response
# endregion


# region [Main Execution]
require_auth()

with st.sidebar:
    st.markdown('<div style="height: 20px;"></div>', unsafe_allow_html=True)

    if st.session_state.get("auth_user_id"):
        disp = st.session_state.get("auth_display_name", st.session_state.get("auth_user_id"))
        role = st.session_state.get("auth_role", "user")
        
        c_user, c_logout = st.columns([0.75, 0.25], gap="small")
        with c_user:
            st.markdown(f"""
            <div style="display:flex; align-items:baseline; padding-top:4px;">
                <span class="user-info-text">{disp}</span>
                <span class="user-role-text">({role})</span>
            </div>
            """, unsafe_allow_html=True)
            
        with c_logout:
            frontgate_url = (st.secrets.get("apps", {}) or {}).get("frontgate", "")
            if frontgate_url:
                st.markdown(
                    f"""
                    <a href="{frontgate_url}" target="_self" 
                       style="float:right; color:#6b7280; font-size:0.75rem; text-decoration:underline; 
                              font-weight:500; cursor:pointer; margin-top:4px;">
                       포털로
                    </a>
                    """,
                    unsafe_allow_html=True
                )
            
        st.markdown('<div style="border-bottom:1px solid #efefef; margin-bottom:12px; margin-top:2px;"></div>', unsafe_allow_html=True)

    if st.button("＋ 새 분석 시작", type="primary", use_container_width=True):
        _reset_chat_only(keep_auth=True)
        st.rerun()
    
    st.markdown('<div style="margin-bottom: 6px;"></div>', unsafe_allow_html=True)
    
    if st.session_state.chat:
        c1, c2 = st.columns(2, gap="small") 
        with c1:
            has_data = bool(st.session_state.last_csv)
            if st.button("세션 저장", use_container_width=True, disabled=not has_data):
                if has_data:
                    with st.spinner("저장..."):
                        success, result = save_current_session_to_db()
                    if success:
                        st.success("완료")
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error(result)
        
        with c2:
            pdf_title = _session_title_for_pdf()
            render_pdf_capture_button("PDF 저장", pdf_title)

    st.markdown('<div class="session-list-container">', unsafe_allow_html=True)
    st.markdown('<div class="session-header">Recent History</div>', unsafe_allow_html=True)

    if not _mongo_enabled():
        st.caption("Mongo 미설정")
    else:
        try:
            user_id = st.session_state.get("auth_user_id") or "public"
            _process_saved_session_actions()
            
            sessions = db_list_sessions(user_id)
            
            if not sessions: 
                st.caption("기록 없음")
            else:
                editing_session = st.session_state.get("editing_session", None)
                for sess in sessions:
                    if sess == editing_session:
                        with st.container(border=True):
                            new_name = st.text_input("이름 변경", value=sess, key=f"new_name_{sess}", label_visibility="collapsed")
                            ec1, ec2 = st.columns(2)
                            if ec1.button("V", key=f"save_{sess}", use_container_width=True):
                                st.session_state.session_to_rename = (sess, new_name)
                                st.session_state.pop('editing_session', None)
                                st.rerun()
                            if ec2.button("X", key=f"cancel_{sess}", use_container_width=True):
                                st.session_state.pop('editing_session', None)
                                st.rerun()
                    else:
                        sc1, sc2 = st.columns([0.85, 0.15], gap="small")
                        with sc1:
                            st.markdown('<div class="sess-name">', unsafe_allow_html=True)
                            if st.button(f"▪ {sess}", key=f"sess_{sess}", use_container_width=True):
                                st.session_state.session_to_load = sess
                                st.rerun()
                            st.markdown('</div>', unsafe_allow_html=True)
                        with sc2:
                            st.markdown('<div class="more-menu">', unsafe_allow_html=True)
                            if hasattr(st, "popover"):
                                with st.popover(":", use_container_width=True):
                                    if st.button("수정", key=f"more_edit_{sess}", use_container_width=True):
                                        st.session_state.editing_session = sess
                                        st.rerun()
                                    if st.button("삭제", key=f"more_del_{sess}", type="primary", use_container_width=True):
                                        st.session_state.session_to_delete = sess
                                        st.rerun()
                            st.markdown('</div>', unsafe_allow_html=True)
        except Exception as e: 
            st.error(f"Error: {e}")
            
    st.markdown("""
        <div style="margin-top:auto; padding-top:1rem; font-size:0.9rem; color:#6b7280; text-align:center;">
            Media) Marketing Team - Data Insight Part<br>Powered by Gemini
        </div>
    """, unsafe_allow_html=True)


if not st.session_state.chat:
    st.markdown(
        """
<div style="display:flex; flex-direction:column; align-items:center; justify-content:center;
            text-align:center; padding-top:8vh;">
  <div class="ytcc-main-title">유튜브 댓글분석 AI 챗봇</div>
  <p style="font-size:1.1rem; color:#6b7280; max-width:600px; margin-top:10px; margin-bottom: 2rem;">
    유튜브 여론이 궁금한 드라마에 대해 대화형식으로 물어보세요<br>
    유튜브 댓글 기반의 시청자 반응을 AI가 분석해줍니다.
  </p>
  
  <div style="background-color:#fff1f2; border:1px solid #ffe4e6; border-radius:12px; 
              padding:1rem 1.5rem; max-width:650px; text-align:left; margin-bottom:1rem; width:100%;">
    <h4 style="margin:0 0 0.5rem 0; font-size:0.95rem; font-weight:700; color:#9f1239;">⚠️ 사용 전 확인해주세요</h4>
    <ul style="margin:0; padding-left:1.2rem; font-size:0.9rem; color:#881337; line-height:1.6;">
        <li><strong>첫 질문 시</strong> 댓글 수집 및 AI 분석에 시간이 소요될 수 있습니다.</li>
        <li>한 세션에서는 <strong>하나의 주제</strong>만 진행해야 분석 정확도가 유지됩니다.</li>
        <li>정확한 분석을 위해 질문에 <strong>기간을 명시</strong>해주세요 (예: 최근 48시간).</li>
    </ul>
  </div>

  <div style="padding:1.5rem; border:1px solid #e5e7eb; border-radius:16px;
              background-color:#ffffff; max-width:650px; text-align:left; box-shadow:0 4px 6px -1px rgba(0,0,0,0.05); width:100%;">
    <h4 style="margin-bottom:1rem; font-size:1rem; font-weight:700; color:#374151;">💡 이렇게 질문해보세요</h4>
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <span style="background:#f3f4f6; padding:6px 12px; border-radius:20px; font-size:0.85rem; color:#4b5563;">최근 24시간 태풍상사 반응 요약해줘</span>
        <span style="background:#f3f4f6; padding:6px 12px; border-radius:20px; font-size:0.85rem; color:#4b5563;">https://youtu.be/xxxx 분석해줘</span>
        <span style="background:#f3f4f6; padding:6px 12px; border-radius:20px; font-size:0.85rem; color:#4b5563;">12월 한달간 프로보노 반응 분석해줘</span>
        <span style="background:#f3f4f6; padding:6px 12px; border-radius:20px; font-size:0.85rem; color:#4b5563;">(후속대화)"정경호"연기력에 대한 반응은 어때?</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

    # ===== 검색 및 데이터 모드 설정 토글 UI =====
    _, col_toggle1, col_toggle2, _ = st.columns([0.8, 1.2, 1.2, 0.8])
    
    with col_toggle1:
        st.write("") 
        st.toggle(
            "🏢 자사 IP 모드", 
            key="own_ip_mode",
        )
        cur_toggle = bool(st.session_state.get("own_ip_mode", False))
        prev_toggle = st.session_state.get("own_ip_toggle_prev", None)
        
        if cur_toggle and (prev_toggle is None or prev_toggle is False):
            with st.spinner("데이터베이스 확인 중..."):
                total_cnt = get_total_pgc_count()
                
                if total_cnt > 0:
                    st.success(f"자사 IP 데이터 연동됨 ({total_cnt:,}개)")
                else:
                    st.warning("데이터가 없거나 DB 연결 실패")
                    
        st.session_state["own_ip_toggle_prev"] = cur_toggle

    with col_toggle2:
        st.write("") 
        st.toggle(
            "🛡️ 엄격한 검색 모드", 
            key="strict_search_mode",
            help="체크 시 해시태그(#)가 정확히 일치하는 영상만 수집합니다. 엉뚱한 노이즈가 섞일 때 켜주세요."
        )

else:
    render_metadata_and_downloads()
    render_chat()


if prompt := st.chat_input("질문을 입력하거나 영상 URL을 붙여넣으세요..."):
    st.session_state.chat.append({"role": "user", "content": prompt})
    st.rerun()

# ===== 대화 및 분석 실행 분기 로직 =====
if st.session_state.chat and st.session_state.chat[-1]["role"] == "user":
    user_query = st.session_state.chat[-1]["content"]
    url_ids = extract_video_ids_from_text(user_query)
    
    # URL 포함 여부 확인
    has_urls = len(url_ids) > 0

    if not st.session_state.get("last_csv"):
        # 첫 번째 분석 턴
        if has_urls:
            # 텍스트 유무와 상관없이 URL이 하나라도 있으면 해당 영상만 강제 분석 (only_these_videos=True)
            response = run_pipeline_first_turn(user_query, extra_video_ids=url_ids, only_these_videos=True)
        else:
            # URL이 없는 순수 텍스트 질문인 경우 UGC 키워드 검색 수행
            response = run_pipeline_first_turn(user_query)
    else:
        # 후속 대화 턴
        response = run_followup_turn(user_query)

    st.session_state.chat.append({"role": "assistant", "content": response})
    st.rerun()
# endregion