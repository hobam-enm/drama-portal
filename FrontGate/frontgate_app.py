from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as py_secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import streamlit as st
from streamlit.components.v1 import html as st_html

try:
    import extra_streamlit_components as stx
except Exception:
    stx = None

try:
    from pymongo import MongoClient
    from pymongo.collection import Collection
except Exception:
    MongoClient = None
    Collection = Any

KST = timezone(timedelta(hours=9))
UTC = timezone.utc


# =========================================================
# page
# =========================================================
st.set_page_config(page_title="드라마 데이터 포털", page_icon="🧭", layout="wide")


# =========================================================
# secrets helpers
# =========================================================
def sget(*keys: str, default=None):
    cur: Any = st.secrets
    try:
        for k in keys:
            cur = cur[k]
        return cur
    except Exception:
        return default


def get_auth_cfg() -> Dict[str, Any]:
    cfg = dict(sget("auth", default={}) or {})
    cfg.setdefault("session_ttl_hours", 12)
    cfg.setdefault("session_idle_minutes", 120)
    cfg.setdefault("remember_me_days", 3)
    cfg.setdefault("frontpage_password", "")
    cfg.setdefault("token", "")
    cfg.setdefault("pepper", "")
    cfg.setdefault("signing_secret", "")
    cfg.setdefault("cookie_name", "drama_portal_session")
    cfg.setdefault("admin_role_name", "admin")
    cfg.setdefault("default_role", "user")
    return cfg


def get_mongo_cfg() -> Dict[str, Any]:
    cfg = dict(sget("mongo", default={}) or {})
    cfg.setdefault("uri", "")
    cfg.setdefault("db_name", "drama_portal")
    cfg.setdefault("users_coll", "users")
    cfg.setdefault("signup_requests_coll", "signup_requests")
    cfg.setdefault("sessions_coll", "sessions")
    cfg.setdefault("audit_logs_coll", "audit_logs")
    return cfg


AUTH = get_auth_cfg()
MONGO = get_mongo_cfg()
COOKIE_NAME = AUTH["cookie_name"]
SIGNING_SECRET = str(AUTH.get("signing_secret") or "")
PEPPER = str(AUTH.get("pepper") or "")


# =========================================================
# cookie manager
# =========================================================
def get_cookie_manager():
    if stx is None:
        return None
    try:
        if "_frontgate_cookie_manager" not in st.session_state:
            st.session_state["_frontgate_cookie_manager"] = stx.CookieManager()
        return st.session_state["_frontgate_cookie_manager"]
    except Exception:
        return None


def get_cookie(name: str) -> str:
    cm = get_cookie_manager()
    if cm is None:
        return ""
    try:
        cookies = cm.get_all() or {}
        return str(cookies.get(name, "") or "")
    except Exception:
        return ""


def set_cookie(name: str, value: str, days: int = 3):
    cm = get_cookie_manager()
    if cm is None:
        return
    try:
        expires_at = datetime.now(KST) + timedelta(days=days)
        cm.set(name, value, expires_at=expires_at, secure=True, same_site="Lax")
    except Exception:
        pass


def delete_cookie(name: str):
    cm = get_cookie_manager()
    if cm is None:
        return
    try:
        cm.delete(name)
    except Exception:
        pass


# =========================================================
# mongo
# =========================================================
@st.cache_resource(show_spinner=False)
def get_mongo_client():
    uri = str(MONGO.get("uri") or "")
    if not uri or MongoClient is None:
        return None
    return MongoClient(uri)


@st.cache_resource(show_spinner=False)
def get_db():
    client = get_mongo_client()
    if client is None:
        return None
    return client[MONGO["db_name"]]


def mongo_available() -> bool:
    return get_db() is not None


def coll(name_key: str):
    db = get_db()
    if db is None:
        return None
    return db[MONGO[name_key]]


