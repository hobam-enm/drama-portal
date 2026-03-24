import streamlit as st
import os
import glob
import json
import time
import re
import hashlib
import datetime
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import google.oauth2.credentials
import googleapiclient.discovery
import google.auth.transport.requests
import extra_streamlit_components as stx 
import google.generativeai as genai
from googleapiclient.errors import HttpError
import html
import html as _html
from pathlib import Path
from streamlit.components.v1 import html as st_html

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
from datetime import datetime, timedelta, timezone

import pymongo
from pymongo import MongoClient
import certifi

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# region [1. 설정 및 상수 (Config & Constants)]
# ==========================================
st.set_page_config(
    page_title="YT(PGC) Data Tracker", 
    page_icon="📊",
    layout="wide", 
    initial_sidebar_state="collapsed" 
)
# endregion


# region [1-1. 입장게이트 (보안 인증)]
# ==========================================
def _rerun():
    """스트림릿 버전 호환 리런 함수"""
    if hasattr(st, "rerun"): st.rerun()
    else: st.experimental_rerun()

def get_cookie_manager():
    return stx.CookieManager(key="yt_auth_cookie_manager")

def _hash_password(password: str) -> str:
    return hashlib.sha256(str(password).encode()).hexdigest()

def check_password_with_cookie() -> bool:
    cookie_manager = get_cookie_manager()
    secret_pwd = st.secrets.get("DASHBOARD_PASSWORD")
    if not secret_pwd:
        if "general" in st.secrets: secret_pwd = st.secrets["general"].get("DASHBOARD_PASSWORD")
            
    if not secret_pwd:
        st.error("🔒 설정 오류: Secrets에 'DASHBOARD_PASSWORD'가 설정되지 않았습니다.")
        st.stop()
        
    hashed_secret = _hash_password(str(secret_pwd))
    cookies = cookie_manager.get_all()
    COOKIE_NAME = "yt_dashboard_auth"
    current_token = cookies.get(COOKIE_NAME)
    
    is_cookie_valid = (current_token == hashed_secret)
    is_session_valid = st.session_state.get("auth_success", False)
    
    if is_cookie_valid or is_session_valid:
        if is_cookie_valid and not is_session_valid:
            st.session_state["auth_success"] = True
        return True

    st.markdown("#### 🔒 Access Restricted")
    st.caption("관계자 외 접근이 제한된 페이지입니다.")
    
    col1, col2 = st.columns([1, 2])
    with col1:
        input_pwd = st.text_input("Password", type="password", key="login_pw_input")
        login_btn = st.button("Login", type="primary", use_container_width=True)

    if login_btn:
        if _hash_password(input_pwd) == hashed_secret:
            expires = datetime.now() + timedelta(days=1)
            cookie_manager.set(COOKIE_NAME, hashed_secret, expires_at=expires)
            st.session_state["auth_success"] = True
            st.success("✅ 인증 성공")
            time.sleep(0.5)
            _rerun()
        else:
            st.error("❌ 비밀번호가 일치하지 않습니다.")
    return False

if not check_password_with_cookie(): st.stop()
# endregion


# region [1-2. 배포 환경 설정 (Secrets 복원)]
# ==========================================
if "tokens" in st.secrets:
    for file_name, content in st.secrets["tokens"].items():
        if not file_name.endswith(".json"): file_name += ".json"
        if not os.path.exists(file_name):
            with open(file_name, "w", encoding='utf-8') as f: f.write(content)
# endregion


# region [1-3. 디자인 및 상수]
# ==========================================
custom_css = """
    <style>
        header[data-testid="stHeader"] { background: transparent; }
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        [data-testid="stDecoration"] {display: none;}
        .block-container { padding-top: 1rem; padding-bottom: 3rem; }
        .stApp { background-color: #f8f9fa; }
        div[data-testid="stMetric"] { background-color: white; padding: 15px; border-radius: 10px; border: 1px solid #eee; box-shadow: 0 2px 4px rgba(0,0,0,0.02); text-align: center; }
        div[data-testid="stMetricLabel"] { font-size: 0.9rem; color: #6c757d; }
        div[data-testid="stMetricValue"] { font-size: 1.6rem; font-weight: 700; color: #2d3436; }
        [data-testid="stForm"] { background-color: white; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #e0e0e0; padding: 20px; }
        h1, h2, h3, h4 { color: #2d3436; font-weight: 700; }
        .stDataFrame { border: 1px solid #f0f0f0; border-radius: 8px; }
    </style>
"""
st.markdown(custom_css, unsafe_allow_html=True)

MAX_WORKERS = 3
SCOPES = ['https://www.googleapis.com/auth/yt-analytics.readonly', 'https://www.googleapis.com/auth/youtube.readonly']

ISO_MAPPING = {
    'KR': 'KOR', 'US': 'USA', 'JP': 'JPN', 'VN': 'VNM', 'TH': 'THA', 
    'ID': 'IDN', 'TW': 'TWN', 'PH': 'PHL', 'MY': 'MYS', 'IN': 'IND',
    'BR': 'BRA', 'MX': 'MEX', 'RU': 'RUS', 'GB': 'GBR', 'DE': 'DEU',
    'FR': 'FRA', 'CA': 'CAN', 'AU': 'AUS', 'HK': 'HKG', 'SG': 'SGP'
}

def render_md_allow_br(text: str) -> str:
    raw = (text or "").strip()
    raw = re.sub(r"^\s*```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```\s*$", "", raw)
    escaped = html.escape(raw)
    escaped = re.sub(r"&lt;br\s*/?&gt;", "<br>", escaped, flags=re.IGNORECASE)
    return escaped
# endregion


# region [2. 유틸리티 함수 (Utilities)]
# ==========================================
def normalize_text(text):
    if not text: return ""
    return re.sub(r'[^a-zA-Z0-9가-힣]', '', text).lower()

def format_korean_number(num):
    if num == 0: return "0회"
    s = ""
    if num >= 100000000:
        eok = num // 100000000; rem = num % 100000000
        s += f"{int(eok)}억 "
        num = rem
    if num >= 10000:
        man = num // 10000; rem = num % 10000
        s += f"{int(man)}만 "
        num = rem
    if num > 0: s += f"{int(num)}"
    return s.strip() + "회"

