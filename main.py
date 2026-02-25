import os
import psycopg2
from pathlib import Path
from datetime import datetime, timedelta

import jwt
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()

# CORS 설정: 환경 변수 기반 허용 origin
_origins_env = os.getenv("ALLOWED_ORIGINS")
if _origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    # 환경 변수가 없으면 개발 편의를 위해 전체 허용
    ALLOWED_ORIGINS = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# Railway가 주입해주는 환경 변수들
DATABASE_URL = os.getenv("DATABASE_URL")
# 프론트엔드(공유 후 이동할) 기본 URL. 예: https://ryeong.github.io/gohome-timer/index.html
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://example.com/index.html")
# slug별 관리자 비밀번호 (환경 변수로 분리) — se, min, tutoring
ADMIN_PASSWORD_SE = os.getenv("ADMIN_PASSWORD_SE") or os.getenv("ADMIN_PASSWORD")
ADMIN_PASSWORD_MIN = os.getenv("ADMIN_PASSWORD_MIN")
ADMIN_PASSWORD_TUTORING = os.getenv("ADMIN_PASSWORD_TUTORING")

# JWT 설정 (반드시 환경 변수로만 설정되도록)
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET 환경 변수가 설정되지 않았습니다!")
REFRESH_SECRET = os.getenv("REFRESH_SECRET")
if not REFRESH_SECRET:
    raise RuntimeError("REFRESH_SECRET 환경 변수가 설정되지 않았습니다!")
JWT_ALGORITHM = "HS256"

# 간단한 IP 기반 레이트 리미트/실패 로그 상태 (메모리)
_rate_limit_state: dict[str, dict] = {}
_failed_login_state: dict[str, int] = {}