# =========================================================
# crypto / password
# =========================================================
def utcnow() -> datetime:
    return datetime.now(UTC)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def pbkdf2_hash_password(password: str, pepper: str, iterations: int = 250_000) -> str:
    salt = py_secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        f"{password}{pepper}".encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored_hash: str, pepper: str) -> bool:
    if not stored_hash:
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iter_s, salt, digest = stored_hash.split("$", 3)
            iterations = int(iter_s)
            cand = hashlib.pbkdf2_hmac(
                "sha256",
                f"{password}{pepper}".encode("utf-8"),
                salt.encode("utf-8"),
                iterations,
            ).hex()
            return hmac.compare_digest(cand, digest)
        except Exception:
            return False
    return hmac.compare_digest(password, stored_hash)


def sign_payload(payload: Dict[str, Any]) -> str:
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(SIGNING_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def verify_signed_payload(token: str) -> Optional[Dict[str, Any]]:
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(SIGNING_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), sig):
            return None
        payload = json.loads(_b64url_decode(body))
        exp = int(payload.get("exp", 0))
        if exp and utcnow().timestamp() > exp:
            return None
        return payload
    except Exception:
        return None


# =========================================================
# data access
# =========================================================
def get_seed_users_from_secrets() -> List[Dict[str, Any]]:
    users = sget("users", default=[]) or []
    normalized: List[Dict[str, Any]] = []
    for u in users:
        u = dict(u)
        u.setdefault("active", True)
        u.setdefault("allowed_apps", [])
        u.setdefault("permissions", [])
        normalized.append(u)
    return normalized


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    if mongo_available():
        c = coll("users_coll")
        user = c.find_one({"id": user_id})
        if user:
            return user
    for u in get_seed_users_from_secrets():
        if str(u.get("id")) == str(user_id):
            return u
    return None


def authenticate_user(login_id: str, password: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    if not login_id or not password:
        return False, None, "아이디와 비밀번호를 입력하세요."

    candidates: List[Dict[str, Any]] = []
    if mongo_available():
        c = coll("users_coll")
        doc = c.find_one({"id": login_id})
        if doc:
            candidates.append(doc)
    candidates.extend([u for u in get_seed_users_from_secrets() if str(u.get("id")) == str(login_id)])

    if not candidates:
        return False, None, "존재하지 않는 계정입니다."

    for user in candidates:
        if not bool(user.get("active", True)):
            return False, None, "비활성화된 계정입니다."
        if verify_password(password, str(user.get("pw_hash") or ""), PEPPER):
            return True, user, ""

    return False, None, "비밀번호가 올바르지 않습니다."


# =========================================================
# sessions
# =========================================================
def create_session(user: Dict[str, Any], remember: bool = False, source_app: str = "frontgate") -> str:
    raw = py_secrets.token_urlsafe(32)
    ttl_hours = int(AUTH.get("session_ttl_hours", 12))
    remember_days = int(AUTH.get("remember_me_days", 3))
    now = utcnow()
    expires_at = now + (timedelta(days=remember_days) if remember else timedelta(hours=ttl_hours))
    session_doc = {
        "token_hash": token_hash(raw),
        "user_id": str(user.get("id")),
        "role": str(user.get("role") or AUTH["default_role"]),
        "allowed_apps": list(user.get("allowed_apps") or []),
        "permissions": list(user.get("permissions") or []),
        "created_at": now,
        "last_seen_at": now,
        "expires_at": expires_at,
        "revoked_at": None,
        "is_active": True,
        "source_app": source_app,
    }
    if mongo_available():
        coll("sessions_coll").insert_one(session_doc)
    return raw


def revoke_session(raw_token: str):
    if not raw_token or not mongo_available():
        return
    coll("sessions_coll").update_one(
        {"token_hash": token_hash(raw_token), "is_active": True},
        {"$set": {"is_active": False, "revoked_at": utcnow()}},
    )


def validate_session(raw_token: str) -> Optional[Dict[str, Any]]:
    if not raw_token or not mongo_available():
        return None
    doc = coll("sessions_coll").find_one({"token_hash": token_hash(raw_token), "is_active": True})
    if not doc:
        return None
    now = utcnow()
    if doc.get("revoked_at") or doc.get("expires_at") is None or doc["expires_at"] < now:
        return None
    idle_minutes = int(AUTH.get("session_idle_minutes", 120))
    last_seen = doc.get("last_seen_at") or doc.get("created_at") or now
    if last_seen + timedelta(minutes=idle_minutes) < now:
        return None
    user = get_user_by_id(str(doc.get("user_id")))
    if not user or not bool(user.get("active", True)):
        return None
    coll("sessions_coll").update_one(
        {"_id": doc["_id"]},
        {"$set": {"last_seen_at": now}},
    )
    return {
        "id": str(user.get("id")),
        "name": str(user.get("name") or user.get("id")),
        "role": str(user.get("role") or AUTH["default_role"]),
        "allowed_apps": list(user.get("allowed_apps") or []),
        "permissions": list(user.get("permissions") or []),
        "session_token": raw_token,
    }


def issue_handoff_token(user: Dict[str, Any], target_app: str) -> str:
    payload = {
        "sub": str(user.get("id")),
        "name": str(user.get("name") or user.get("id")),
        "role": str(user.get("role") or AUTH["default_role"]),
        "perms": list(user.get("permissions") or []),
        "apps": list(user.get("allowed_apps") or []),
        "app": target_app,
        "iat": int(utcnow().timestamp()),
        "exp": int((utcnow() + timedelta(minutes=10)).timestamp()),
    }
    return sign_payload(payload)


def with_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))