TRAFFIC_MAP = {
    'YT_SEARCH': '유튜브 검색', 'RELATED_VIDEO': '추천 동영상',
    'BROWSE_FEATURES': '탐색 기능', 'EXT_URL': '외부 링크',
    'NO_LINK_OTHER': '기타', 'PLAYLIST': '재생목록',
    'VIDEO_CARD': '카드/최종화면', 'NOTIFICATION': '알림'
}
def map_traffic_source(key): return TRAFFIC_MAP.get(key, key)

def parse_utc_to_kst_date(utc_str):
    try:
        dt_utc = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
        return (dt_utc + timedelta(hours=9)).date()
    except: return None

def parse_duration_to_minutes(duration_str):
    if not duration_str: return 0.0
    pattern = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?')
    match = pattern.match(duration_str)
    if not match: return 0.0
    h, m, s = match.groups()
    total_sec = (int(h or 0) * 3600) + (int(m or 0) * 60) + (int(s or 0))
    return round(total_sec / 60, 1)

# ==========================================
# MongoDB 연결 및 데이터 처리 함수
# ==========================================
@st.cache_resource
def init_mongo():
    try:
        if "mongo" not in st.secrets: return None
        uri = st.secrets["mongo"]["uri"]
        return MongoClient(uri, tlsCAFile=certifi.where())
    except Exception as e:
        print(f"MongoDB Init Error: {e}")
        return None

def save_to_mongodb(file_name, content_list, update_source="unknown"):
    try:
        client = init_mongo()
        if not client: return False, "DB 연결 실패"
        
        db = client.get_database("yt_dashboard")
        col_videos = db.get_collection("videos")
        col_meta = db.get_collection("metadata")
        
        col_videos.delete_many({"source_file": file_name})
        
        if content_list:
            docs = []
            for item in content_list:
                doc = item.copy()
                doc['source_file'] = file_name
                docs.append(doc)
            col_videos.insert_many(docs)
            
        col_meta.update_one(
            {"_id": file_name},
            {"$set": {
                "updated_at": datetime.now(), 
                "count": len(content_list),
                "last_source": update_source
            }},
            upsert=True
        )
        load_from_mongodb.clear()
        get_last_update_time.clear()
        return True, f"MongoDB Saved ({len(content_list)} items)"

    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=3600, show_spinner=False)
def load_from_mongodb(file_name):
    try:
        client = init_mongo()
        if not client: return []
        
        db = client.get_database("yt_dashboard")
        return list(db.get_collection("videos").find({"source_file": file_name}, {"_id": 0, "source_file": 0}))

    except Exception as e:
        print(f"Load Error: {e}")
        return []

@st.cache_data(ttl=3600, show_spinner=False)
def get_last_update_time(file_name):
    try:
        client = init_mongo()
        if not client: return None
        
        db = client.get_database("yt_dashboard")
        doc = db.get_collection("metadata").find_one({"_id": file_name})
        
        if doc and 'updated_at' in doc:
            ts = doc['updated_at']
            dt_kst = ts + timedelta(hours=9)
            return dt_kst.strftime("%Y-%m-%d %H:%M")
        return None
    except: return None
# endregion


# region [3. 시각화 함수 (Visualization)]
# ==========================================
def get_pyramid_chart_and_df(stats_dict, total_views):
    """
    stats_dict: { 'age13-17_male': count_or_weighted_value, ... }
    total_views: 기준 조회수
    """
    if not stats_dict: return None, None, ""
    age_order = ['age13-17', 'age18-24', 'age25-34', 'age35-44', 'age45-54', 'age55-64', 'age65-']
    display_labels = [label.replace('age', '') for label in age_order]
    
    male_data = defaultdict(float); female_data = defaultdict(float)
    table_rows = []; total_male = 0; total_female = 0

    if total_views <= 0:
        total_views = 1

    for key, count in stats_dict.items():
        parts = key.split('_')
        if len(parts) != 2: continue
        age_group, gender = parts[0], parts[1]
        if gender not in ['male', 'female']: continue
        if age_group not in age_order: continue 
        
        pct = (count / total_views) * 100
        clean_age = age_group.replace('age', '')
        
        if gender == 'male':
            male_data[clean_age] += pct; total_male += pct
        elif gender == 'female':
            female_data[clean_age] += pct; total_female += pct
            
        table_rows.append({"연령": clean_age, "성별": "남" if gender=='male' else "여", "조회수": int(count), "비율": pct})

    male_vals = [male_data[l] for l in display_labels]
    female_vals = [female_data[l] for l in display_labels]
    male_vals_neg = [-v for v in male_vals] 

    fig = go.Figure()
    
    fig.add_trace(go.Bar(
        y=display_labels, x=male_vals_neg, 
        name='남성', orientation='h', 
        marker=dict(color='#5684D5'), 
        text=[f"{v:.1f}%" if v>0 else "" for v in male_vals], 
        textposition='auto',
        insidetextfont=dict(color='white') 
    ))
    
    fig.add_trace(go.Bar(
        y=display_labels, x=female_vals, 
        name='여성', orientation='h', 
        marker=dict(color='#FF7675'), 
        text=[f"{v:.1f}%" if v>0 else "" for v in female_vals], 
        textposition='auto',
        insidetextfont=dict(color='white')
    ))
    
    max_val = max(max(male_vals) if male_vals else 0, max(female_vals) if female_vals else 0)
    rng = max_val * 1.2 if max_val > 0 else 10

    fig.update_layout(
        barmode='overlay', 
        xaxis=dict(range=[-rng, rng], showticklabels=False, zeroline=False), 
        margin=dict(l=10, r=10, t=30, b=10), 
        height=300, 
        paper_bgcolor='rgba(0,0,0,0)', 
        plot_bgcolor='rgba(0,0,0,0)'
    )
    
    df = pd.DataFrame(table_rows)
    if not df.empty:
        df['연령'] = pd.Categorical(df['연령'], categories=display_labels, ordered=True)
        df = df.sort_values(['연령', '성별'])

    return fig, df, f"👥 성별/연령 (남 {total_male:.1f}% vs 여 {total_female:.1f}%)"

