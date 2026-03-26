from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
import os
import time
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
    from streamlit_js_eval import streamlit_js_eval
except Exception:
    streamlit_js_eval = None

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
st.set_page_config(page_title="드라마 마케팅 대시보드", page_icon="🧭", layout="wide", initial_sidebar_state="collapsed")


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
    cfg.setdefault("local_storage_key", "drama_portal_auth")
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
LOCAL_STORAGE_KEY = str(AUTH.get("local_storage_key") or "drama_portal_auth")
SIGNING_SECRET = str(AUTH.get("signing_secret") or "")
PEPPER = str(AUTH.get("pepper") or "")


ROLE_MASTER = "master"
ROLE_ADMIN = "admin"
ROLE_TEAM_MEMBER = "team_member"
ROLE_USER = "user"
ROLE_OPTIONS = [ROLE_MASTER, ROLE_ADMIN, ROLE_TEAM_MEMBER, ROLE_USER]
INITIAL_MASTER_NAME = "김호범"


def normalize_role(role: Optional[str], name: Optional[str] = None) -> str:
    if str(name or "").strip() == INITIAL_MASTER_NAME:
        return ROLE_MASTER
    role = str(role or "").strip().lower()
    if role in {"team", "teammember", "team_member", "member"}:
        return ROLE_TEAM_MEMBER
    if role in ROLE_OPTIONS:
        return role
    admin_role = str(AUTH.get("admin_role_name", ROLE_ADMIN) or ROLE_ADMIN).strip().lower()
    if role == admin_role:
        return ROLE_ADMIN
    default_role = str(AUTH.get("default_role", ROLE_USER) or ROLE_USER).strip().lower()
    if default_role in ROLE_OPTIONS:
        return default_role
    return ROLE_USER


def role_rank(role: Optional[str], name: Optional[str] = None) -> int:
    order = {
        ROLE_USER: 0,
        ROLE_TEAM_MEMBER: 1,
        ROLE_ADMIN: 2,
        ROLE_MASTER: 3,
    }
    return order.get(normalize_role(role, name=name), 0)


def is_master(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    return normalize_role(user.get("role"), user.get("name")) == ROLE_MASTER


def can_manage_role(actor: Optional[Dict[str, Any]], target_role: Optional[str], target_name: Optional[str] = None) -> bool:
    actor_role = normalize_role((actor or {}).get("role"), (actor or {}).get("name"))
    desired_role = normalize_role(target_role, target_name)
    if actor_role == ROLE_MASTER:
        return True
    if actor_role != ROLE_ADMIN:
        return False
    return desired_role in {ROLE_ADMIN, ROLE_TEAM_MEMBER, ROLE_USER}


def can_manage_user(actor: Optional[Dict[str, Any]], target_user: Optional[Dict[str, Any]]) -> bool:
    actor_role = normalize_role((actor or {}).get("role"), (actor or {}).get("name"))
    target_role = normalize_role((target_user or {}).get("role"), (target_user or {}).get("name"))
    if actor_role == ROLE_MASTER:
        return True
    if actor_role != ROLE_ADMIN:
        return False
    return target_role in {ROLE_TEAM_MEMBER, ROLE_USER}


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


def inject_local_storage_set(token: str, seq: Optional[str] = None):
    safe_token = json.dumps(token or "")
    safe_key = json.dumps(LOCAL_STORAGE_KEY)
    nonce = json.dumps(seq or str(uuid.uuid4()))
    st_html(
        f"""
        <script>
        try {{
            window.localStorage.setItem({safe_key}, {safe_token});
            window.localStorage.setItem("__frontgate_ls_nonce__", {nonce});
        }} catch (e) {{}}
        </script>
        """,
        height=0,
    )


def inject_local_storage_remove(seq: Optional[str] = None):
    safe_key = json.dumps(LOCAL_STORAGE_KEY)
    nonce = json.dumps(seq or str(uuid.uuid4()))
    st_html(
        f"""
        <script>
        try {{
            window.localStorage.removeItem({safe_key});
            window.localStorage.setItem("__frontgate_ls_nonce__", {nonce});
        }} catch (e) {{}}
        </script>
        """,
        height=0,
    )


def inject_browser_session_set(token: str, days: int = 3):
    safe_token = json.dumps(token or "")
    safe_key = json.dumps(LOCAL_STORAGE_KEY)
    cookie_name = json.dumps(COOKIE_NAME)
    max_age = int(max(days, 1) * 86400)
    st_html(
        f"""
        <script>
        try {{
            window.localStorage.setItem({safe_key}, {safe_token});
            const encoded = encodeURIComponent({safe_token});
            document.cookie = {cookie_name} + "=" + encoded + "; path=/; max-age={max_age}; SameSite=Lax; Secure";
        }} catch (e) {{}}
        </script>
        """,
        height=0,
    )


def inject_browser_session_remove():
    safe_key = json.dumps(LOCAL_STORAGE_KEY)
    cookie_name = json.dumps(COOKIE_NAME)
    st_html(
        f"""
        <script>
        try {{
            window.localStorage.removeItem({safe_key});
            document.cookie = {cookie_name} + "=; path=/; max-age=0; SameSite=Lax; Secure";
        }} catch (e) {{}}
        </script>
        """,
        height=0,
    )


def get_local_storage_token(seq: Optional[str] = None) -> str:
    if streamlit_js_eval is None:
        return ""
    try:
        key = f"frontgate_local_storage_token_{seq or uuid.uuid4().hex}"
        value = streamlit_js_eval(
            js_expressions=f"window.localStorage.getItem({json.dumps(LOCAL_STORAGE_KEY)})",
            key=key,
        )
        return str(value or "")
    except Exception:
        return ""


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


def ensure_mongo_indexes():
    if not mongo_available():
        return
    if st.session_state.get("_frontgate_indexes_ready"):
        return
    try:
        sessions = coll("sessions_coll")
        if sessions is not None:
            sessions.create_index("token_hash", unique=True, name="token_hash_unique")
            sessions.create_index("user_id", name="user_id_idx")
            sessions.create_index("delete_after", expireAfterSeconds=0, name="delete_after_ttl")
        users = coll("users_coll")
        if users is not None:
            users.create_index("id", unique=True, name="user_id_unique")
        reqs = coll("signup_requests_coll")
        if reqs is not None:
            reqs.create_index([("login_id", 1), ("status", 1)], name="signup_login_status_idx")
        st.session_state["_frontgate_indexes_ready"] = True
    except Exception:
        pass


# =========================================================
# crypto / password
# =========================================================
def utcnow() -> datetime:
    return datetime.now(UTC)




def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

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
        u["role"] = normalize_role(u.get("role"), u.get("name"))
        normalized.append(u)
    return normalized


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    if mongo_available():
        c = coll("users_coll")
        user = c.find_one({"id": user_id})
        if user:
            user["role"] = normalize_role(user.get("role"), user.get("name"))
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
            user = dict(user)
            user["role"] = normalize_role(user.get("role"), user.get("name"))
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
        "role": normalize_role(user.get("role"), user.get("name")),
        "allowed_apps": list(user.get("allowed_apps") or []),
        "permissions": list(user.get("permissions") or []),
        "created_at": now,
        "last_seen_at": now,
        "expires_at": expires_at,
        "revoked_at": None,
        "is_active": True,
        "source_app": source_app,
        "delete_after": expires_at + timedelta(days=1),
    }
    if mongo_available():
        sessions = coll("sessions_coll")
        sessions.update_many(
            {"user_id": str(user.get("id")), "is_active": True},
            {"$set": {"is_active": False, "revoked_at": now, "delete_after": now + timedelta(days=1)}},
        )
        sessions.insert_one(session_doc)
    return raw