def get_admin_password(slug: str) -> str | None:
    if slug == "se":
        return ADMIN_PASSWORD_SE
    if slug == "min":
        return ADMIN_PASSWORD_MIN
    if slug == "tutoring":
        return ADMIN_PASSWORD_TUTORING
    return None


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=12))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_slug(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        slug = payload.get("slug")
        if slug not in ("se", "min", "tutoring"):
            raise HTTPException(status_code=403, detail="유효하지 않은 토큰입니다.")
        return slug
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")


def check_rate_limit(ip: str, key: str, limit: int = 10, window_sec: int = 60, block_sec: int = 3600) -> None:
    """
    매우 단순한 레이트 리미터.
    - 같은 IP + key 조합 기준
    - window_sec 동안 limit번 넘게 호출하면 block_sec 동안 차단
    """
    now = datetime.utcnow().timestamp()
    state_key = f"{ip}:{key}"
    info = _rate_limit_state.get(state_key)

    if info is None:
        _rate_limit_state[state_key] = {
            "window_start": now,
            "count": 1,
            "blocked_until": 0.0,
        }
        return

    # 차단 상태인지 확인
    if info.get("blocked_until", 0.0) > now:
        raise HTTPException(status_code=429, detail="요청이 너무 많아요. 잠깐 쉬어가기!")

    window_start = info.get("window_start", now)
    count = info.get("count", 0)

    # 새 윈도우 시작
    if now - window_start > window_sec:
        info["window_start"] = now
        info["count"] = 1
        info["blocked_until"] = 0.0
        return

    # 같은 윈도우 안
    count += 1
    info["count"] = count
    if count > limit:
        info["blocked_until"] = now + block_sec
        raise HTTPException(status_code=429, detail="시도를 너무 많이 했어요. 잠시 후에 다시 시도해주세요.")

def get_db_connection():
    # 데이터베이스 연결 객체 생성
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """서버 시작 시 테이블이 없으면 생성하고, slug별 기본값을 삽입합니다."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS timer_settings (
            id SERIAL PRIMARY KEY,
            slug VARCHAR(50) UNIQUE NOT NULL DEFAULT 'se',
            hour INTEGER,
            minute INTEGER
        )
    """)
    # 기존 테이블에 slug 컬럼이 없으면 추가 (마이그레이션)
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'timer_settings' AND column_name = 'slug'
    """)
    if cur.fetchone() is None:
        cur.execute("ALTER TABLE timer_settings ADD COLUMN slug VARCHAR(50) DEFAULT 'se'")
        cur.execute("UPDATE timer_settings SET slug = 'se' WHERE slug IS NULL OR id = 1")
    # 기존 값 마이그레이션: saeryung→se, friend→min
    cur.execute("UPDATE timer_settings SET slug = 'se' WHERE slug = 'saeryung'")
    cur.execute("UPDATE timer_settings SET slug = 'min' WHERE slug = 'friend'")
    # slug별 행이 없으면 기본 18:00으로 삽입
    for slug in ('se', 'min', 'tutoring'):
        cur.execute("SELECT 1 FROM timer_settings WHERE slug = %s", (slug,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO timer_settings (slug, hour, minute) VALUES (%s, 18, 0)",
                (slug,)
            )
    conn.commit()
    cur.close()
    conn.close()

# 웹 실행 시 DB 초기화 실행
try:
    init_db()
except Exception as e:
    print(f"DB Initialization Error: {e}")


class LoginRequest(BaseModel):
    slug: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TimeUpdate(BaseModel):
    hour: int
    minute: int

@app.get("/")
def read_root():
    return {"message": "세령님의 퇴근 타이머 백엔드가 정상 작동 중입니다!! ! 🐬✨"}


@app.get("/min")
def friend_page():
    """미녕 공익 퇴근 타이머 페이지 (공용 타이머 화면 재사용)."""
    return FileResponse(BASE_DIR / "index.html")


@app.get("/tutoring")
def tutoring_page():
    """주원이 수업 종료 타이머 페이지 (공용 타이머 화면 재사용)."""
    return FileResponse(BASE_DIR / "index.html")


BASE_IMG_URL = "https://ryeong03.github.io/gohome-timer/images"  # GitHub Pages 이미지 경로

SHARE_META = {
    "se": {
        "title": "세령이 탈출 타이머 🐬",
        "description": "세령이 퇴근까지 남은 시간 확인하기",
        "image": f"{BASE_IMG_URL}/og-se.png",
    },
    "min": {
        "title": "미녕 공익 퇴근 타이머 🪖",
        "description": "미녕이 공익 퇴근까지 남은 시간 확인하기",
        "image": f"{BASE_IMG_URL}/og-min.png",
    },
    "tutoring": {
        "title": "주원이 수업 종료 타이머 📚",
        "description": "주원이 수업 끝날 때까지 남은 시간 확인하기",
        "image": f"{BASE_IMG_URL}/og-tutoring.png",
    },
}


@app.get("/share/{slug}", response_class=HTMLResponse)
def share_page(slug: str):
    """
    링크 공유용 페이지.
    - 카톡/디코 등은 여기 OG 태그를 보고 미리보기를 만들고
    - 브라우저는 FRONTEND_BASE_URL?user=slug 로 리다이렉트된다.
    """
    if slug not in SHARE_META:
        raise HTTPException(status_code=404, detail="존재하지 않는 공유 링크입니다.")

    cfg = SHARE_META[slug]
    target_url = f"{FRONTEND_BASE_URL}?user={slug}"
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>{cfg['title']}</title>
  <meta property="og:title" content="{cfg['title']}">
  <meta property="og:description" content="{cfg['description']}">
  <meta property="og:image" content="{cfg['image']}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{target_url}">
  <meta http-equiv="refresh" content="0; url={target_url}">
</head>
<body>
  <p>공유 링크로 이동 중입니다... <a href="{target_url}">바로 이동</a></p>
</body>
</html>
"""
    return HTMLResponse(content=html)