def get_traffic_chart(traffic_dict):
    if not traffic_dict: return None
    sorted_t = sorted(traffic_dict.items(), key=lambda x: x[1], reverse=True)
    labels = []; values = []
    for k, v in sorted_t[:5]: labels.append(map_traffic_source(k)); values.append(v)
    if len(sorted_t) > 5: labels.append("기타"); values.append(sum(v for k,v in sorted_t[5:]))
    if not values: return None
    fig = px.pie(names=labels, values=values, hole=0.5, color_discrete_sequence=['#00b894', '#00cec9', '#55efc4', '#81ecec'])
    fig.update_traces(textposition='inside', textinfo='percent+label')
    fig.update_layout(margin=dict(l=20, r=20, t=0, b=20), height=300, showlegend=False, paper_bgcolor='rgba(0,0,0,0)')
    return fig

def get_keyword_bar_chart(keyword_dict):
    if not keyword_dict: return None
    sorted_k = sorted(keyword_dict.items(), key=lambda x: x[1], reverse=True)[:10]
    if not sorted_k: return None
    words = [k[0] for k in sorted_k][::-1]; counts = [k[1] for k in sorted_k][::-1]
    fig = go.Figure(go.Bar(x=counts, y=words, orientation='h', marker=dict(color='#fdcb6e'), text=[f"{int(v):,}" for v in counts], textposition='auto'))
    fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=300, xaxis=dict(visible=False), paper_bgcolor='rgba(0,0,0,0)')
    return fig

def get_channel_share_chart(ch_details, highlight_channel=None):
    if not ch_details: return None
    sorted_ch = sorted(ch_details, key=lambda x: x['total_views'], reverse=True)
    labels = []; values = []
    
    for ch in sorted_ch[:5]: labels.append(ch['channel_name']); values.append(ch['total_views'])
    if len(sorted_ch) > 5: labels.append("그 외 채널"); values.append(sum(ch['total_views'] for ch in sorted_ch[5:]))
    
    if sum(values) == 0: return None
    
    fig = go.Figure(data=[go.Pie(
        labels=labels, 
        values=values, 
        hole=0.5, 
        marker=dict(colors=px.colors.sequential.Blues_r)
    )])
    
    fig.update_traces(textposition='inside', textinfo='percent+label')
    fig.update_layout(margin=dict(l=20, r=20, t=0, b=20), height=350, showlegend=False, paper_bgcolor='rgba(0,0,0,0)')
    return fig

def get_country_map(country_stats):
    if not country_stats: return None
    data = []
    for k, v in country_stats.items():
        iso3 = ISO_MAPPING.get(k, k)
        c_name = {'KR':'대한민국','US':'미국','JP':'일본','VN':'베트남'}.get(k, k)
        
        if iso3 == 'KOR' or k == 'KR': continue
            
        data.append({'iso_alpha': iso3, 'views': v, 'country': c_name, 'fmt': format_korean_number(v)})
    
    if not data: return None
    df_map = pd.DataFrame(data)
    
    fig = go.Figure(go.Choropleth(
        locations=df_map['iso_alpha'], 
        z=df_map['views'], 
        text=df_map['country'], 
        customdata=df_map[['country','fmt']], 
        colorscale='Blues', 
        marker_line_color='rgba(0,0,0,0.1)',
        marker_line_width=0.5,
        hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}<extra></extra>"
    ))
    
    fig.update_geos(
        showcountries=True, countrycolor="rgba(0,0,0,0.1)", 
        showcoastlines=True, coastlinecolor="rgba(0,0,0,0.1)",
        projection_type='natural earth', 
        fitbounds="locations", 
        bgcolor='rgba(0,0,0,0)'
    )
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=350, dragmode='pan', paper_bgcolor='rgba(0,0,0,0)')
    return fig

def get_daily_trend_chart(daily_stats, recent_gap=0):
    if not daily_stats: return None

    items = []
    for k, v in daily_stats.items():
        try:
            d = datetime.strptime(str(k), "%Y-%m-%d").date()
        except Exception:
            continue
        items.append((d, float(v or 0)))
    if not items: return None
    items.sort(key=lambda x: x[0])

    x_labels = [d.strftime('%Y-%m-%d') for d, v in items]
    y_vals = [v for d, v in items]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=x_labels, 
        y=y_vals,
        marker_color='#74b9ff',
        name='조회수(확정)'
    ))
    
    fig.update_layout(
        xaxis_title="날짜",
        yaxis_title="조회수",
        margin=dict(l=10, r=10, t=20, b=10),
        height=320,
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)'
    )
    return fig
# endregion


# region [4. API 및 데이터 처리 (API & Data Processing)]
# ==========================================
def get_last_update_raw(file_name):
    try:
        client = init_mongo()
        if not client: return None
        db = client.get_database("yt_dashboard")
        doc = db.get_collection("metadata").find_one({"_id": file_name})
        if doc and 'updated_at' in doc:
            return doc['updated_at']
        return None
    except: return None

# ===== [KST/주차(월~일) 유틸] =====
KST = timezone(timedelta(hours=9))

def kst_now():
    return datetime.now(timezone.utc).astimezone(KST)

def confirmed_cutoff_dt(hours=48):
    return kst_now() - timedelta(hours=hours)

def confirmed_cutoff_date(hours=48):
    return confirmed_cutoff_dt(hours).date()

