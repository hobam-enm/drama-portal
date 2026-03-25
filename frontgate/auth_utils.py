import base64
import hashlib
import hmac
import json
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


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _normalize_user_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sub": str(user.get("sub") or user.get("id") or ""),
        "id": str(user.get("id") or user.get("sub") or ""),
        "name": str(user.get("name") or user.get("id") or user.get("sub") or ""),
        "role": str(user.get("role") or "user"),
        "apps": list(user.get("apps") or user.get("allowed_apps") or []),
        "perms": list(user.get("perms") or user.get("permissions") or []),
        "session_token": str(user.get("session_token") or user.get("st") or ""),
    }


def is_admin(payload: Dict[str, Any]) -> bool:
    role = str(payload.get("role", ""))
    admin_role = st.secrets.get("auth", {}).get("admin_role_name", "admin")
    if role == admin_role:
        return True
    perms = payload.get("perms", []) or payload.get("permissions", []) or []
    return "user_manage" in perms or "approve_signup" in perms


def has_app_access(payload: Dict[str, Any], app_name: str) -> bool:
    if is_admin(payload):
        return True
    apps = payload.get("apps", []) or payload.get("allowed_apps", []) or []
    return app_name in apps


def verify_handoff_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        secret = str(st.secrets.get("auth", {}).get("signing_secret", ""))
        if not secret:
            return None
        body, sig = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).decode("utf-8").rstrip("=")
        if not hmac.compare_digest(expected_b64, sig):
            return None
        payload = json.loads(_b64url_decode(body))
        exp = int(payload.get("exp", 0))
        if exp and datetime.now(UTC).timestamp() > exp:
            return None
        return payload
    except Exception:
        return None


def _remove_query_param(key: str) -> None:
    try:
        current = dict(st.query_params)
        if key in current:
            current.pop(key, None)
            st.query_params.clear()
            for k, v in current.items():
                st.query_params[k] = v
    except Exception:
        pass


def _get_cookie_manager():
    if stx is None:
        return None
    try:
        if "_portal_cookie_manager" not in st.session_state:
            st.session_state["_portal_cookie_manager"] = stx.CookieManager()
        return st.session_state["_portal_cookie_manager"]
    except Exception:
        return None


def _app_cookie_name(app_name: str) -> str:
    base = str(st.secrets.get("auth", {}).get("cookie_name", "drama_portal_session") or "drama_portal_session")
    return f"{base}_{app_name}"


def _app_local_storage_key(app_name: str) -> str:
    base = str(st.secrets.get("auth", {}).get("local_storage_key", "drama_portal_auth") or "drama_portal_auth")
    return f"{base}_{app_name}"


def _get_cookie(name: str) -> str:
    cm = _get_cookie_manager()
    if cm is None:
        return ""
    try:
        cookies = cm.get_all() or {}
        return str(cookies.get(name, "") or "")
    except Exception:
        return ""


def _set_cookie(name: str, value: str, days: int = 3):
    cm = _get_cookie_manager()
    if cm is None:
        return
    try:
        expires_at = datetime.now(KST) + timedelta(days=days)
        cm.set(name, value, expires_at=expires_at, secure=True, same_site="Lax")
    except Exception:
        pass


def _inject_browser_session_set(app_name: str, token: str, days: int = 3):
    safe_token = json.dumps(token or "")
    safe_key = json.dumps(_app_local_storage_key(app_name))
    cookie_name = json.dumps(_app_cookie_name(app_name))
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


def _get_local_storage_token(app_name: str, seq: Optional[str] = None) -> str:
    if streamlit_js_eval is None:
        return ""
    try:
        key = f"portal_local_storage_token_{app_name}_{seq or 'x'}"
        value = streamlit_js_eval(
            js_expressions=f"window.localStorage.getItem({json.dumps(_app_local_storage_key(app_name))})",
            key=key,
        )
        return str(value or "")
    except Exception:
        return ""


@st.cache_resource(show_spinner=False)
def _get_mongo_client():
    uri = str(st.secrets.get("mongo", {}).get("uri", "") or "")
    if not uri or MongoClient is None:
        return None
    return MongoClient(uri)


def _ensure_utc(dt):
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    client = _get_mongo_client()
    mongo_cfg = st.secrets.get("mongo", {})
    if client is not None:
        db = client[str(mongo_cfg.get("db_name", "drama_portal"))]
        coll = db[str(mongo_cfg.get("users_coll", "users"))]
        user = coll.find_one({"id": user_id})
        if user:
            return user
    for u in st.secrets.get("users", []) or []:
        if str(u.get("id")) == str(user_id):
            return dict(u)
    return None


