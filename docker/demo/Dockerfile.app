# Build from repository root. Contains API code/config; compose can mount development code.
# Runs as uid/gid 10001 and never writes under /app, so it works with a read-only root filesystem
# (give it a tmpfs at /tmp; HOME=/tmp).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 PYTHONPATH=/app/src
WORKDIR /app
COPY docker/demo/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin app
COPY src /app/src
COPY configs /app/configs
ENV HOME=/tmp
USER 10001:10001
EXPOSE 8768
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8768/health', timeout=2).close()"
CMD ["python", "-m", "trend_agent.api.server", "--mode", "service", "--host", "0.0.0.0", "--port", "8768"]
