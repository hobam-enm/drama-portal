import base64
import hashlib
import hmac
import json
import secrets as py_secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

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
except Exception:
    MongoClient = None

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


# =========================
# secrets helpers
# =========================
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
    cfg.setdefault("sessions_coll", "sessions")
    return cfg


AUTH = get_auth_cfg()
MONGO = get_mongo_cfg()
COOKIE_NAME = str(AUTH.get("cookie_name") or "drama_portal_session")
LOCAL_STORAGE_KEY = str(AUTH.get("local_storage_key") or "drama_portal_auth")
SIGNING_SECRET = str(AUTH.get("signing_secret") or "")


# =========================
# utility functions
# =========================
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


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _remove_query_param(name: str):
    try:
        qp = dict(st.query_params)
        if name in qp:
            qp.pop(name, None)
            st.query_params.clear()
            for k, v in qp.items():
                st.query_params[k] = v
    except Exception:
        pass


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def is_admin(payload: Dict[str, Any]) -> bool:
    role = str(payload.get("role", ""))
    admin_role = AUTH.get("admin_role_name", "admin")
    if role == admin_role:
        return True
    perms = payload.get("perms", payload.get("permissions", []))
    return "user_manage" in perms or "approve_signup" in perms


def has_app_access(payload: Dict[str, Any], app_name: str) -> bool:
    if is_admin(payload):
        return True
    return app_name in payload.get("apps", payload.get("allowed_apps", []))


# =========================
# browser storage helpers
# =========================
def get_cookie_manager():
    if stx is None:
        return None
    try:
        if "_childapp_cookie_manager" not in st.session_state:
            st.session_state["_childapp_cookie_manager"] = stx.CookieManager()
        return st.session_state["_childapp_cookie_manager"]
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


def get_local_storage_token(seq: Optional[str] = None) -> str:
    if streamlit_js_eval is None:
        return ""
    try:
        key = f"childapp_local_storage_token_{seq or uuid.uuid4().hex}"
        value = streamlit_js_eval(
            js_expressions=f"window.localStorage.getItem({json.dumps(LOCAL_STORAGE_KEY)})",
            key=key,
        )
        return str(value or "")
    except Exception:
        return ""


# =========================
# mongo helpers
# =========================
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


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    if mongo_available():
        c = coll("users_coll")
        if c is not None:
            user = c.find_one({"id": user_id})
            if user:
                return user
    for u in sget("users", default=[]) or []:
        if str(dict(u).get("id")) == str(user_id):
            return dict(u)
    return None


def create_session_from_payload(payload: Dict[str, Any], source_app: str) -> str:
    raw = py_secrets.token_urlsafe(32)
    ttl_hours = int(AUTH.get("session_ttl_hours", 12))
    remember_days = int(AUTH.get("remember_me_days", 3))
    now = utcnow()
    expires_at = now + timedelta(days=remember_days)
    session_doc = {
        "token_hash": token_hash(raw),
        "user_id": str(payload.get("sub") or payload.get("id") or ""),
        "role": str(payload.get("role") or AUTH["default_role"]),
        "allowed_apps": list(payload.get("apps") or payload.get("allowed_apps") or []),
        "permissions": list(payload.get("perms") or payload.get("permissions") or []),
        "created_at": now,
        "last_seen_at": now,
        "expires_at": expires_at,
        "revoked_at": None,
        "is_active": True,
        "source_app": source_app,
        "delete_after": expires_at + timedelta(days=1),
        "remember_days": remember_days,
        "session_ttl_hours": ttl_hours,
    }
    if mongo_available():
        sessions = coll("sessions_coll")
        sessions.insert_one(session_doc)
    return raw


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
    if user and not bool(user.get("active", True)):
        sessions.update_one({"_id": doc["_id"]}, {"$set": {"is_active": False, "revoked_at": now, "delete_after": now + timedelta(days=1)}})
        return None
    sessions.update_one({"_id": doc["_id"]}, {"$set": {"last_seen_at": now}})
    allowed_apps = list((user or {}).get("allowed_apps") or doc.get("allowed_apps") or [])
    permissions = list((user or {}).get("permissions") or doc.get("permissions") or [])
    role = str((user or {}).get("role") or doc.get("role") or AUTH["default_role"])
    name = str((user or {}).get("name") or doc.get("user_id") or "")
    user_id = str((user or {}).get("id") or doc.get("user_id") or "")
    return {
        "id": user_id,
        "sub": user_id,
        "name": name,
        "role": role,
        "apps": allowed_apps,
        "allowed_apps": allowed_apps,
        "perms": permissions,
        "permissions": permissions,
        "session_token": raw_token,
    }


# =========================
# handoff verification
# =========================
def verify_handoff_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        if not SIGNING_SECRET:
            return None
        body, sig = token.split(".", 1)
        expected = hmac.new(SIGNING_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).decode("utf-8").rstrip("=")
        if not hmac.compare_digest(expected_b64, sig):
            return None
        payload = json.loads(_b64url_decode(body))
        exp = int(payload.get("exp", 0))
        if exp and utcnow().timestamp() > exp:
            return None
        return payload
    except Exception:
        return None


