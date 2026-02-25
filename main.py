import os
import psycopg2
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from datetime import datetime
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()

# CORS 설정: 어떤 도메인에서든 접근 가능하게게
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Railway가 주입해주는 환경 변수들
DATABASE_URL = os.getenv("DATABASE_URL")
# slug별 관리자 비밀번호 (환경 변수로 분리) — se, min, tutoring
ADMIN_PASSWORD_SE = os.getenv("ADMIN_PASSWORD_SE") or os.getenv("ADMIN_PASSWORD")
ADMIN_PASSWORD_MIN = os.getenv("ADMIN_PASSWORD_MIN")
ADMIN_PASSWORD_TUTORING = os.getenv("ADMIN_PASSWORD_TUTORING")


def get_admin_password(slug: str) -> str | None:
    if slug == "se":
        return ADMIN_PASSWORD_SE
    if slug == "min":
        return ADMIN_PASSWORD_MIN
    if slug == "tutoring":
        return ADMIN_PASSWORD_TUTORING
    return None

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

class TimeUpdate(BaseModel):
    hour: int
    minute: int
    password: str
    slug: str = "se" 

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

        

@app.post("/api/admin/set-time")
def set_target_time(data: TimeUpdate):
    """관리자 비밀번호를 확인한 후 해당 slug의 퇴근/종료 시간을 업데이트합니다."""
    if data.slug not in ("se", "min", "tutoring"):
        raise HTTPException(status_code=400, detail="올바른 slug가 아닙니다: se, min, tutoring")
    expected_pw = get_admin_password(data.slug)
    if not expected_pw or data.password != expected_pw:
        raise HTTPException(status_code=403, detail="승인되지 않은 요청입니다.")
    if not (0 <= data.hour <= 23 and 0 <= data.minute <= 59):
        raise HTTPException(status_code=400, detail="올바른 시간 형식이 아닙니다.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE timer_settings SET hour = %s, minute = %s WHERE slug = %s",
            (data.hour, data.minute, data.slug)
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"message": f"'{data.slug}' 타이머가 {data.hour:02d}:{data.minute:02d}로 설정되었습니다."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="데이터베이스 업데이트 실패")