def _validate_session(raw_token: str) -> Optional[Dict[str, Any]]:
    client = _get_mongo_client()
    mongo_cfg = st.secrets.get("mongo", {})
    auth_cfg = st.secrets.get("auth", {})
    if not raw_token or client is None:
        return None
    db = client[str(mongo_cfg.get("db_name", "drama_portal"))]
    sessions = db[str(mongo_cfg.get("sessions_coll", "sessions"))]
    doc = sessions.find_one({"token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), "is_active": True})
    if not doc:
        return None
    now = datetime.now(UTC)
    expires_at = _ensure_utc(doc.get("expires_at"))
    revoked_at = _ensure_utc(doc.get("revoked_at"))
    if revoked_at or expires_at is None or expires_at < now:
        return None
    idle_minutes = int(auth_cfg.get("session_idle_minutes", 120) or 120)
    last_seen = _ensure_utc(doc.get("last_seen_at")) or _ensure_utc(doc.get("created_at")) or now
    if last_seen + timedelta(minutes=idle_minutes) < now:
        return None
    user = _get_user_by_id(str(doc.get("user_id") or ""))
    if not user or not bool(user.get("active", True)):
        return None
    sessions.update_one({"_id": doc["_id"]}, {"$set": {"last_seen_at": now}})
    return {
        "id": str(user.get("id")),
        "name": str(user.get("name") or user.get("id")),
        "role": str(user.get("role") or auth_cfg.get("default_role", "user")),
        "allowed_apps": list(user.get("allowed_apps") or []),
        "permissions": list(user.get("permissions") or []),
        "session_token": raw_token,
    }


def _restore_from_browser_session(app_name: str) -> Optional[Dict[str, Any]]:
    bootstrap_count = int(st.session_state.get(f"_restore_bootstrap_count_{app_name}", 0) or 0)

    cookie_token = _get_cookie(_app_cookie_name(app_name))
    if cookie_token:
        user = _validate_session(cookie_token)
        if user:
            _set_cookie(_app_cookie_name(app_name), cookie_token, days=int(st.secrets.get("auth", {}).get("remember_me_days", 3) or 3))
            return _normalize_user_payload(user)

    ls_token = _get_local_storage_token(app_name, seq=f"boot_{bootstrap_count}")
    if ls_token and ls_token not in ("null", "undefined"):
        user = _validate_session(ls_token)
        if user:
            _set_cookie(_app_cookie_name(app_name), ls_token, days=int(st.secrets.get("auth", {}).get("remember_me_days", 3) or 3))
            return _normalize_user_payload(user)

    if bootstrap_count < 2:
        st.session_state[f"_restore_bootstrap_count_{app_name}"] = bootstrap_count + 1
        _get_cookie_manager()
        _get_local_storage_token(app_name, seq=f"bootstrap_{bootstrap_count}")
        st.rerun()
    return None


def check_auth(app_name: str) -> Dict[str, Any]:
    if "current_user" in st.session_state:
        user_payload = _normalize_user_payload(st.session_state["current_user"])
        if not has_app_access(user_payload, app_name):
            st.error("이 서비스에 접근할 권한이 없습니다.")
            st.stop()
        st.session_state["current_user"] = user_payload
        return user_payload

    restored = _restore_from_browser_session(app_name)
    if restored:
        st.session_state["current_user"] = restored
        st.session_state[f"_restore_bootstrap_count_{app_name}"] = 0
        return restored

    token = st.query_params.get("auth")
    if token:
        payload = verify_handoff_token(token)
        if payload:
            if payload.get("app") != app_name:
                st.error("잘못된 접근입니다. (앱 대상 불일치)")
                st.stop()
            norm = _normalize_user_payload(payload)
            if not has_app_access(norm, app_name):
                st.error("이 서비스에 접근할 권한이 없습니다.")
                st.stop()
            st.session_state["current_user"] = norm
            raw_session = str(payload.get("st") or "")
            if raw_session:
                days = int(st.secrets.get("auth", {}).get("remember_me_days", 3) or 3)
                _set_cookie(_app_cookie_name(app_name), raw_session, days=days)
                _inject_browser_session_set(app_name, raw_session, days=days)
            _remove_query_param("auth")
            return norm

    st.warning("인증이 필요합니다. 드라마 데이터 포털을 통해 접속해 주세요.")
    frontgate_url = st.secrets.get("apps", {}).get("frontgate", "")
    if frontgate_url:
        st.markdown(f"[➡️ 포털 메인으로 이동하기]({frontgate_url})")
    st.stop()
