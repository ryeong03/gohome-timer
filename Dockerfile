FROM python:3.11-slim
WORKDIR /app
# PostgreSQL 연동을 위한 필수 패키지 설치
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Railway의 PORT 환경 변수를 사용하도록 설정
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]