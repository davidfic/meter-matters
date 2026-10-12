FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELEC_DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code only. .dockerignore keeps every bill, export and secret out of the image.
# test-report.json is the last test run's results, written by the deploy; the [n] lets
# the build go on without one.
COPY *.py test-report.jso[n] ./
COPY templates ./templates
COPY static ./static

RUN useradd --system --uid 10001 --create-home --home-dir /home/elec elec \
    && mkdir /data && chown elec /data
USER elec
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# Workers, threads and the scheduler are set in gunicorn.conf.py.
CMD ["gunicorn", "wsgi:app"]
