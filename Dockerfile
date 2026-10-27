# gps2cctv 실시간 자동 녹화 서버
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data /config \
    && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV GPS2CCTV_DB=/data/gps2cctv.sqlite3 \
    GPS2CCTV_DATA_DIR=/data \
    TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1

USER app
VOLUME ["/data"]
EXPOSE 5055

ENTRYPOINT ["gps2cctv"]
CMD ["--config", "/config/config.toml", "serve"]
