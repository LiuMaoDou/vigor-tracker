FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home vigor \
    && mkdir /data && chown vigor:vigor /data
COPY server.py index.html favicon.ico ./
USER vigor
ENV VIGOR_DB_PATH=/data/vigor.sqlite
EXPOSE 8765
CMD ["gunicorn", "--bind", "0.0.0.0:8765", "--workers", "1", "--threads", "4", "--access-logfile", "-", "server:app"]
