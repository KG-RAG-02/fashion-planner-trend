# Build from repository root. Contains API code/config; compose can mount development code.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 PYTHONPATH=/app/src
WORKDIR /app
COPY docker/demo/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY src /app/src
COPY configs /app/configs
EXPOSE 8768
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8768/health', timeout=2).close()"
CMD ["python", "-m", "trend_agent.api.server", "--mode", "service", "--host", "0.0.0.0", "--port", "8768"]