def get_creds_with_status(token_filename):
    creds = None
    if not os.path.exists(token_filename):
        return None, "token 파일 없음"

    try:
        creds = google.oauth2.credentials.Credentials.from_authorized_user_file(token_filename, SCOPES)
    except Exception as e:
        return None, f"token 파일 파싱 실패: {e}"

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(google.auth.transport.requests.Request())
                with open(token_filename, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                return None, f"토큰 갱신 실패: {e}"
        else:
            if creds and creds.expired and not creds.refresh_token:
                return None, "토큰 만료 (refresh token 없음)"
            return None, "유효하지 않은 토큰"

    return creds, "정상"


def get_creds_from_file(token_filename):
    creds, _reason = get_creds_with_status(token_filename)
    return creds


def diagnose_channel_connection(token_file):
    file_label = os.path.basename(token_file).replace("token_", "").replace(".json", "")
    cache_name = f"cache_{os.path.basename(token_file)}"
    last_update = get_last_update_time(cache_name)
    cached_videos = load_from_mongodb(cache_name)

    result = {
        'token_file': os.path.basename(token_file),
        'label': file_label,
        'channel_name': file_label,
        'status': '미연결',
        'reason': '',
        'cached_videos': len(cached_videos),
        'last_update': last_update or '-'
    }

    creds, reason = get_creds_with_status(token_file)
    if not creds:
        result['reason'] = reason
        return result

    try:
        youtube = googleapiclient.discovery.build('youtube', 'v3', credentials=creds)
        ch_res = youtube.channels().list(part='snippet,contentDetails', mine=True).execute()
        items = ch_res.get('items', [])

        if not items:
            result['reason'] = '인증은 되었지만 조회 가능한 본인 채널이 없음'
            return result

        ch_item = items[0]
        ch_name = ch_item.get('snippet', {}).get('title') or file_label
        uploads_id = ch_item.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads')

        result['channel_name'] = ch_name
        if not uploads_id:
            result['reason'] = 'uploads 재생목록을 찾지 못함'
            return result

        result['status'] = '연결됨'
        result['reason'] = '정상'
        return result

    except HttpError as e:
        result['reason'] = f'YouTube API 오류: {e.status_code if hasattr(e, "status_code") else str(e)}'
        return result
    except Exception as e:
        result['reason'] = f'채널 확인 실패: {e}'
        return result

def process_sync_channel(token_file, status_box, update_source="manual"):
    def log_to_db(level, msg, detail=None):
        try:
            client = init_mongo()
            if client:
                db = client.get_database("yt_dashboard")
                db.get_collection("system_logs").insert_one({
                    'level': level, 'msg': msg, 'detail': str(detail),
                    'source': f'process_sync_channel({update_source})', 
                    'timestamp': datetime.now()
                })
        except: pass

    if status_box is None:
        class DummyBox:
            def success(self, m): pass
            def error(self, m): print(f"[Error] {m}")
            def warning(self, m): pass
            def info(self, m): pass
            def markdown(self, m): pass
            def caption(self, m): pass
        status_box = DummyBox()

    file_label = os.path.basename(token_file).replace("token_", "").replace(".json", "")
    creds = get_creds_from_file(token_file)
    if not creds: 
        status_box.error(f"❌ [{file_label}] 토큰 오류")
        return None
        
    try:
        youtube = googleapiclient.discovery.build('youtube', 'v3', credentials=creds)
        
        ch_res = youtube.channels().list(part='snippet,contentDetails', mine=True).execute()
        if not ch_res['items']: return None
        ch_item = ch_res['items'][0]
        ch_name = ch_item['snippet']['title']
        uploads_id = ch_item['contentDetails']['relatedPlaylists']['uploads'] 
        
        cache_name = f"cache_{os.path.basename(token_file)}"
        
        last_update = get_last_update_raw(cache_name)
        
        if last_update:
            target_limit_dt = last_update - timedelta(days=7)
            status_box.info(f"🔄 [{ch_name}] 최신화 (업로드일 {target_limit_dt.strftime('%m-%d')} 이후 탐색)")
        else:
            target_limit_dt = datetime.utcnow() - timedelta(days=365*2) 
            status_box.warning(f"⚠️ [{ch_name}] 기록 없음 -> 최근 2년 탐색")

        limit_date_str = target_limit_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        cached_videos = load_from_mongodb(cache_name)
        cached_ids = {v['id'] for v in cached_videos}
        
        new_videos = []; next_pg = None; stop = False
        
        while not stop:
            req = youtube.playlistItems().list(
                part='snippet,contentDetails,status', 
                playlistId=uploads_id, 
                maxResults=50, 
                pageToken=next_pg
            )
            res = req.execute()
            
            items = res.get('items', [])
            if not items:
                if not res.get('nextPageToken'): stop = True
                else:
                    next_pg = res.get('nextPageToken'); time.sleep(0.1); continue
            
            for item in items:
                privacy_status = item.get('status', {}).get('privacyStatus')
                if privacy_status != 'public':
                    continue

                vid = item['snippet']['resourceId']['videoId']
                
                real_publish_at = item['contentDetails'].get('videoPublishedAt')
                upload_at = item['snippet']['publishedAt']
                
                if upload_at < limit_date_str:
                    stop = True
                    break 

                if not real_publish_at:
                    continue

                if vid in cached_ids:
                    continue 
                else:
                    new_videos.append({
                        'id': vid, 'title': item['snippet']['title'], 
                        'date': real_publish_at, 
                        'description': item['snippet']['description']
                    })
            
            if len(new_videos) > 0 and len(new_videos) % 50 == 0:
                status_box.markdown(f"🏃 **[{ch_name}]** 탐색 중... +{len(new_videos)}")
            
            if stop: break
            next_pg = res.get('nextPageToken')
            if not next_pg: stop = True
            time.sleep(0.1) 
        
        final_list = new_videos + cached_videos
        final_list = list({v['id']:v for v in final_list}.values())
        
        is_ok, msg = save_to_mongodb(cache_name, final_list, update_source=update_source)
        
        if is_ok:
            if new_videos:
                status_box.success(f"🔥 **[{ch_name}] +{len(new_videos)}건 업데이트**")
                log_to_db('success', f"[{ch_name}] 업데이트 완료 ({update_source})", f"추가: {len(new_videos)}")
            else:
                status_box.success(f"✅ **[{ch_name}] 최신 상태 (변동없음)**")
        else:
            status_box.error(f"저장 실패: {msg}")
            log_to_db('error', f"[{ch_name}] 저장 실패", msg)
        
        return {'creds': creds, 'name': ch_name, 'videos': final_list}
        
    except Exception as e:
        status_box.error(f"에러: {e}")
        log_to_db('fatal', f"[{ch_name}] 로직 에러", str(e))
        return {'error': str(e)}

def process_analysis_channel(channel_data, keyword, vid_start, vid_end, anl_start, anl_end):
    creds = channel_data['creds']; videos = channel_data['videos']
    norm_kw = normalize_text(keyword)
    
    # [1] 필터링 (Keyword & Date)
    temp_target_ids = [] 
    id_map = {}; date_map = {}
    
    for v in videos:
        if norm_kw in normalize_text(v['title']) or norm_kw in normalize_text(v.get('description','')):
            v_dt = parse_utc_to_kst_date(v['date'])
            if v_dt and (vid_start <= v_dt <= vid_end):
                temp_target_ids.append(v['id'])
                id_map[v['id']] = v['title']
                date_map[v['id']] = v['date']
    
    target_ids = list(dict.fromkeys(temp_target_ids))
    if not target_ids: return None
    
    # [2] API 준비
    yt_anl = googleapiclient.discovery.build('youtubeAnalytics', 'v2', credentials=creds)
    youtube = googleapiclient.discovery.build('youtube', 'v3', credentials=creds)
    
    tot_v=0; tot_l=0; tot_s=0; 
    cnt_100k = 0; cnt_500k = 0; cnt_1m = 0
    rt_tot_v=0; rt_tot_l=0
    
    demo=defaultdict(float); traffic=defaultdict(float); country=defaultdict(float); daily=defaultdict(float); kws=defaultdict(float)
    w_avg_sum=0; v_for_avg=0; top_vids=[]
    
    # ===== [3] 배치 처리 함수 (독립 실행 보장 + 데모그래픽 분할) =====
    def fetch_batch_data(batch_ids):
        vid_str = ",".join(batch_ids)
        
        # [A] 실시간 데이터 (Data API)
        r_real = {}
        try:
            r_real_raw = youtube.videos().list(part='statistics,contentDetails', id=vid_str).execute()
            r_real = {item['id']: item for item in r_real_raw.get('items', [])}
        except Exception as e:
            print(f"Data API Error: {e}")

        # [B] Analytics 데이터
        r_main = {}; r_share = []; r_demo_rows = []; r_traf = []; r_keyw = []; r_ctry = []; r_daily = []
        
        try:
            # 1. Main Metrics
            r_main_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='views,likes,averageViewPercentage', dimensions='video', filters=f'video=={vid_str}').execute()
            r_main = {row[0]: {'v': row[1], 'l': row[2], 'p': row[3]} for row in r_main_raw.get('rows', [])}

            # 2. Shares
            r_share_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='shares', filters=f'video=={vid_str}').execute()
            r_share = r_share_raw.get('rows', [])

            # 3. Demographics [수정: maxResults=2500 추가]
            # 영상50개 * 14개(성별/연령조합) = 700행. 기본값(100)이면 뒤에 다 잘림. 넉넉하게 2500 설정.
            r_demo_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='viewerPercentage', dimensions='video,ageGroup,gender', filters=f'video=={vid_str}', maxResults=2500).execute()
            r_demo_rows = r_demo_raw.get('rows', [])

            # 4. Traffic Source
            r_traf_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='views', dimensions='insightTrafficSourceType', filters=f'video=={vid_str}').execute()
            r_traf = r_traf_raw.get('rows', [])

            # 5. Keywords
            try:
                r_kw_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='views', dimensions='insightTrafficSourceDetail', filters=f'video=={vid_str};insightTrafficSourceType==YT_SEARCH', maxResults=15, sort='-views').execute()
                r_keyw = r_kw_raw.get('rows', [])
            except: r_keyw = []

            # 6. Countries
            r_ctry_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='views', dimensions='country', filters=f'video=={vid_str}', maxResults=50).execute()
            r_ctry = r_ctry_raw.get('rows', [])

            # 7. Daily Stats
            r_daily_raw = yt_anl.reports().query(ids='channel==MINE', startDate=anl_start, endDate=anl_end, metrics='views', dimensions='day', filters=f'video=={vid_str}', sort='day').execute()
            r_daily = r_daily_raw.get('rows', [])
            
        except Exception as e:
            pass

        return (r_main, r_share, r_demo_rows, r_traf, r_keyw, r_ctry, r_daily, r_real)

    # [4] 메인 루프
    for i in range(0, len(target_ids), 50):
        batch = target_ids[i:i+50]
        
        try:
            results = fetch_batch_data(batch)
            process_queue = [(batch, results)]
        except:
            process_queue = []
            for single_id in batch:
                try:
                    res_single = fetch_batch_data([single_id])
                    process_queue.append(([single_id], res_single))
                except: pass

        # [5] 데이터 집계
        for batch_ids, (l_anl, l_s, l_demo_rows, l_tr, l_kws, l_ctr, l_day, l_rt) in process_queue:
            if l_s: tot_s += l_s[0][0]
            
            # [영상별 데모그래픽 맵핑]
            vid_demo_map = defaultdict(lambda: defaultdict(float))
            
            for r in l_demo_rows:
                vid, age, gen, pct = r[0], r[1], r[2], r[3]
                # 해당 영상의 기간 조회수 가져오기
                v_views = l_anl.get(vid, {'v':0}).get('v', 0)
                
                # 채널 전체 통계용 (가중치 적용)
                demo[f"{age}_{gen}"] += v_views * (pct / 100)
                
                # 영상 개별 통계용
                vid_demo_map[vid][f"{age}_{gen}"] = pct

            for r in l_tr: traffic[r[0]] += r[1]
            for r in l_kws: 
                if r[0]!='GOOGLE_SEARCH': kws[r[0]]+=r[1]
            for r in l_ctr: country[r[0]]+=r[1]
            for r in l_day: daily[r[0]]+=r[1]
            
            for vid_id in batch_ids:
                a_data = l_anl.get(vid_id, {'v':0,'l':0,'p':0})
                
                rt_item = l_rt.get(vid_id, {})
                rt_stat = rt_item.get('statistics', {})
                try:
                    rt_v = int(rt_stat.get('viewCount') or 0)
                    rt_l = int(rt_stat.get('likeCount') or 0)
                except:
                    rt_v = 0; rt_l = 0
                
                rt_tot_v += rt_v; rt_tot_l += rt_l
                
                if rt_v >= 1000000: cnt_1m += 1
                if rt_v >= 500000: cnt_500k += 1
                if rt_v >= 100000: cnt_100k += 1
                
                fin_v = a_data['v']; fin_l = a_data['l']
                tot_v += fin_v; tot_l += fin_l
                if fin_v>0 and a_data['p']>0:
                    w_avg_sum += (fin_v*a_data['p']); v_for_avg += fin_v
                
                if fin_v > 0 or rt_v > 0:
                    dur = parse_duration_to_minutes(rt_item.get('contentDetails',{}).get('duration'))
                    
                    my_demo_stats = {}
                    if vid_id in vid_demo_map:
                        for k, pct in vid_demo_map[vid_id].items():
                            my_demo_stats[k] = fin_v * (pct / 100)
                    
                    top_vids.append({
                        'id': vid_id, 'title': id_map.get(vid_id,'?'),
                        'views': rt_v, 'period_views': fin_v, 'period_likes': fin_l,
                        'avg_pct': a_data['p'], 'duration_min': dur,
                        'demo_stats': my_demo_stats
                    })
    
    top_vids.sort(key=lambda x: x['period_views'], reverse=True)
    
    return {
        'channel_name': channel_data['name'], 
        'video_count': len(target_ids),
        'total_views': tot_v, 'total_likes': tot_l, 'total_shares': tot_s,
        'avg_view_pct': (w_avg_sum/v_for_avg) if v_for_avg>0 else 0,
        'demo_counts': demo, 'traffic_counts': traffic, 'country_counts': country,
        'daily_stats': daily, 'keywords_counts': kws, 'top_video_stats': top_vids,
        'count_100k': cnt_100k, 'count_500k': cnt_500k, 'count_1m': cnt_1m,
        'rt_total_views': rt_tot_v,
        'rt_total_likes': rt_tot_l
    }