# =========================
# restore / auth gate
# =========================
def _restore_from_browser() -> Optional[Dict[str, Any]]:
    if not mongo_available():
        return None

    bootstrap_count = int(st.session_state.get("_child_restore_bootstrap_count", 0) or 0)

    cookie_token = get_cookie(COOKIE_NAME)
    if cookie_token:
        user = validate_session(cookie_token)
        if user:
            st.session_state["current_user"] = user
            st.session_state["_child_restore_bootstrap_count"] = 0
            st.session_state["_child_show_restore_splash"] = False
            return user

    ls_token = get_local_storage_token(seq=f"boot_{bootstrap_count}")
    if ls_token and ls_token not in ("null", "undefined"):
        user = validate_session(ls_token)
        if user:
            set_cookie(COOKIE_NAME, ls_token, days=int(AUTH.get("remember_me_days", 3)))
            st.session_state["current_user"] = user
            st.session_state["_child_ls_synced"] = ls_token
            st.session_state["_child_restore_bootstrap_count"] = 0
            st.session_state["_child_show_restore_splash"] = False
            return user

    if bootstrap_count < 2:
        st.session_state["_child_restore_bootstrap_count"] = bootstrap_count + 1
        st.session_state["_child_show_restore_splash"] = True
        get_cookie_manager()
        get_local_storage_token(seq=f"bootstrap_{bootstrap_count}")
        st.rerun()

    st.session_state["_child_show_restore_splash"] = False
    return None


def _render_child_login_finalize() -> bool:
    pending = st.session_state.get("_child_pending_login_finalize") or {}
    token = str(pending.get("token") or "")
    if not token:
        return False
    days = int(pending.get("days") or int(AUTH.get("remember_me_days", 3) or 3))
    stage = int(st.session_state.get("_child_pending_login_stage", 0) or 0)
    st.info("로그인 상태를 복구하는 중입니다...")
    if stage == 0:
        set_cookie(COOKIE_NAME, token, days=days)
        inject_browser_session_set(token, days=days)
        st.session_state["_child_pending_login_stage"] = 1
        st.rerun()
    st.session_state.pop("_child_pending_login_finalize", None)
    st.session_state.pop("_child_pending_login_stage", None)
    st.session_state["_child_ls_synced"] = token
    st.rerun()


def check_auth(app_name: str) -> Dict[str, Any]:
    if _render_child_login_finalize():
        st.stop()

    # 1) in-memory session
    if "current_user" in st.session_state:
        user_payload = st.session_state["current_user"]
        if not has_app_access(user_payload, app_name):
            st.error("이 서비스에 접근할 권한이 없습니다.")
            st.stop()
        return user_payload

    # 2) restore from cookie / localStorage / mongo
    restored = _restore_from_browser()
    if restored:
        if not has_app_access(restored, app_name):
            st.error("이 서비스에 접근할 권한이 없습니다.")
            st.stop()
        if st.session_state.get("_child_ls_synced") != str(restored.get("session_token") or ""):
            inject_browser_session_set(str(restored.get("session_token") or ""), days=int(AUTH.get("remember_me_days", 3)))
            st.session_state["_child_ls_synced"] = str(restored.get("session_token") or "")
        return restored

    # 3) first-entry handoff token from frontgate
    token = st.query_params.get("auth")
    if token:
        payload = verify_handoff_token(token)
        if payload:
            if payload.get("app") != app_name:
                st.error("잘못된 접근입니다. (앱 대상 불일치)")
                st.stop()
            if not has_app_access(payload, app_name):
                st.error("이 서비스에 접근할 권한이 없습니다.")
                st.stop()

            session_token = ""
            if mongo_available():
                session_token = create_session_from_payload(payload, source_app=app_name)
                if session_token:
                    st.session_state["_child_pending_login_finalize"] = {
                        "token": session_token,
                        "days": int(AUTH.get("remember_me_days", 3)),
                    }
                    st.session_state["_child_pending_login_stage"] = 0

            user_payload = {
                "id": str(payload.get("sub") or ""),
                "sub": str(payload.get("sub") or ""),
                "name": str(payload.get("name") or payload.get("sub") or ""),
                "role": str(payload.get("role") or AUTH["default_role"]),
                "apps": list(payload.get("apps") or []),
                "allowed_apps": list(payload.get("apps") or []),
                "perms": list(payload.get("perms") or []),
                "permissions": list(payload.get("perms") or []),
                "session_token": session_token,
            }
            st.session_state["current_user"] = user_payload
            _remove_query_param("auth")
            if session_token:
                st.rerun()
            return user_payload

    # 4) deny
    if st.session_state.get("_child_show_restore_splash"):
        st.info("로그인 상태를 확인하는 중입니다...")
        st.stop()

    st.warning("인증이 필요합니다. 드라마 데이터 포털을 통해 접속해 주세요.")
    frontgate_url = st.secrets.get("apps", {}).get("frontgate", "")
    if frontgate_url:
        st.markdown(f"[➡️ 포털 메인으로 이동하기]({frontgate_url})")
    st.stop()