def _get_time_left_by_slug(slug: str):
    """slug에 해당하는 타이머 설정으로 남은 초를 계산합니다."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT hour, minute FROM timer_settings WHERE slug = %s", (slug,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"타이머를 찾을 수 없습니다: {slug}")
    h, m = row
    now = datetime.now()
    target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
    time_left = target_time - now
    return {
        "seconds_left": int(time_left.total_seconds()),
        "target_time": f"{h:02d}:{m:02d}"
    }

@app.get("/api/clock-out")
def get_time_left():
    """세령님 퇴근 시간 (기본). 현재 설정된 퇴근 시간과 남은 초를 반환합니다."""
    try:
        return _get_time_left_by_slug("se")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="데이터베이스 조회 실패")

@app.get("/api/clock-out/{slug}")
def get_time_left_by_slug(slug: str):
    """slug별 타이머: se(세령 퇴근), min(미녕 공익 퇴근), tutoring(주원이 수업 종료)."""
    try:
        return _get_time_left_by_slug(slug)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="데이터베이스 조회 실패")

        

@app.post("/api/admin/login")
def admin_login(data: LoginRequest, request: Request):
    """slug별 관리자 로그인 후 JWT 토큰 발급."""
    # 레이트 리밋: IP별 로그인 시도 제한
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip, "admin_login")

    if data.slug not in ("se", "min", "tutoring"):
        raise HTTPException(status_code=400, detail="올바른 slug가 아닙니다: se, min, tutoring")
    expected_pw = get_admin_password(data.slug)
    if not expected_pw or data.password != expected_pw:
        # 비밀번호 실패 횟수 누적 및 경고 로그
        key = f"{client_ip}:{data.slug}"
        count = _failed_login_state.get(key, 0) + 1
        _failed_login_state[key] = count
        if count >= 5:
            print(f"⚠️ 경고: {client_ip} 에서 slug={data.slug} 비밀번호 연속 {count}회 실패")
        raise HTTPException(status_code=403, detail="비밀번호가 틀렸습니다.")
    # 성공 시 실패 카운트 리셋
    _failed_login_state[f"{client_ip}:{data.slug}"] = 0

    # 짧은 수명의 액세스 토큰 (예: 15분)
    access_token = create_access_token({"slug": data.slug}, expires_delta=timedelta(minutes=15))
    # 더 긴 수명의 리프레시 토큰 (예: 7일)
    refresh_payload = {"slug": data.slug, "exp": datetime.utcnow() + timedelta(days=7)}
    refresh_token = jwt.encode(refresh_payload, REFRESH_SECRET, algorithm=JWT_ALGORITHM)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


@app.post("/api/admin/refresh")
def refresh_access_token(data: RefreshRequest, request: Request):
    """리프레시 토큰으로 새로운 액세스 토큰 발급."""
    client_ip = request.client.host if request.client else "unknown"
    # 리프레시도 과도한 시도 방지용 레이트 리밋
    check_rate_limit(client_ip, "admin_refresh", limit=30, window_sec=60, block_sec=3600)

    try:
        payload = jwt.decode(data.refresh_token, REFRESH_SECRET, algorithms=[JWT_ALGORITHM])
        slug = payload.get("slug")
        if slug not in ("se", "min", "tutoring"):
            raise HTTPException(status_code=401, detail="다시 로그인하세요.")
        new_access = create_access_token({"slug": slug}, expires_delta=timedelta(minutes=15))
        return {"access_token": new_access}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="리프레시 토큰이 만료되었습니다. 다시 로그인하세요.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="다시 로그인하세요.")


@app.post("/api/admin/set-time")
def set_target_time(data: TimeUpdate, slug: str = Depends(get_current_slug)):
    """JWT로 인증된 slug의 퇴근/종료 시간을 업데이트합니다."""
    if not (0 <= data.hour <= 23 and 0 <= data.minute <= 59):
        raise HTTPException(status_code=400, detail="올바른 시간 형식이 아닙니다.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE timer_settings SET hour = %s, minute = %s WHERE slug = %s",
            (data.hour, data.minute, slug)
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"message": f"'{slug}' 타이머가 {data.hour:02d}:{data.minute:02d}로 설정되었습니다."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="데이터베이스 업데이트 실패")