# [스케줄러]
def job_auto_update_data():
    print(f"⏰ [Auto] 시작: {datetime.now()}")
    token_files = glob.glob("token_*.json")
    if not token_files: return
    
    try:
        cnt = 0
        for tf in token_files:
            res = process_sync_channel(tf, None, update_source="auto")
            if res and 'error' not in res: cnt+=1
        
        load_from_mongodb.clear()
        get_last_update_time.clear()
        
        try:
            client = init_mongo()
            if client:
                client.get_database("yt_dashboard").get_collection("system_logs").insert_one({
                    'level': 'info', 'msg': "스케줄러 완료",
                    'detail': f"성공: {cnt}/{len(token_files)}",
                    'source': 'scheduler',
                    'timestamp': datetime.now()
                })
        except: pass
        
    except Exception as e:
        try:
            client = init_mongo()
            if client:
                client.get_database("yt_dashboard").get_collection("system_logs").insert_one({
                    'level': 'fatal', 'msg': "스케줄러 오류",
                    'detail': str(e),
                    'source': 'scheduler',
                    'timestamp': datetime.now()
                })
        except: pass

@st.cache_resource
def init_scheduler():
    s = BackgroundScheduler()
    s.add_job(job_auto_update_data, CronTrigger(hour='0,9,12,18', minute=0, timezone=pytz.timezone('Asia/Seoul')))
    s.start()