def revoke_session(raw_token: str):
    if not raw_token or not mongo_available():
        return
    now = utcnow()
    coll("sessions_coll").update_one(
        {"token_hash": token_hash(raw_token), "is_active": True},
        {"$set": {"is_active": False, "revoked_at": now, "delete_after": now + timedelta(days=1)}},
    )


def validate_session(raw_token: str) -> Optional[Dict[str, Any]]:
    if not raw_token or not mongo_available():
        return None
    sessions = coll("sessions_coll")
    doc = sessions.find_one({"token_hash": token_hash(raw_token), "is_active": True})
    if not doc:
        return None
    now = utcnow()
    expires_at = ensure_utc(doc.get("expires_at"))
    revoked_at = ensure_utc(doc.get("revoked_at"))
    if revoked_at or expires_at is None or expires_at < now:
        sessions.update_one({"_id": doc["_id"]}, {"$set": {"is_active": False, "revoked_at": now, "delete_after": now + timedelta(days=1)}})
        return None
    idle_minutes = int(AUTH.get("session_idle_minutes", 120))
    last_seen = ensure_utc(doc.get("last_seen_at")) or ensure_utc(doc.get("created_at")) or now
    if last_seen + timedelta(minutes=idle_minutes) < now:
        sessions.update_one({"_id": doc["_id"]}, {"$set": {"is_active": False, "revoked_at": now, "delete_after": now + timedelta(days=1)}})
        return None
    user = get_user_by_id(str(doc.get("user_id")))
    if not user or not bool(user.get("active", True)):
        sessions.update_one({"_id": doc["_id"]}, {"$set": {"is_active": False, "revoked_at": now, "delete_after": now + timedelta(days=1)}})
        return None
    sessions.update_one(
        {"_id": doc["_id"]},
        {"$set": {"last_seen_at": now}},
    )
    return {
        "id": str(user.get("id")),
        "name": str(user.get("name") or user.get("id")),
        "role": normalize_role(user.get("role"), user.get("name")),
        "allowed_apps": list(user.get("allowed_apps") or []),
        "permissions": list(user.get("permissions") or []),
        "session_token": raw_token,
    }


