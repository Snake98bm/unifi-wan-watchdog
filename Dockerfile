FROM python:3.13-alpine
LABEL org.opencontainers.image.title="wan-watchdog" org.opencontainers.image.version="1.0"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apk add --no-cache openssh-client && adduser -D -u 1000 app && mkdir /data && chown app:app /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
USER app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "main.py"]
