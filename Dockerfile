FROM python:3.13-alpine
LABEL org.opencontainers.image.title="wan-watchdog" org.opencontainers.image.version="2.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
# openssh-client: port bounce; su-exec: the probe containers drop from root after their DHCP lease
RUN apk add --no-cache openssh-client su-exec && adduser -D -u 1000 app && mkdir /data && chown app:app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
RUN chmod 755 /app/probe-entry.sh
USER app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "main.py"]
