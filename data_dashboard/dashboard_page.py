from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

import streamlit as st

_SOURCE_CACHE: str | None = None


def _dashboard_secret(*keys: str, default=None):
    cur: Any = st.secrets
    try:
        for k in keys:
            cur = cur[k]
        return cur
    except Exception:
        return default


def _patched_dashboard_source() -> str:
    global _SOURCE_CACHE
    if _SOURCE_CACHE is not None:
        return _SOURCE_CACHE

    src_path = Path(__file__).with_name("Dashboard.py")
    source = src_path.read_text(encoding="utf-8", errors="replace")

    source = re.sub(
        r'''st\.set_page_config\(.*?\)\s*\n\s*\n''',
        "",
        source,
        count=1,
        flags=re.S,
    )

    source = re.sub(
        r'''#region \[ 3\. 인증/쿠키 게이트 \].*?if not check_password_with_cookie\(\):\s*st\.stop\(\)\s*''',
        "",
        source,
        count=1,
        flags=re.S,
    )

    source = source.replace(
        'creds_info = st.secrets["gcp_service_account"]',
        'creds_info = _dashboard_secret("google", "service_account") or st.secrets["gcp_service_account"]',
    )
    source = source.replace(
        'sheet_id = st.secrets["SHEET_ID"]',
        'sheet_id = _dashboard_secret("data_dashboard", "sheet_id") or st.secrets["SHEET_ID"]',
    )
    source = source.replace(
        'worksheet_name = st.secrets["SHEET_NAME"]',
        'worksheet_name = _dashboard_secret("data_dashboard", "sheet_name") or st.secrets["SHEET_NAME"]',
    )

    source = source.replace(
        'with st.sidebar:\n    render_gradient_title("드라마 성과 대시보드", emoji="") # (폰트 키운 버전 적용됨)\n',
        'with st.sidebar:\n    if st.button("← 서비스 홈", use_container_width=True, key="portal_home_btn"):\n        st.query_params["app"] = "home"\n        _rerun()\n    render_gradient_title("드라마 성과 대시보드", emoji="") # (폰트 키운 버전 적용됨)\n',
        1,
    )

    source = source.replace(
        '        st.query_params["page"] = page_key\n',
        '        st.query_params["app"] = "data_dashboard"\n        st.query_params["page"] = page_key\n',
    )

    _SOURCE_CACHE = source
    return source


def render_dashboard_page(user: Dict[str, Any] | None = None) -> None:
    namespace: Dict[str, Any] = {
        "__file__": str(Path(__file__).with_name("Dashboard.py")),
        "__name__": "__dashboard_page__",
        "_dashboard_secret": _dashboard_secret,
        "PORTAL_CURRENT_USER": user or {},
    }
    exec(_patched_dashboard_source(), namespace, namespace)
