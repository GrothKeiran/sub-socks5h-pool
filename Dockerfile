FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/app/data XDG_CACHE_HOME=/app/data/.cache TMPDIR=/app/data/tmp
COPY tools/install_core.py /app/tools/install_core.py
RUN python3 tools/install_core.py \
    && python3 -c "import shutil; shutil.rmtree('/app/.cache')" \
    && groupadd --gid 10001 pool && useradd --uid 10001 --gid pool --no-create-home pool \
    && mkdir -p /app/data/tmp /app/.cache/tmp /app/.cache/tests \
    && chown -R pool:pool /app/data /app/.cache
COPY subpool /app/subpool
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/sub-socks5h-pool/
EXPOSE 1080 8090
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)"
CMD ["python3", "-m", "subpool"]