def issue_handoff_token(user: Dict[str, Any], target_app: str) -> str:
    session_token = str(user.get("session_token") or "")
    remember_days = int(AUTH.get("remember_me_days", 3) or 3)
    ttl_hours = int(AUTH.get("session_ttl_hours", 12) or 12)
    expires_at = utcnow() + (timedelta(days=remember_days) if session_token else timedelta(hours=ttl_hours))
    payload = {
        "sub": str(user.get("id")),
        "name": str(user.get("name") or user.get("id")),
        "role": str(user.get("role") or AUTH["default_role"]),
        "perms": list(user.get("permissions") or []),
        "apps": list(user.get("allowed_apps") or []),
        "app": target_app,
        "st": session_token,
        "iat": int(utcnow().timestamp()),
        "exp": int(expires_at.timestamp()),
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
        # 하드 새로고침 직후엔 쿠키/로컬스토리지가 첫 렌더에서 늦게 들어올 수 있어
        # 최대 2회까지 부트스트랩 rerun 후 복구를 시도한다.
        bootstrap_count = int(st.session_state.get("_restore_bootstrap_count", 0) or 0)

        cookie_token = get_cookie(COOKIE_NAME)
        if cookie_token:
            user = validate_session(cookie_token)
            if user:
                st.session_state["current_user"] = user
                st.session_state["_restore_bootstrap_count"] = 0
                st.session_state["_show_restore_splash"] = False
                st.session_state.pop("_pending_login_finalize", None)
                return user

        ls_token = get_local_storage_token(seq=f"boot_{bootstrap_count}")
        if ls_token and ls_token not in ("null", "undefined"):
            user = validate_session(ls_token)
            if user:
                # localStorage에만 남아 있어도 쿠키를 재세팅해서 이후 복구를 안정화
                set_cookie(COOKIE_NAME, ls_token, days=int(AUTH.get("remember_me_days", 3)))
                st.session_state["current_user"] = user
                st.session_state["_frontgate_ls_synced"] = ls_token
                st.session_state["_restore_bootstrap_count"] = 0
                st.session_state["_show_restore_splash"] = False
                st.session_state.pop("_pending_login_finalize", None)
                return user

        if bootstrap_count < 2:
            st.session_state["_restore_bootstrap_count"] = bootstrap_count + 1
            st.session_state["_show_restore_splash"] = True
            get_cookie_manager()
            get_local_storage_token(seq=f"bootstrap_{bootstrap_count}")
            st.rerun()
        st.session_state["_show_restore_splash"] = False

    # 과도기 fallback: front password or ?key=
    qs_key = str(st.query_params.get("key", "") or "")
    token_secret = str(AUTH.get("token") or "")
    if token_secret and qs_key and hmac.compare_digest(qs_key, token_secret):
        fallback_user = {
            "id": "front_fallback",
            "name": "Front Fallback",
            "role": ROLE_MASTER,
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
        current = {
            "id": str(user.get("id")),
            "name": str(user.get("name") or user.get("id")),
            "role": normalize_role(user.get("role"), user.get("name")),
            "allowed_apps": list(user.get("allowed_apps") or []),
            "permissions": list(user.get("permissions") or []),
            "session_token": raw,
        }
    else:
        current = {
            "id": str(user.get("id")),
            "name": str(user.get("name") or user.get("id")),
            "role": normalize_role(user.get("role"), user.get("name")),
            "allowed_apps": list(user.get("allowed_apps") or []),
            "permissions": list(user.get("permissions") or []),
            "session_token": "",
        }
    st.session_state["current_user"] = current
    st.session_state["_frontgate_restore_attempts"] = 0
    if current.get("session_token"):
        st.session_state["_pending_login_finalize"] = {
            "token": str(current.get("session_token")),
            "days": int(AUTH.get("remember_me_days", 3)) if remember else 1,
        }
        st.session_state["_pending_login_stage"] = 0
        st.session_state["_frontgate_ls_synced"] = str(current.get("session_token"))


def logout_user():
    user = st.session_state.get("current_user") or {}
    raw = str(user.get("session_token") or get_cookie(COOKIE_NAME) or "")
    if raw:
        revoke_session(raw)
    delete_cookie(COOKIE_NAME)
    inject_browser_session_remove()
    st.session_state.pop("current_user", None)
    st.session_state.pop("_pending_login_finalize", None)
    st.session_state.pop("_pending_login_stage", None)
    st.session_state["_frontgate_restore_attempts"] = 0
    st.session_state["_frontgate_ls_synced"] = ""
    st.session_state["_show_restore_splash"] = False
    st.rerun()


def is_admin(user: Optional[Dict[str, Any]]) -> bool:
    if not user:
        return False
    role = normalize_role(user.get("role"), user.get("name"))
    if role in {ROLE_MASTER, ROLE_ADMIN}:
        return True
    return "user_manage" in list(user.get("permissions") or []) or "approve_signup" in list(user.get("permissions") or [])


def has_permission(user: Optional[Dict[str, Any]], permission: str) -> bool:
    if not user:
        return False
    if is_admin(user):
        return True
    return permission in list(user.get("permissions") or [])


# =========================================================
# signup / admin
# =========================================================
def submit_signup_request(name: str, login_id: str, password: str, password_confirm: str, email: str, department: str, reason: str, requested_apps: List[str]) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 요청 저장이 불가합니다."
    if not name or not login_id:
        return False, "이름과 아이디는 필수입니다."
    if not password or not password_confirm:
        return False, "비밀번호와 비밀번호 확인을 입력하세요."
    if password != password_confirm:
        return False, "비밀번호 확인이 일치하지 않습니다."
    if len(password) < 4:
        return False, "비밀번호는 4자 이상으로 입력하세요."

    login_id = login_id.strip()
    users_c = coll("users_coll")
    if users_c.find_one({"id": login_id}):
        return False, "이미 사용 중인 아이디입니다."

    req = {
        "type": "signup",
        "name": name.strip(),
        "login_id": login_id,
        "pw_hash": pbkdf2_hash_password(password, PEPPER),
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
    return True, "권한 요청이 접수되었습니다. 승인 후 해당 비밀번호로 로그인할 수 있습니다."


def get_signup_requests(status: Optional[str] = None) -> List[Dict[str, Any]]:
    if not mongo_available():
        return []
    q: Dict[str, Any] = {}
    if status:
        q["status"] = status
    return list(coll("signup_requests_coll").find(q).sort("requested_at", -1))


def approve_request(req: Dict[str, Any], admin_user: Dict[str, Any], allowed_apps: List[str], role: Optional[str] = None, permissions: Optional[List[str]] = None):
    login_id = str(req.get("login_id"))
    user_doc = {
        "id": login_id,
        "name": str(req.get("name") or login_id),
        "email": str(req.get("email") or ""),
        "department": str(req.get("department") or ""),
        "role": normalize_role(role or AUTH["default_role"], str(req.get("name") or login_id)),
        "pw_hash": str(req.get("pw_hash") or ""),
        "active": True,
        "allowed_apps": list(allowed_apps or []),
        "permissions": list(permissions or []),
        "created_at": utcnow(),
        "approved_at": utcnow(),
        "approved_by": str(admin_user.get("id")),
    }
    users_c = coll("users_coll")
    users_c.update_one({"id": login_id}, {"$set": user_doc}, upsert=True)
    coll("signup_requests_coll").update_one(
        {"_id": req["_id"]},
        {"$set": {
            "status": "approved",
            "reviewed_at": utcnow(),
            "reviewed_by": str(admin_user.get("id")),
            "approved_apps": list(allowed_apps or []),
            "approved_role": normalize_role(role or AUTH["default_role"], req.get("name")),
            "approved_permissions": list(permissions or []),
        }},
    )


def reject_request(req_id, admin_user: Dict[str, Any], note: str):
    coll("signup_requests_coll").update_one(
        {"_id": req_id},
        {"$set": {"status": "rejected", "reviewed_at": utcnow(), "reviewed_by": str(admin_user.get("id")), "review_note": note}},
    )


def submit_password_reset_request(login_id: str, name: str, email: str, reason: str) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 요청 저장이 불가합니다."
    login_id = (login_id or "").strip()
    if not login_id:
        return False, "아이디를 입력하세요."
    user = get_user_by_id(login_id)
    if not user:
        return False, "존재하지 않는 계정입니다."
    c = coll("signup_requests_coll")
    existing = c.find_one({"type": "password_reset", "login_id": login_id, "status": {"$in": ["pending", "approved"]}})
    if existing:
        return False, "이미 처리 중인 비밀번호 재설정 요청이 있습니다."
    c.insert_one({
        "type": "password_reset",
        "login_id": login_id,
        "name": (name or "").strip(),
        "email": (email or "").strip(),
        "reason": (reason or "").strip(),
        "status": "pending",
        "requested_at": utcnow(),
        "reviewed_at": None,
        "reviewed_by": None,
        "review_note": None,
    })
    return True, "비밀번호 재설정 요청이 접수되었습니다. 관리자 승인 후 로그인 화면에서 새 비밀번호를 설정할 수 있습니다."


def approve_password_reset_request(req: Dict[str, Any], admin_user: Dict[str, Any]):
    login_id = str(req.get("login_id") or "")
    if not mongo_available() or not login_id:
        return
    coll("users_coll").update_one(
        {"id": login_id},
        {"$set": {"must_change_password": True, "password_reset_approved_at": utcnow(), "password_reset_approved_by": str(admin_user.get("id"))}},
    )
    coll("signup_requests_coll").update_one(
        {"_id": req["_id"]},
        {"$set": {"status": "approved", "reviewed_at": utcnow(), "reviewed_by": str(admin_user.get("id"))}},
    )


def complete_password_reset(login_id: str, new_password: str, password_confirm: str) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 비밀번호 변경이 불가합니다."
    login_id = (login_id or "").strip()
    if not login_id or not new_password or not password_confirm:
        return False, "아이디와 새 비밀번호를 모두 입력하세요."
    if new_password != password_confirm:
        return False, "비밀번호 확인이 일치하지 않습니다."
    if len(new_password) < 4:
        return False, "비밀번호는 4자 이상으로 입력하세요."
    user = coll("users_coll").find_one({"id": login_id})
    if not user:
        return False, "존재하지 않는 계정입니다."
    if not bool(user.get("must_change_password", False)):
        approved_reset = coll("signup_requests_coll").find_one({"type": "password_reset", "login_id": login_id, "status": "approved"})
        if not approved_reset:
            return False, "관리자 승인된 비밀번호 재설정 요청이 없습니다."
    coll("users_coll").update_one(
        {"id": login_id},
        {"$set": {"pw_hash": pbkdf2_hash_password(new_password, PEPPER), "must_change_password": False, "password_updated_at": utcnow()}, "$unset": {"password_reset_approved_at": "", "password_reset_approved_by": ""}},
    )
    coll("signup_requests_coll").update_many(
        {"type": "password_reset", "login_id": login_id, "status": "approved"},
        {"$set": {"status": "completed", "completed_at": utcnow()}},
    )
    return True, "새 비밀번호가 설정되었습니다. 이제 새 비밀번호로 로그인하세요."


def create_user_by_admin(admin_user: Dict[str, Any], login_id: str, name: str, password: str, password_confirm: str, role: str, allowed_apps: List[str], permissions: List[str], email: str, department: str) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 사용자 생성이 불가합니다."
    login_id = (login_id or "").strip()
    name = (name or "").strip()
    if not login_id or not name:
        return False, "아이디와 이름은 필수입니다."
    if not password or password != password_confirm:
        return False, "비밀번호와 비밀번호 확인이 일치해야 합니다."
    if len(password) < 4:
        return False, "비밀번호는 4자 이상으로 입력하세요."
    users_c = coll("users_coll")
    if users_c.find_one({"id": login_id}):
        return False, "이미 사용 중인 아이디입니다."
    users_c.insert_one({
        "id": login_id,
        "name": name,
        "email": (email or "").strip(),
        "department": (department or "").strip(),
        "role": normalize_role(role or AUTH["default_role"], name),
        "pw_hash": pbkdf2_hash_password(password, PEPPER),
        "active": True,
        "allowed_apps": list(allowed_apps or []),
        "permissions": list(permissions or []),
        "must_change_password": False,
        "created_at": utcnow(),
        "approved_at": utcnow(),
        "approved_by": str(admin_user.get("id")),
        "created_by": str(admin_user.get("id")),
    })
    return True, "사용자가 생성되었습니다."


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


def update_user_access(actor_user: Dict[str, Any], user_id: str, allowed_apps: List[str], role: Optional[str] = None, permissions: Optional[List[str]] = None) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 사용자 수정이 불가합니다."
    target_user = coll("users_coll").find_one({"id": user_id}) or get_user_by_id(user_id)
    if not target_user:
        return False, "대상 사용자를 찾을 수 없습니다."
    target_user = dict(target_user)
    if not can_manage_user(actor_user, target_user):
        return False, "현재 권한으로는 이 사용자를 수정할 수 없습니다."
    desired_role = normalize_role(role if role is not None else target_user.get("role"), target_user.get("name"))
    if not can_manage_role(actor_user, desired_role, target_user.get("name")):
        return False, "현재 권한으로는 해당 권한 등급으로 변경할 수 없습니다."
    update_doc = {"allowed_apps": list(allowed_apps or [])}
    if role is not None:
        update_doc["role"] = desired_role
    if permissions is not None:
        update_doc["permissions"] = list(permissions or [])
    coll("users_coll").update_one({"id": user_id}, {"$set": update_doc})
    return True, "접근 권한이 저장되었습니다."


def transfer_master(actor_user: Dict[str, Any], target_user_id: str) -> Tuple[bool, str]:
    if not mongo_available():
        return False, "MongoDB가 설정되지 않아 마스터 이양이 불가합니다."
    if not is_master(actor_user):
        return False, "마스터만 마스터 권한을 이양할 수 있습니다."
    actor_id = str(actor_user.get("id") or "")
    target_user = coll("users_coll").find_one({"id": target_user_id}) or get_user_by_id(target_user_id)
    if not target_user:
        return False, "대상 사용자를 찾을 수 없습니다."
    target_user = dict(target_user)
    if actor_id == str(target_user.get("id") or ""):
        return False, "이미 현재 마스터 계정입니다."
    now = utcnow()
    coll("users_coll").update_one(
        {"id": actor_id},
        {"$set": {"role": ROLE_ADMIN, "updated_at": now, "updated_by": actor_id}},
    )
    coll("users_coll").update_one(
        {"id": str(target_user.get("id") or "")},
        {"$set": {"role": ROLE_MASTER, "updated_at": now, "updated_by": actor_id}},
    )
    if st.session_state.get("current_user") and str(st.session_state["current_user"].get("id") or "") == actor_id:
        st.session_state["current_user"]["role"] = ROLE_ADMIN
    return True, f"{target_user.get('name') or target_user_id} 계정으로 마스터 권한을 이양했습니다."


def format_dt_kst(dt: Any, default: str = "-") -> str:
    parsed = ensure_utc(dt)
    if parsed is None:
        return default
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def get_user_session_summary(user_id: str) -> Dict[str, Any]:
    summary = {
        "active_session_count": 0,
        "last_seen_at": None,
        "last_source_app": None,
    }
    if not mongo_available():
        return summary

    sessions = coll("sessions_coll")
    if sessions is None:
        return summary

    now = utcnow()
    idle_minutes = int(AUTH.get("session_idle_minutes", 120) or 120)
    active_query = {
        "user_id": str(user_id),
        "is_active": True,
        "revoked_at": None,
        "expires_at": {"$gte": now},
        "$or": [
            {"last_seen_at": {"$gte": now - timedelta(minutes=idle_minutes)}},
            {
                "$and": [
                    {"last_seen_at": None},
                    {"created_at": {"$gte": now - timedelta(minutes=idle_minutes)}},
                ]
            },
        ],
    }
    try:
        summary["active_session_count"] = int(sessions.count_documents(active_query))
        latest_doc = sessions.find_one(
            {"user_id": str(user_id)},
            sort=[("last_seen_at", -1), ("created_at", -1)],
        )
        if latest_doc:
            summary["last_seen_at"] = latest_doc.get("last_seen_at") or latest_doc.get("created_at")
            summary["last_source_app"] = latest_doc.get("source_app") or "-"
    except Exception:
        return summary
    return summary


# =========================================================
# UI helpers
# =========================================================
APP_META_DEFAULTS = {
    "frontgate": {"title": "🧭 드라마 포털", "desc": "통합 진입 포털"},
    "data_dashboard": {"title": "📊 데이터 대시보드", "desc": "드라마 성과데이터 한눈에 비교하기"},
    "ip_briefing": {"title": "📝 주간 IP시청자 브리핑", "desc": "IP별 주간 시청자반응 브리핑"},
    "insightlab": {"title": "🔬 드라마 인사이트랩", "desc": "드라마 관련 다양한 인사이트 보고서"},
    "chatbot": {"title": "💬 유튜브 댓글 분석 AI챗봇", "desc": "드라마 유튜브 반응 AI분석/심층대화"},
    "yt_datacrawler": {"title": "🔭 유튜브 데이터 트래커", "desc": "PGC영상의 상세통계 확인하기"},

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


def clean_allowed_apps(values: Optional[List[str]], options: List[str]) -> List[str]:
    valid = set(options)
    return [v for v in list(values or []) if v in valid]


def get_admin_page() -> str:
    page = str(st.session_state.get("admin_page") or "")
    return page if page in {"signup_requests", "password_resets", "member_management"} else ""


def set_admin_page(page: str):
    st.session_state["admin_page"] = page



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

        can_access = is_admin(user) or (key in allowed)
        public_apps = {"ip_briefing", "insightlab"}
        needs_auth_handoff = key not in public_apps
        final_url = (
            with_query_param(url, "auth", issue_handoff_token(user, key))
            if can_access and SIGNING_SECRET and needs_auth_handoff
            else url
        )
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
    row2 = [k for k in [ "chatbot", "yt_datacrawler"] if k in all_keys]
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
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("<div class='grad-title'>드라마 마케팅 대시보드</div>", unsafe_allow_html=True)
    st.markdown("<div class='grad-sub'>문의: 미디어)마케팅팀 데이터인사이트파트</div>", unsafe_allow_html=True)
    st.write("")






def render_restore_splash(message: str = "로그인 상태를 확인하고 있습니다."):
    st.markdown("### 사이트 접속 중")
    st.info(message)


def render_login_finalize():
    pending = st.session_state.get("_pending_login_finalize") or {}
    token = str(pending.get("token") or "")
    if not token:
        return False
    days = int(pending.get("days") or int(AUTH.get("remember_me_days", 3) or 3))
    stage = int(st.session_state.get("_pending_login_stage", 0) or 0)
    st.markdown("### 로그인 중")
    st.info("사이트에 접속하고 있습니다. 잠시만 기다려 주세요.")
    if stage == 0:
        inject_browser_session_set(token, days=days)
        st.session_state["_pending_login_stage"] = 1
        time.sleep(1.0)
        st.rerun()
    st.session_state.pop("_pending_login_finalize", None)
    st.session_state.pop("_pending_login_stage", None)
    st.rerun()

def render_login_panel():
    st.markdown("### 🔐 포털 로그인")
    with st.form("login_form", clear_on_submit=False):
        login_id = st.text_input("아이디", placeholder="아이디")
        password = st.text_input("비밀번호", type="password")
        remember = st.checkbox("로그인 상태 유지", value=True)
        submitted = st.form_submit_button("로그인", use_container_width=True)
    if submitted:
        ok, user, msg = authenticate_user(login_id.strip(), password)
        if ok and user:
            if bool(user.get("must_change_password", False)):
                st.session_state["reset_target_login_id"] = str(user.get("id"))
                st.info("관리자 승인이 완료되었습니다. 아래 '새 비밀번호 설정'에서 비밀번호를 다시 설정해 주세요.")
            else:
                login_user(user, remember=remember)
                st.rerun()
        else:
            st.error(msg or "로그인에 실패했습니다.")

    with st.expander("비밀번호 재설정 요청", expanded=False):
        with st.form("pw_reset_request_form", clear_on_submit=True):
            login_id = st.text_input("아이디 *", key="pw_reset_req_login")
            name = st.text_input("이름", key="pw_reset_req_name")
            email = st.text_input("이메일", key="pw_reset_req_email")
            reason = st.text_area("요청 메모", key="pw_reset_req_reason", height=90, placeholder="예: 비밀번호 분실")
            submitted = st.form_submit_button("재설정 요청 보내기", use_container_width=True)
        if submitted:
            ok, msg = submit_password_reset_request(login_id, name, email, reason)
            (st.success if ok else st.error)(msg)

    with st.expander("새 비밀번호 설정", expanded=bool(st.session_state.get("reset_target_login_id"))):
        default_login = st.session_state.get("reset_target_login_id", "")
        with st.form("pw_reset_complete_form", clear_on_submit=True):
            login_id = st.text_input("아이디 *", value=default_login, key="pw_reset_complete_login")
            new_password = st.text_input("새 비밀번호 *", type="password")
            password_confirm = st.text_input("새 비밀번호 확인 *", type="password")
            submitted = st.form_submit_button("비밀번호 변경", use_container_width=True)
        if submitted:
            ok, msg = complete_password_reset(login_id, new_password, password_confirm)
            if ok:
                st.session_state.pop("reset_target_login_id", None)
            (st.success if ok else st.error)(msg)


def render_signup_panel():
    st.markdown("### 📨 권한 요청")
    app_keys = [k for k in apps_config().keys() if k != "frontgate"]
    labels = {k: app_meta(k)["title"] for k in app_keys}
    with st.form("signup_request_form", clear_on_submit=True):
        name = st.text_input("이름 *")
        login_id = st.text_input("희망 아이디 *", placeholder="한글아이디 가능")
        password = st.text_input("비밀번호 *", type="password")
        password_confirm = st.text_input("비밀번호 확인 *", type="password")
        department = st.text_input("부서 *", placeholder="미디어)마케팅팀")
        email = st.text_input("이메일", placeholder="aaa@cj.net")
        requested_apps = st.multiselect("사용 희망 서비스", app_keys, format_func=lambda x: labels.get(x, x))
        reason = st.text_area("사용 목적", height=120, placeholder="생략 가능")
        submitted = st.form_submit_button("권한 요청 보내기", use_container_width=True)
    if submitted:
        ok, msg = submit_signup_request(name, login_id, password, password_confirm, email, department, reason, requested_apps)
        (st.success if ok else st.error)(msg)



def role_label(role: Optional[str]) -> str:
    role = normalize_role(role)
    labels = {
        ROLE_MASTER: "마스터",
        ROLE_ADMIN: "어드민",
        ROLE_TEAM_MEMBER: "팀원",
        ROLE_USER: "유저",
    }
    return labels.get(role, role)


def assignable_role_options(actor_user: Dict[str, Any], target_user: Optional[Dict[str, Any]] = None) -> List[str]:
    target_name = (target_user or {}).get("name")
    if is_master(actor_user):
        return [ROLE_MASTER, ROLE_ADMIN, ROLE_TEAM_MEMBER, ROLE_USER]
    if is_admin(actor_user):
        return [ROLE_ADMIN, ROLE_TEAM_MEMBER, ROLE_USER]
    current_role = normalize_role((target_user or {}).get("role"), target_name)
    return [current_role]


def render_admin_panel(admin_user: Dict[str, Any], page: str):
    st.markdown("### 🛠 관리자 페이지")

    app_keys = [k for k in apps_config().keys() if k != "frontgate"]
    app_labels = {k: app_meta(k)["title"] for k in app_keys}
    permission_options = ["user_manage", "approve_signup", "session_manage", "ytan_admin"]

    if page == "signup_requests":
        st.caption("가입 요청 검토 및 승인")
        reqs = get_signup_requests("pending")
        signup_reqs = [r for r in reqs if str(r.get("type") or "signup") == "signup"]
        if not signup_reqs:
            st.info("대기 중인 가입 요청이 없습니다.")
        for req in signup_reqs:
            req_id = str(req.get("_id"))
            default_apps = clean_allowed_apps(req.get("requested_apps") or [], app_keys)
            with st.expander(f"[대기] {req.get('name')} · {req.get('login_id')}"):
                st.write(f"- 이메일: {req.get('email') or '-'}")
                st.write(f"- 부서: {req.get('department') or '-'}")
                st.write(f"- 요청 앱: {', '.join(default_apps) or '-'}")
                st.write(f"- 사유: {req.get('reason') or '-'}")
                st.write(f"- 요청시각: {req.get('requested_at')}")
                selected_apps = st.multiselect(
                    "승인할 접근 가능 서비스",
                    app_keys,
                    default=default_apps,
                    format_func=lambda x: app_labels.get(x, x),
                    key=f"approve_apps_{req_id}",
                )
                role_options = assignable_role_options(admin_user, {"name": req.get("name")})
                default_role = normalize_role(AUTH["default_role"], req.get("name"))
                default_index = role_options.index(default_role) if default_role in role_options else len(role_options) - 1
                approved_role = st.selectbox(
                    "권한",
                    role_options,
                    index=default_index,
                    format_func=role_label,
                    key=f"approve_role_{req_id}",
                )
                approved_permissions = st.multiselect(
                    "추가 권한",
                    permission_options,
                    default=[],
                    key=f"approve_perms_{req_id}",
                )
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("승인", key=f"approve_{req_id}", use_container_width=True):
                        approve_request(req, admin_user, allowed_apps=selected_apps, role=approved_role, permissions=approved_permissions)
                        st.success("승인 완료")
                        st.rerun()
                with c2:
                    note = st.text_input("반려 메모", key=f"rej_note_{req_id}")
                    if st.button("반려", key=f"reject_{req_id}", use_container_width=True):
                        reject_request(req["_id"], admin_user, note)
                        st.success("반려 처리 완료")
                        st.rerun()

    elif page == "password_resets":
        st.caption("비밀번호 재설정 요청 승인")
        reqs = get_signup_requests("pending")
        pw_reqs = [r for r in reqs if str(r.get("type") or "") == "password_reset"]
        if not pw_reqs:
            st.info("대기 중인 비밀번호 재설정 요청이 없습니다.")
        for req in pw_reqs:
            req_id = str(req.get("_id"))
            with st.expander(f"[대기] 비밀번호 재설정 · {req.get('login_id')}"):
                st.write(f"- 이름: {req.get('name') or '-'}")
                st.write(f"- 이메일: {req.get('email') or '-'}")
                st.write(f"- 메모: {req.get('reason') or '-'}")
                st.write(f"- 요청시각: {req.get('requested_at')}")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("승인", key=f"pw_approve_{req_id}", use_container_width=True):
                        approve_password_reset_request(req, admin_user)
                        st.success("재설정 승인 완료")
                        st.rerun()
                with c2:
                    note = st.text_input("반려 메모", key=f"pw_rej_note_{req_id}")
                    if st.button("반려", key=f"pw_reject_{req_id}", use_container_width=True):
                        reject_request(req["_id"], admin_user, note)
                        st.success("반려 처리 완료")
                        st.rerun()

    elif page == "member_management":
        st.caption("멤버 상태와 서비스 접근 권한 관리")
        users = list_users()
        if not users:
            st.info("사용자가 없습니다.")

        with st.expander("새 계정 생성", expanded=False):
            with st.form("admin_create_user_form", clear_on_submit=True):
                login_id = st.text_input("아이디 *", key="admin_create_login_id")
                name = st.text_input("이름 *", key="admin_create_name")
                password = st.text_input("비밀번호 *", type="password", key="admin_create_password")
                password_confirm = st.text_input("비밀번호 확인 *", type="password", key="admin_create_password_confirm")
                email = st.text_input("이메일", key="admin_create_email")
                department = st.text_input("부서", key="admin_create_department")
                create_role_options = assignable_role_options(admin_user)
                default_create_role = normalize_role(AUTH["default_role"])
                default_create_idx = create_role_options.index(default_create_role) if default_create_role in create_role_options else len(create_role_options) - 1
                role = st.selectbox("권한", create_role_options, index=default_create_idx, format_func=role_label, key="admin_create_role")
                allowed_apps = st.multiselect("접근 가능 서비스", app_keys, format_func=lambda x: app_labels.get(x, x), key="admin_create_apps")
                permissions = st.multiselect("추가 권한", permission_options, key="admin_create_permissions")
                submitted = st.form_submit_button("계정 생성", use_container_width=True)
            if submitted:
                ok, msg = create_user_by_admin(admin_user, login_id, name, password, password_confirm, role, allowed_apps, permissions, email, department)
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()

        for u in users:
            uid = str(u.get("id"))
            session_summary = get_user_session_summary(uid)
            with st.expander(f"{u.get('name') or uid} · {uid}"):
                info_col1, info_col2, info_col3 = st.columns(3)
                info_col1.metric("최근 접속", format_dt_kst(session_summary.get("last_seen_at")))
                info_col2.metric("마지막 접속 앱", str(session_summary.get("last_source_app") or "-"))
                info_col3.metric("현재 활성 세션 수", int(session_summary.get("active_session_count") or 0))
                st.caption(f"현재 상태: {'활성' if bool(u.get('active', True)) else '비활성'} · 현재 권한: {role_label(u.get('role'))}")

                current_role = normalize_role(u.get("role"), u.get("name"))
                role_options = assignable_role_options(admin_user, u)
                if current_role not in role_options:
                    role_options = [current_role] + role_options
                new_role = st.selectbox("권한", role_options, index=role_options.index(current_role), format_func=role_label, key=f"user_role_{uid}")
                user_default_apps = clean_allowed_apps(u.get("allowed_apps") or [], app_keys)
                new_apps = st.multiselect(
                    "접근 가능 서비스",
                    app_keys,
                    default=user_default_apps,
                    format_func=lambda x: app_labels.get(x, x),
                    key=f"user_apps_{uid}",
                )
                new_perms = st.multiselect(
                    "추가 권한",
                    permission_options,
                    default=[p for p in list(u.get("permissions") or []) if p in permission_options],
                    key=f"user_perms_{uid}",
                )
                can_edit_user = can_manage_user(admin_user, u)
                c1, c2 = st.columns(2)
                with c1:
                    if mongo_available() and can_edit_user and st.button("권한 저장", key=f"save_access_{uid}", use_container_width=True):
                        ok, msg = update_user_access(admin_user, uid, new_apps, role=new_role, permissions=new_perms)
                        (st.success if ok else st.error)(msg)
                        if ok:
                            st.rerun()
                with c2:
                    if mongo_available() and can_edit_user and uid != str(admin_user.get("id")):
                        currently_active = bool(u.get("active", True))
                        label = "비활성화" if currently_active else "활성화"
                        if st.button(label, key=f"toggle_{uid}", use_container_width=True):
                            toggle_user_active(uid, not currently_active)
                            st.success("상태가 변경되었습니다.")
                            st.rerun()
                if not can_edit_user:
                    st.caption("현재 권한으로는 이 계정을 수정할 수 없습니다.")
                if is_master(admin_user) and uid != str(admin_user.get("id")):
                    st.divider()
                    st.warning("마스터 권한 이양은 즉시 적용됩니다.")
                    if st.button("이 계정으로 마스터 권한 이양", key=f"transfer_master_{uid}", use_container_width=True):
                        ok, msg = transfer_master(admin_user, uid)
                        (st.success if ok else st.error)(msg)
                        if ok:
                            st.rerun()



def render_sidebar(user: Dict[str, Any]):
    with st.sidebar:
        st.markdown("### 메뉴")
        if is_admin(user):
            st.caption("관리자 페이지")
            if st.button("가입 요청", use_container_width=True):
                set_admin_page("signup_requests")
                st.rerun()
            if st.button("비밀번호 재설정", use_container_width=True):
                set_admin_page("password_resets")
                st.rerun()
            if st.button("멤버 관리", use_container_width=True):
                set_admin_page("member_management")
                st.rerun()
            if get_admin_page() and st.button("서비스 홈으로", use_container_width=True):
                set_admin_page("")
                st.rerun()


# =========================================================
# main
# =========================================================
render_login_finalize()

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

if user and user.get("session_token") and st.session_state.get("_frontgate_ls_synced") != str(user.get("session_token")):
    inject_local_storage_set(str(user.get("session_token")))
    st.session_state["_frontgate_ls_synced"] = str(user.get("session_token"))

render_sidebar(user)
admin_page = get_admin_page() if is_admin(user) else ""
if admin_page:
    render_admin_panel(user, admin_page)
else:
    render_card_rows(user)

st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
if st.button("로그아웃", use_container_width=True):
    logout_user()

st.markdown("<hr style='margin-top:30px; opacity:.2;'>", unsafe_allow_html=True)
st.markdown("<p style='text-align:center; opacity:.65;'>© 드라마 마케팅 대시보드</p>", unsafe_allow_html=True)