# =========================================================
# auth state
# =========================================================
def get_current_user() -> Optional[Dict[str, Any]]:
    if st.session_state.get("current_user"):
        return st.session_state["current_user"]

    if mongo_available():
        cookie_token = get_cookie(COOKIE_NAME)
        if cookie_token:
            user = validate_session(cookie_token)
            if user:
                st.session_state["current_user"] = user
                return user

    # 과도기 fallback: front password or ?key=
    qs_key = str(st.query_params.get("key", "") or "")
    token_secret = str(AUTH.get("token") or "")
    if token_secret and qs_key and hmac.compare_digest(qs_key, token_secret):
        fallback_user = {
            "id": "front_fallback",
            "name": "Front Fallback",
            "role": AUTH["admin_role_name"],
            "allowed_apps": list((sget("apps", default={}) or {}).keys()),
            "permissions": ["user_manage", "approve_signup", "session_manage", "ytan_admin"],
            "session_token": "",
        }
        st.session_state["current_user"] = fallback_user
        return fallback_user

    return None


def login_user(user: Dict[str, Any], remember: bool):
    if mongo_available():
        raw = create_session(user, remember=remember)
        set_cookie(COOKIE_NAME, raw, days=int(AUTH.get("remember_me_days", 3)))
        current = {
            "id": str(user.get("id")),
            "name": str(user.get("name") or user.get("id")),
            "role": str(user.get("role") or AUTH["default_role"]),
            "allowed_apps": list(user.get("allowed_apps") or []),
            "permissions": list(user.get("permissions") or []),
            "session_token": raw,
        }
    else:
        current = {
            "id": str(user.get("id")),
            "name": str(user.get("name") or user.get("id")),
            "role": str(user.get("role") or AUTH["default_role"]),
            "allowed_apps": list(user.get("allowed_apps") or []),
            "permissions": list(user.get("permissions") or []),
            "session_token": "",
        }
    st.session_state["current_user"] = current


def logout_user():
    user = st.session_state.get("current_user") or {}
    raw = str(user.get("session_token") or get_cookie(COOKIE_NAME) or "")
    if raw:
        revoke_session(raw)
    delete_cookie(COOKIE_NAME)
    st.session_state.pop("current_user", None)
    st.rerun()