init_scheduler()
# endregion


# ==========================================
# [5. 메인 UI 및 실행 로직 (Main UI & Execution)]
# ==========================================
# [수정] 팝업 함수: 영상 ID를 받아서 플레이어 임베딩
@st.dialog("🎬 영상 재생 및 분석") 
def show_video_details(video_title, demo_stats, period_views, video_id):
    # 1. 유튜브 영상 플레이어 (가장 상단에 배치)
    st.video(f"https://youtu.be/{video_id}")
    
    st.markdown(f"<div style='font-size:1.1em;font-weight:bold;margin:10px 0'>{video_title}</div>", unsafe_allow_html=True)
    st.divider()

    # 2. 기존 데모그래픽 분석 로직
    if not demo_stats:
        st.warning("⚠️ 이 영상은 데모그래픽 데이터가 집계되지 않았습니다. (조회수 부족 등)")
        return

    fig, df, summary_text = get_pyramid_chart_and_df(demo_stats, period_views)
    
    if fig:
        st.markdown("##### 📊 시청자 성별/연령 분석")
        st.info(summary_text) 
        st.plotly_chart(fig, use_container_width=True)
        
        with st.expander("📋 상세 수치표 열기"):
             df_disp = df.copy()
             df_disp['조회수'] = df_disp['조회수'].apply(lambda x: f"{x:,}")
             df_disp['비율'] = df_disp['비율'].apply(lambda x: f"{x:.1f}%")
             st.dataframe(df_disp, use_container_width=True, hide_index=True)

st.title("📊 YT(PGC) Data Tracker")

all_token_files = glob.glob("token_*.json")
with st.expander("🔌 연결된 채널 리스트 / 연결 상태 보기", expanded=False):
    if all_token_files:
        connection_rows = [diagnose_channel_connection(tf) for tf in all_token_files]
        df_conn = pd.DataFrame(connection_rows)[['channel_name', 'status', 'reason', 'cached_videos', 'last_update']].copy()
        df_conn.columns = ['채널명', '연결상태', '사유', '캐시영상수', '마지막업데이트']
        st.dataframe(df_conn, use_container_width=True, hide_index=True)
    else:
        st.info("확인 가능한 token_*.json 파일이 없습니다.")
        st.caption("현재 코드만으로는 '연결되어야 할 전체 채널 마스터 목록'이 없어서, 존재하지 않는 채널까지 미연결로 자동 표기하진 못합니다.")


