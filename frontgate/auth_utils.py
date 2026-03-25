# ===== 1. 모듈 및 라이브러리 임포트 =====
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import streamlit as st


# ===== 2. 유틸리티 함수: Base64 디코딩 및 권한 확인 =====

def _b64url_decode(data: str) -> bytes:
    """
    URL Safe Base64 문자열을 디코딩하는 함수입니다.
    데이터 길이가 4의 배수가 아닐 경우 발생하는 패딩(=) 누락 에러를 방지합니다.
    """
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def is_admin(payload: Dict[str, Any]) -> bool:
    """
    사용자의 페이로드 정보를 바탕으로 관리자 권한이 있는지 확인합니다.
    """
    role = str(payload.get("role", ""))
    admin_role = st.secrets.get("auth", {}).get("admin_role_name", "admin")
    
    if role == admin_role:
        return True
        
    perms = payload.get("perms", [])
    return "user_manage" in perms or "approve_signup" in perms


def has_app_access(payload: Dict[str, Any], app_name: str) -> bool:
    """
    관리자이거나 해당 앱(app_name)이 사용자의 허용된 앱 목록에 있는지 확인합니다.
    """
    if is_admin(payload):
        return True
    return app_name in payload.get("apps", [])


# ===== 3. 토큰 검증 로직 =====

def verify_handoff_token(token: str) -> Optional[Dict[str, Any]]:
    """
    프론트게이트에서 전달받은 토큰의 서명(HMAC-SHA256)과 만료 시간을 검증합니다.
    검증에 성공하면 사용자 정보가 담긴 페이로드 딕셔너리를 반환합니다.
    """
    try:
        # 1) 스트림릿 클라우드 Secrets에서 서명 키 로드
        secret = str(st.secrets.get("auth", {}).get("signing_secret", ""))
        if not secret:
            return None
            
        # 2) 토큰 분리 (바디와 서명)
        body, sig = token.split(".", 1)
        
        # 3) 서명 재계산 및 유효성 비교
        expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).decode("utf-8").rstrip("=")
        
        if not hmac.compare_digest(expected_b64, sig):
            return None
            
        # 4) 페이로드 파싱 및 만료 시간(exp) 확인
        payload = json.loads(_b64url_decode(body))
        exp = int(payload.get("exp", 0))
        
        if exp and datetime.now(timezone.utc).timestamp() > exp:
            return None
            
        return payload
        
    except Exception:
        return None


# ===== 4. 메인 인증 게이트 함수 =====

def check_auth(app_name: str) -> Dict[str, Any]:
    """
    개별 앱의 최상단에서 호출되어 인증 상태를 확인하는 메인 함수입니다.
    - 세션 유지: 이미 인증된 경우 통과
    - 토큰 검증: URL 쿼리 파라미터에 유효한 토큰이 있으면 세션에 저장 후 통과
    - 접근 차단: 토큰이 없거나 유효하지 않으면 실행을 중단(st.stop)하고 안내 메시지 출력
    """
    # 1) 이미 세션에 로그인 정보가 저장된 경우 (페이지 이동, 새로고침 시)
    if "current_user" in st.session_state:
        user_payload = st.session_state["current_user"]
        
        if not has_app_access(user_payload, app_name):
            st.error("이 서비스에 접근할 권한이 없습니다.")
            st.stop()
            
        return user_payload

    # 2) URL의 쿼리 파라미터에서 프론트게이트 발급 토큰 추출
    token = st.query_params.get("auth")
    
    if token:
        payload = verify_handoff_token(token)
        
        if payload:
            # 앱 대상 불일치 검증 (다른 앱의 접근 토큰을 복사해온 경우 차단)
            if payload.get("app") != app_name:
                st.error("잘못된 접근입니다. (앱 대상 불일치)")
                st.stop()
                
            # 사용자 권한 검증
            if not has_app_access(payload, app_name):
                st.error("이 서비스에 접근할 권한이 없습니다.")
                st.stop()

            # 3) 검증 성공 시 세션에 저장하여 이후 상태 유지
            st.session_state["current_user"] = payload
            
            # 보안 및 깔끔한 UI를 위해 URL에서 토큰 파라미터 제거
            st.query_params.clear()
            
            return payload

    # 4) 인증 실패 또는 토큰이 없는 경우 실행 차단
    st.warning("인증이 필요합니다. 드라마 데이터 포털을 통해 접속해 주세요.")
    
    # 설정된 프론트게이트 포털 URL이 있다면 링크 제공
    frontgate_url = st.secrets.get("apps", {}).get("frontgate", "")
    if frontgate_url:
        st.markdown(f"[➡️ 포털 메인으로 이동하기]({frontgate_url})")
        
    st.stop()