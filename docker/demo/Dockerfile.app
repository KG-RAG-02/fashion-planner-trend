# 파이썬 환경(가상환경 대신)을 구워 넣은 앱 이미지. 코드(src, scripts, configs, tests)는 넣지 않는다:
# docker-compose.yml의 demo 서비스가 호스트 폴더를 연결하므로 파일을 고치면 바로 반영된다.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
CMD ["sleep", "infinity"]