with st.sidebar:
    st.header("🎛️ 데이터 관리")
    if 'admin_auth' not in st.session_state: st.session_state['admin_auth'] = False
    
    if not st.session_state['admin_auth']:
        if st.text_input("관리자 비밀번호", type="password") == st.secrets.get("admin",{}).get("password",""):
            st.session_state['admin_auth'] = True; st.rerun()
            
    if st.session_state['admin_auth']:
        token_files = glob.glob("token_*.json")
        st.markdown("---")

        with st.expander("🔌 채널 연결 상태", expanded=False):
            if token_files:
                diag_rows = [diagnose_channel_connection(tf) for tf in token_files]
                df_diag = pd.DataFrame(diag_rows)[['channel_name', 'status', 'reason', 'cached_videos', 'last_update', 'token_file']].copy()
                df_diag.columns = ['채널명', '연결상태', '사유', '캐시영상수', '마지막업데이트', '토큰파일']
                st.dataframe(df_diag, use_container_width=True, hide_index=True)

                connected_cnt = int((pd.Series([r['status'] for r in diag_rows]) == '연결됨').sum())
                disconnected_cnt = len(diag_rows) - connected_cnt
                st.caption(f"연결됨 {connected_cnt}개 / 미연결 {disconnected_cnt}개")
            else:
                st.warning("token_*.json 파일이 없어 연결 상태를 확인할 채널이 없습니다.")
                st.caption("※ 현재 구조상 '예상 채널 목록'이 없으면, 파일이 아예 없는 채널까지 자동 식별할 수는 없습니다.")
        
        if token_files:
            last_ts = get_last_update_time(f"cache_{os.path.basename(token_files[0])}")
            if last_ts: st.info(f"🕒 DB 최신화: {last_ts}")
        
        if st.button("🔄 최신 영상 업데이트 (수동)", type="primary", use_container_width=True):
            st.session_state['channels_data'] = []
            ph = {tf: st.empty() for tf in token_files}
            ready = []
            ctx = get_script_run_ctx()
            def worker(tf, sb): 
                add_script_run_ctx(ctx=ctx)
                return process_sync_channel(tf, sb, update_source="manual")
            
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                fs = {ex.submit(worker, tf, ph[tf]): tf for tf in token_files}
                for f in as_completed(fs):
                    r = f.result()
                    if r and 'name' in r: ready.append(r)
            
            if ready:
                st.success("업데이트 완료!")
                load_from_mongodb.clear()
                get_last_update_time.clear()
                time.sleep(1)
                st.rerun()

        st.markdown("---")
        
        with st.expander("⚠️ DB 초기화 및 전체 재수집"):
            if 'admin_unlocked' not in st.session_state: st.session_state['admin_unlocked'] = False
            if not st.session_state['admin_unlocked']:
                if st.text_input("2차 비밀번호", type="password") == "dima1234":
                    st.session_state['admin_unlocked'] = True; st.rerun()
            
            if st.session_state['admin_unlocked']:
                st.warning("경고: 모든 데이터를 새로 수집합니다.")
                if st.button("🔥 전체 데이터 덮어쓰기", type="secondary"):
                    st.session_state['channels_data'] = []
                    ph = {tf: st.empty() for tf in token_files}
                    ready = []
                    ctx = get_script_run_ctx()
                    
                    def deep_worker(tf, sb):
                        add_script_run_ctx(ctx=ctx)
                        try:
                            client = init_mongo()
                            db = client.get_database("yt_dashboard")
                            cache_n = f"cache_{os.path.basename(tf)}"
                            db.get_collection("metadata").delete_one({"_id": cache_n})
                            db.get_collection("videos").delete_many({"source_file": cache_n})
                        except: pass
                        
                        return process_sync_channel(tf, sb, update_source="manual_reset")
                        
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                        fs = {ex.submit(deep_worker, tf, ph[tf]): tf for tf in token_files}
                        for f in as_completed(fs):
                            r = f.result()
                            if r and 'name' in r: ready.append(r)
                    if ready:
                        st.success("완료!")
                        load_from_mongodb.clear()
                        get_last_update_time.clear()
                        time.sleep(1)
                        st.rerun()

# [메인 로직]
if 'channels_data' not in st.session_state or not st.session_state['channels_data']:
    token_files = glob.glob("token_*.json")
    temp = []
    for tf in token_files:
        c_name = f"cache_{os.path.basename(tf)}"
        vids = load_from_mongodb(c_name)
        if vids:
            creds = get_creds_from_file(tf)
            if creds: temp.append({'creds': creds, 'name': os.path.basename(tf).replace("token_","").replace(".json",""), 'videos': vids})
    
    if temp: st.session_state['channels_data'] = temp
    else: st.info("👋 데이터 준비 중... (DB가 비어있다면 관리자 메뉴에서 업데이트하세요)")