def is_admin(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    if str(user.get("role")) == str(AUTH.get("admin_role_name", "admin")):
        return True
    return "user_manage" in list(user.get("permissions") or []) or "approve_signup" in list(user.get("permissions") or [])


# =========================================================
# signup / admin
# =========================================================
def submit_signup_request(name: str, login_id: str, email: str, department: str, reason: str, requested_apps: List[str]) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 요청 저장이 불가합니다."
    if not name or not login_id:
        return False, "이름과 아이디는 필수입니다."

    req = {
        "name": name.strip(),
        "login_id": login_id.strip(),
        "email": email.strip(),
        "department": department.strip(),
        "reason": reason.strip(),
        "requested_apps": requested_apps,
        "status": "pending",
        "requested_at": utcnow(),
        "reviewed_at": None,
        "reviewed_by": None,
        "review_note": None,
    }
    c = coll("signup_requests_coll")
    existing = c.find_one({"login_id": req["login_id"], "status": "pending"})
    if existing:
        return False, "이미 대기 중인 요청이 있습니다."
    c.insert_one(req)
    return True, "접근 요청이 접수되었습니다."


def get_signup_requests(status: Optional[str] = None) -> List[Dict[str, Any]]:
    if not mongo_available():
        return []
    q: Dict[str, Any] = {}
    if status:
        q["status"] = status
    return list(coll("signup_requests_coll").find(q).sort("requested_at", -1))


def approve_request(req: Dict[str, Any], admin_user: Dict[str, Any], temp_password: str):
    login_id = str(req.get("login_id"))
    user_doc = {
        "id": login_id,
        "name": str(req.get("name") or login_id),
        "email": str(req.get("email") or ""),
        "department": str(req.get("department") or ""),
        "role": AUTH["default_role"],
        "pw_hash": pbkdf2_hash_password(temp_password, PEPPER),
        "active": True,
        "allowed_apps": list(req.get("requested_apps") or []),
        "permissions": [],
        "created_at": utcnow(),
        "approved_at": utcnow(),
        "approved_by": str(admin_user.get("id")),
    }
    users_c = coll("users_coll")
    users_c.update_one({"id": login_id}, {"$set": user_doc}, upsert=True)
    coll("signup_requests_coll").update_one(
        {"_id": req["_id"]},
        {"$set": {"status": "approved", "reviewed_at": utcnow(), "reviewed_by": str(admin_user.get("id"))}},
    )


def reject_request(req_id, admin_user: Dict[str, Any], note: str):
    coll("signup_requests_coll").update_one(
        {"_id": req_id},
        {"$set": {"status": "rejected", "reviewed_at": utcnow(), "reviewed_by": str(admin_user.get("id")), "review_note": note}},
    )


def list_users() -> List[Dict[str, Any]]:
    users = []
    if mongo_available():
        users.extend(list(coll("users_coll").find({}).sort("created_at", -1)))
    seed_ids = {str(u.get("id")) for u in users}
    for u in get_seed_users_from_secrets():
        if str(u.get("id")) not in seed_ids:
            users.append(u)
    return users


def toggle_user_active(user_id: str, active: bool):
    if not mongo_available():
        return
    coll("users_coll").update_one({"id": user_id}, {"$set": {"active": active}})


# =========================================================
# UI helpers
# =========================================================
APP_META_DEFAULTS = {
    "frontgate": {"title": "🧭 드라마 포털", "desc": "통합 진입 포털"},
    "data_dashboard": {"title": "📊 데이터 대시보드", "desc": "드라마 성과데이터 한눈에 비교"},
    "ip_briefing": {"title": "📝 IP 브리핑", "desc": "IP별 상세 성과 브리핑"},
    "insightlab": {"title": "🔬 인사이트랩", "desc": "월간 리포트 및 분석 자료"},
    "yt_datacrawler": {"title": "🔭 유튜브 PGC 트래커", "desc": "유튜브 채널/영상 통계 트래킹"},
    "chatbot": {"title": "💬 댓글 분석 챗봇", "desc": "유튜브 댓글 기반 AI 분석"},
}


def apps_config() -> Dict[str, str]:
    return dict(sget("apps", default={}) or {})


def app_images() -> Dict[str, str]:
    return dict(sget("apps_img", default={}) or {})


def app_meta(key: str) -> Dict[str, str]:
    meta = APP_META_DEFAULTS.get(key, {"title": key, "desc": ""}).copy()
    try:
        sec_meta = dict(sget("apps_meta", key, default={}) or {})
        meta.update({k: v for k, v in sec_meta.items() if v})
    except Exception:
        pass
    return meta


def build_cards_html(user: Dict[str, Any], keys: List[str]) -> str:
    html_parts = []
    allowed = set(user.get("allowed_apps") or [])
    img_map = app_images()
    url_map = apps_config()

    for key in keys:
        url = str(url_map.get(key) or "").strip()
        if not url:
            continue
        meta = app_meta(key)
        img = str(img_map.get(key) or "https://images.unsplash.com/photo-1507842217343-583bb7270b66")

        can_access = is_admin(user) or (not allowed) or (key in allowed)
        final_url = with_query_param(url, "auth", issue_handoff_token(user, key)) if can_access and SIGNING_SECRET else url
        state_badge = "접근 가능" if can_access else "권한 없음"
        badge_class = "ok" if can_access else "blocked"
        href = final_url if can_access else "#"
        target = "_blank" if can_access else "_self"
        html_parts.append(
            f"""
            <a class="card-link {'disabled' if not can_access else ''}" href="{href}" target="{target}" rel="noopener noreferrer">
              <div class="card">
                <div class="thumb-wrap"><img class="thumb" src="{img}" alt="{meta['title']}"></div>
                <div class="body">
                  <div class="state-badge {badge_class}">{state_badge}</div>
                  <div class="title">{meta['title']}</div>
                  <p class="desc">{meta['desc']}</p>
                </div>
              </div>
            </a>
            """
        )
    return "".join(html_parts)


def render_card_rows(user: Dict[str, Any]):
    all_keys = [k for k in apps_config().keys() if k != "frontgate"]
    row1 = [k for k in ["data_dashboard", "ip_briefing", "insightlab"] if k in all_keys]
    row2 = [k for k in ["yt_datacrawler", "chatbot"] if k in all_keys]
    rest = [k for k in all_keys if k not in row1 + row2]
    if rest:
        row2.extend(rest)

    row1_html = build_cards_html(user, row1)
    row2_html = build_cards_html(user, row2)

    st_html(
        f"""
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8" />
        <style>
          :root {{ --card-w: 360px; --thumb-h: 220px; }}
          body {{ margin:0; padding:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto; }}
          .zone {{ margin: 8px 0 28px 0; padding: 0 6px; }}
          .zone-title {{ font-weight: 800; opacity:.88; margin: 0 0 12px 0; font-size: 1.08rem; text-align:center; }}
          .scroll-wrap {{ position: relative; }}
          .row-scroll {{ display:flex; justify-content:center; gap:24px; overflow-x:auto; overflow-y:hidden; padding:8px 4px 18px 4px; scrollbar-width:none; }}
          .row-scroll::-webkit-scrollbar {{ display:none; }}
          .card {{ position:relative; flex:0 0 var(--card-w); width:var(--card-w); background:rgba(255,255,255,.94); border:1px solid rgba(0,0,0,.06); border-radius:18px; box-shadow:0 10px 28px rgba(0,0,0,.12); overflow:hidden; transition:transform .2s ease; }}
          .card:hover {{ transform:translateY(-4px); }}
          .thumb-wrap {{ width:100%; height:var(--thumb-h); background:#0f1116; }}
          .thumb {{ width:100%; height:100%; object-fit:cover; object-position:center; display:block; }}
          .body {{ padding:14px 18px 18px 18px; }}
          .title {{ font-weight:800; font-size:1.05rem; line-height:1.25rem; margin:8px 0 6px 0; color:inherit; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
          .desc {{ margin:0; opacity:.72; font-size:.92rem; min-height:2.4em; }}
          a.card-link {{ text-decoration:none; color:inherit; display:block; }}
          a.card-link.disabled {{ pointer-events:none; opacity:.7; }}
          .state-badge {{ display:inline-block; font-size:.75rem; font-weight:700; border-radius:999px; padding:4px 10px; margin-bottom:8px; }}
          .state-badge.ok {{ background:#e9f8ef; color:#0f8a43; }}
          .state-badge.blocked {{ background:#fff1f2; color:#d92d20; }}
        </style>
        </head>
        <body>
          <div class="zone">
            <div class="zone-title">대시보드 & 인사이트</div>
            <div class="scroll-wrap"><div class="row-scroll">{row1_html}</div></div>
          </div>
          <div class="zone">
            <div class="zone-title">데이터 분석 도구</div>
            <div class="scroll-wrap"><div class="row-scroll">{row2_html}</div></div>
          </div>
        </body>
        </html>
        """,
        height=860,
        scrolling=False,
    )


def render_header(user: Optional[Dict[str, Any]]):
    st.markdown(
        """
        <style>
          .grad-title {
            font-weight: 900;
            font-size: clamp(28px, 4vw, 42px);
            line-height: 1.15;
            margin: 4px 0 2px 0;
            background: linear-gradient(90deg, #6757e7 0%, #9B72CB 35%, #ff7bb0 70%, #ff8a4d 100%);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            letter-spacing: 0.2px;
            text-align: center;
          }
          .grad-sub { text-align: center; opacity: .72; margin-top: 2px; }
          .top-userbox {
            display:flex; justify-content:center; gap:8px; align-items:center; margin:12px 0 4px 0;
            font-size:.95rem; opacity:.85;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("<div class='grad-title'>드라마 데이터 포털</div>", unsafe_allow_html=True)
    st.markdown("<div class='grad-sub'>문의: 미디어)마케팅팀 데이터인사이트파트</div>", unsafe_allow_html=True)
    if user:
        st.markdown(
            f"<div class='top-userbox'><span>👤 {user.get('name')}</span><span>·</span><span>{user.get('role')}</span></div>",
            unsafe_allow_html=True,
        )
    st.write("")


def render_login_panel():
    st.markdown("### 🔐 포털 로그인")
    fallback = str(AUTH.get("frontpage_password") or "")
    with st.form("login_form", clear_on_submit=False):
        login_id = st.text_input("아이디", placeholder="예: hbkim")
        password = st.text_input("비밀번호", type="password")
        remember = st.checkbox("로그인 상태 유지", value=True)
        submitted = st.form_submit_button("로그인", use_container_width=True)
    if submitted:
        ok, user, msg = authenticate_user(login_id.strip(), password)
        if ok and user:
            login_user(user, remember=remember)
            st.success("로그인되었습니다.")
            st.rerun()
        elif fallback and not login_id.strip() and hmac.compare_digest(password, fallback):
            pseudo_user = {
                "id": "front_fallback",
                "name": "임시 관리자",
                "role": AUTH["admin_role_name"],
                "active": True,
                "allowed_apps": list(apps_config().keys()),
                "permissions": ["user_manage", "approve_signup", "session_manage", "ytan_admin"],
            }
            login_user(pseudo_user, remember=remember)
            st.success("과도기 비밀번호로 입장했습니다.")
            st.rerun()
        else:
            st.error(msg or "로그인에 실패했습니다.")


def render_signup_panel():
    st.markdown("### 📨 접근 요청")
    app_keys = [k for k in apps_config().keys() if k != "frontgate"]
    labels = {k: app_meta(k)["title"] for k in app_keys}
    with st.form("signup_request_form", clear_on_submit=True):
        name = st.text_input("이름 *")
        login_id = st.text_input("희망 아이디 *")
        email = st.text_input("이메일")
        department = st.text_input("부서")
        requested_apps = st.multiselect("사용 희망 서비스", app_keys, format_func=lambda x: labels.get(x, x))
        reason = st.text_area("사용 목적", height=120, placeholder="예: tvN 드라마 성과 모니터링 및 리포트 참고")
        submitted = st.form_submit_button("접근 요청 보내기", use_container_width=True)
    if submitted:
        ok, msg = submit_signup_request(name, login_id, email, department, reason, requested_apps)
        (st.success if ok else st.error)(msg)


def render_admin_panel(admin_user: Dict[str, Any]):
    st.markdown("### 🛠 관리자")
    tab1, tab2 = st.tabs(["가입 요청", "사용자 목록"])

    with tab1:
        reqs = get_signup_requests()
        if not reqs:
            st.info("대기 중인 요청이 없습니다.")
        for req in reqs:
            with st.expander(f"[{req.get('status')}] {req.get('name')} · {req.get('login_id')}"):
                st.write(f"- 이메일: {req.get('email') or '-'}")
                st.write(f"- 부서: {req.get('department') or '-'}")
                st.write(f"- 요청 앱: {', '.join(req.get('requested_apps') or []) or '-'}")
                st.write(f"- 사유: {req.get('reason') or '-'}")
                st.write(f"- 요청시각: {req.get('requested_at')}")
                if req.get("status") == "pending":
                    c1, c2 = st.columns(2)
                    with c1:
                        temp_pw = st.text_input("승인 시 임시 비밀번호", key=f"temp_pw_{req['_id']}", placeholder="임시 비밀번호 입력")
                        if st.button("승인", key=f"approve_{req['_id']}", use_container_width=True):
                            if not temp_pw.strip():
                                st.error("임시 비밀번호를 입력하세요.")
                            else:
                                approve_request(req, admin_user, temp_pw.strip())
                                st.success("승인 완료")
                                st.rerun()
                    with c2:
                        note = st.text_input("반려 메모", key=f"rej_note_{req['_id']}")
                        if st.button("반려", key=f"reject_{req['_id']}", use_container_width=True):
                            reject_request(req["_id"], admin_user, note)
                            st.success("반려 처리 완료")
                            st.rerun()

    with tab2:
        users = list_users()
        if not users:
            st.info("사용자가 없습니다.")
        for u in users:
            uid = str(u.get("id"))
            with st.expander(f"{u.get('name') or uid} · {uid}"):
                st.write(f"- role: {u.get('role', '-')}")
                st.write(f"- active: {u.get('active', True)}")
                st.write(f"- allowed_apps: {', '.join(u.get('allowed_apps') or []) or '-'}")
                st.write(f"- permissions: {', '.join(u.get('permissions') or []) or '-'}")
                if mongo_available() and uid != str(admin_user.get("id")):
                    currently_active = bool(u.get("active", True))
                    label = "비활성화" if currently_active else "활성화"
                    if st.button(label, key=f"toggle_{uid}"):
                        toggle_user_active(uid, not currently_active)
                        st.success("상태가 변경되었습니다.")
                        st.rerun()


# =========================================================
# main
# =========================================================
user = get_current_user()
render_header(user)

if not mongo_available():
    st.warning("MongoDB 연결이 없어 중앙 세션/가입요청/관리자 기능 일부가 제한됩니다. 통합 secrets의 [mongo].uri를 확인하세요.")
if not SIGNING_SECRET:
    st.warning("auth.signing_secret 이 비어 있어 세부 앱으로 전달되는 auth 토큰이 비서명 상태입니다. 통합 secrets를 확인하세요.")
if not PEPPER:
    st.warning("auth.pepper 가 비어 있어 계정 비밀번호 검증이 정상 동작하지 않을 수 있습니다.")

if user is None:
    c1, c2 = st.columns([1, 1])
    with c1:
        render_login_panel()
    with c2:
        render_signup_panel()
    st.stop()

# top actions
left, right = st.columns([8, 2])
with left:
    st.caption("로그인 완료. 권한이 있는 서비스만 활성화되어 보입니다.")
with right:
    if st.button("로그아웃", use_container_width=True):
        logout_user()

render_card_rows(user)

if is_admin(user):
    st.divider()
    render_admin_panel(user)

st.markdown("<hr style='margin-top:30px; opacity:.2;'>", unsafe_allow_html=True)
st.markdown("<p style='text-align:center; opacity:.65;'>© 드라마 데이터 포털</p>", unsafe_allow_html=True)
