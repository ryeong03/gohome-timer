import os
import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from pydantic import BaseModel

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
# Railway Variables 탭에서 직접 설정해야 작동
SECRET_ADMIN_KEY = os.getenv("ADMIN_PASSWORD")

def get_db_connection():
    # 데이터베이스 연결 객체 생성
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """서버 시작 시 테이블이 없으면 생성하고 기본값(18:00)을 삽입합니다."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS timer_settings (
            id SERIAL PRIMARY KEY,
            hour INTEGER,
            minute INTEGER
        )
    """)
    cur.execute("SELECT COUNT(*) FROM timer_settings")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO timer_settings (hour, minute) VALUES (18, 0)")
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

@app.get("/api/clock-out")
def get_time_left():
    """현재 설정된 퇴근 시간과 남은 초를 반환합니다."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT hour, minute FROM timer_settings LIMIT 1")
        h, m = cur.fetchone()
        cur.close()
        conn.close()

        now = datetime.now()
        target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        time_left = target_time - now
        
        return {
            "seconds_left": int(time_left.total_seconds()),
            "target_time": f"{h:02d}:{m:02d}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="데이터베이스 조회 실패")

@app.post("/api/admin/set-time")
def set_target_time(data: TimeUpdate):
    """관리자 비밀번호를 확인한 후 퇴근 시간을 업데이트합니다."""
    # 환경 변수가 설정되지 않았거나 입력값이 다르면 차단
    if not SECRET_ADMIN_KEY or data.password != SECRET_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="승인되지 않은 요청입니다.")
    
    if not (0 <= data.hour <= 23 and 0 <= data.minute <= 59):
        raise HTTPException(status_code=400, detail="올바른 시간 형식이 아닙니다.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE timer_settings SET hour = %s, minute = %s WHERE id = 1",
            (data.hour, data.minute)
        )
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "세령님의 명령으로 퇴근 시간이 강제 조작되었습니다!! ! 🐬✨🐬"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="데이터베이스 업데이트 실패")