if 'channels_data' in st.session_state and st.session_state['channels_data']:
    data = st.session_state['channels_data']
    tv = sum(len(c['videos']) for c in data)
    st.markdown(f"<div style='background:white;padding:10px;border-radius:8px;border:1px solid #eee;margin-bottom:20px'>✅ <b>채널:</b> {len(data)}개 | 📁 <b>영상:</b> {tv:,}개</div>", unsafe_allow_html=True)
    
    with st.form("anl_form"):
        st.subheader("🔍 통합 분석")
        c1,c2,c3 = st.columns([2,2,1])
        kw = c1.text_input("분석 IP", placeholder="예: 눈물의 여왕")

        today_kst = kst_now().date()
        default_start = today_kst.replace(day=1)
        date_range = c2.date_input("분석 기간(날짜)", value=(default_start, today_kst))

        cutoff_dt = confirmed_cutoff_dt(hours=48)
        cutoff_date = cutoff_dt.date()

        if st.form_submit_button("분석 시작", type="primary", use_container_width=True):
            if not kw.strip():
                st.error("키워드 입력 필요")
            else:
                p_s = date_range[0]
                p_e = date_range[1] if isinstance(date_range, (tuple, list)) and len(date_range)>1 else date_range[0]

                v_s, v_e = p_s, p_e
                a_s = p_s
                a_e = min(p_e, cutoff_date)

                st.session_state['anl_kw'] = kw
                st.session_state['anl_period'] = (p_s, p_e)
                st.session_state['anl_confirmed_dt'] = cutoff_dt
                st.session_state['anl_confirmed_end'] = a_e

                res = []
                bar = st.progress(0, "분석 중...")
                ctx = get_script_run_ctx()
                def w(cd, k, vs, ve, ast, aet):
                    add_script_run_ctx(ctx)
                    return process_analysis_channel(cd, k, vs, ve, ast, aet)

                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    fs = {ex.submit(w, c, kw, v_s, v_e, a_s, a_e): c for c in data}
                    dn = 0
                    for f in as_completed(fs):
                        dn += 1
                        bar.progress(dn/len(data), f"채널 {dn}/{len(data)}")
                        r = f.result()
                        if r: res.append(r)
                bar.empty()
                st.session_state['anl_res'] = res
                if not res:
                    st.warning("결과 없음 (기간/키워드 확인)")

    if 'anl_res' in st.session_state and st.session_state['anl_res']:
        raw = st.session_state['anl_res']
        st.divider()
        st.markdown(f"### 📊 결과: <span style='color:#2980b9'>{st.session_state['anl_kw']}</span>", unsafe_allow_html=True)
        tgt = raw

        p_s, p_e = st.session_state.get('anl_period', (None, None))
        cutoff_dt = st.session_state.get('anl_confirmed_dt', None)
        confirmed_end = st.session_state.get('anl_confirmed_end', None)
        
        if p_s and p_e and cutoff_dt and confirmed_end:
            if p_e > confirmed_end:
                st.caption(f"※ 최신 구간(오늘/어제 등)은 Analytics 확정 전이므로 상세 분석 차트에는 반영되지 않을 수 있습니다.")

        # 집계 변수 초기화
        fin_v=0; fin_cnt=0; fin_rt=0
        tot_100k=0; tot_500k=0; tot_1m=0
        
        stt=defaultdict(float); trf=defaultdict(float); ctr=defaultdict(float); day=defaultdict(float); kws=defaultdict(float)
        top_v=[]
        
        for d in tgt:
            fin_v+=d['total_views'];
            fin_rt+=d.get('rt_total_views',0)
            fin_cnt+=d['video_count']
            
            tot_100k+=d.get('count_100k', 0)
            tot_500k+=d.get('count_500k', 0)
            tot_1m+=d.get('count_1m', 0)
            
            for k,v in d['demo_counts'].items(): stt[k]+=v
            for k,v in d['traffic_counts'].items(): trf[k]+=v
            for k,v in d['country_counts'].items(): ctr[k]+=v
            for k,v in d['daily_stats'].items(): day[k]+=v
            for k,v in d['keywords_counts'].items(): kws[k]+=v
            top_v.extend(d['top_video_stats'])
            
        st.markdown("##### ⚡ 실시간 현황")
        m1,m2,m3,m4,m5 = st.columns(5)
        m1.metric("총 조회수 (실시간)", f"{int(fin_rt):,}")
        m2.metric("영상 수", f"{fin_cnt:,}")
        m3.metric("10만+", f"{tot_100k:,}")
        m4.metric("50만+", f"{tot_500k:,}")
        m5.metric("100만+", f"{tot_1m:,}")
        st.caption(f"※ 위 카드는 YouTube Data API 실시간 조회수 기준입니다. (업데이트: {kst_now().strftime('%Y-%m-%d %H:%M')} KST)")
        st.write("")
        st.divider()
        
        st.markdown("##### 📉 상세 분석")
        
        f_d, df_d, _ = get_pyramid_chart_and_df(stt, fin_v)
        if f_d:
            c1,c2=st.columns([1.6,1])
            with c1: 
                with st.container(border=True):
                    st.markdown("##### 👥 성별/연령 분포")
                    st.plotly_chart(f_d, use_container_width=True)
            with c2: 
                with st.container(border=True):
                    st.markdown("##### 📋 상세 데이터")
                    df_d_disp = df_d.copy()
                    df_d_disp['조회수'] = df_d_disp['조회수'].apply(lambda x: f"{x:,}")
                    df_d_disp['비율'] = df_d_disp['비율'].apply(lambda x: f"{x:.1f}%")
                    st.dataframe(df_d_disp, use_container_width=True, hide_index=True, height=300)
        st.write("")
            
        f_t = get_daily_trend_chart(day)
        if f_t: 
            with st.container(border=True):
                st.markdown("##### 🗓️ 일자별 조회수 추이")
                st.plotly_chart(f_t, use_container_width=True)
        st.write("")
        
        # [수정] Top 100 리스트 UI: 표(DataFrame)로 변경 + 선택(Click) 시 팝업
        st.markdown("##### 🥇 인기 영상 Top 100")
        st.caption("💡체크박스 선택시 해당영상의 데모그래픽 데이터 확인 가능")

        dedup = list({v['id']: v for v in (top_v or [])}.values())
        if not dedup:
            st.caption("데이터가 없습니다.")
        else:
            top100 = sorted(dedup, key=lambda x: x.get('period_views', 0), reverse=True)[:100]
            
            # DataFrame 생성
            df_table = pd.DataFrame(top100)
            df_table['title_disp'] = df_table['title'] # 원본 제목 보존
            df_table['link'] = df_table['id'].apply(lambda x: f"https://youtu.be/{x}")
            
            # 표시용 DF 정리
            df_show = df_table[['title', 'period_views', 'views', 'link']].copy()
            df_show.columns = ['제목', '기간조회수(확정)', '실시간누적(참고)', '영상보러가기']
            
            # [핵심] 숫자를 콤마(,)가 포함된 문자열로 변환
            df_show['기간조회수(확정)'] = df_show['기간조회수(확정)'].apply(lambda x: f"{int(x):,}")
            df_show['실시간누적(참고)'] = df_show['실시간누적(참고)'].apply(lambda x: f"{int(x):,}")
            
            # 표 출력 (selection_mode='single-row')
            event = st.dataframe(
                df_show,
                column_config={
                    "제목": st.column_config.TextColumn("영상 제목", width="large"),
                    # [수정] 데이터가 문자열이 되었으므로 NumberColumn 대신 기본 Column 사용
                    "기간조회수(확정)": st.column_config.Column("기간조회수(확정)"),
                    "실시간누적(참고)": st.column_config.Column("실시간누적(참고)"),
                    "영상보러가기": st.column_config.LinkColumn(display_text="Watch")
                },
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                height=400 
            )

            # 선택 이벤트 처리
            if event.selection.rows:
                idx = event.selection.rows[0]
                selected_item = top100[idx]
                # [수정] 마지막 인자로 id 추가 전달
                show_video_details(
                    selected_item['title'], 
                    selected_item.get('demo_stats'), 
                    selected_item['period_views'], 
                    selected_item['id']
                )

        st.write("")

        f_share = get_channel_share_chart(tgt)
        f_map = get_country_map(ctr)
        
        c_share, c_map = st.columns(2)
        with c_share:
             if f_share:
                with st.container(border=True):
                    st.markdown("##### 🏆 채널별 조회수 점유율")
                    st.plotly_chart(f_share, use_container_width=True)
        with c_map:
            if f_map:
                with st.container(border=True):
                    st.markdown("##### 🌍 글로벌 조회수 분포(국내 제외)")
                    st.plotly_chart(f_map, use_container_width=True)
        st.write("")

        r2_1, r2_2 = st.columns(2)
        f_tr = get_traffic_chart(trf); f_kw = get_keyword_bar_chart(kws)
        with r2_1: 
            if f_tr: 
                with st.container(border=True):
                    st.markdown("##### 🚦 유입 경로 Top 5")
                    st.plotly_chart(f_tr, use_container_width=True)
        with r2_2: 
            if f_kw: 
                with st.container(border=True):
                    st.markdown("##### 🔍 Top 10 검색어")
                    st.plotly_chart(f_kw, use_container_width=True)