FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --home /app --uid 1000 netops \
 && mkdir -p /data /secrets && chown netops /data /secrets
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
ENV NETOPS_DATA_DIR=/data NETOPS_CREDENTIAL_KEY_FILE=/secrets/credential.key
USER netops
EXPOSE 8000
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
