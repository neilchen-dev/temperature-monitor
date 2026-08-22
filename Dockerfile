FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 以非 root 运行；uid/gid 固定为 1000，便于宿主对 data/、logs/ 授权。
RUN groupadd -g 1000 appuser && useradd -u 1000 -g 1000 -m appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R 1000:1000 /app/data /app/logs
USER appuser

ARG BUILD_VERSION=latest

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="amd64"

EXPOSE 5000

CMD ["python","app.py"]
