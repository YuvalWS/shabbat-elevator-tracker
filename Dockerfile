# Shabbat Elevator Tracker - multi-arch (amd64, arm64) image.
FROM alpine:3.20

RUN apk add --no-cache python3 py3-pip py3-numpy py3-aiohttp py3-yaml ffmpeg tini tzdata \
 && addgroup -g 1000 app && adduser -D -u 1000 -G app app \
 && mkdir -p /config /data && chown app:app /data

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY shabbat_elevator_tracker ./shabbat_elevator_tracker
COPY config.example.yaml /usr/share/shabbat-elevator-tracker/config.example.yaml
RUN pip install --no-cache-dir --break-system-packages . \
 && rm -rf /src /root/.cache

USER app
WORKDIR /data
# /config/config.yaml is used when present; otherwise defaults + SET_* environment variables
ENV SET_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4)" || exit 1
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python3", "-m", "shabbat_elevator_tracker", "serve"]
