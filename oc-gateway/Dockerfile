FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --home /nonexistent gw
WORKDIR /app
COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway/ /app/
COPY config/ /app/config/
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"
# ONE worker + threads: routing state (sticky failover, rate estimates) lives in this process; SQLite persists the rest.
CMD ["gunicorn","-b","0.0.0.0:8080","-w","1","--threads","32","--timeout","600","--access-logfile","-","wsgi:app"]
