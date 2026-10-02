FROM python:3.12-slim

# gcc is needed to build a couple of scientific wheels on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Train the predictor at build time. Doing it here rather than at startup keeps
# container boot fast and makes the image self-contained -- no first-request
# stall while scikit-learn fits 3000 rows.
RUN python scripts/train_model.py --samples 4000

# The ledger and the signing key must survive a container restart.
VOLUME ["/app/data"]

ENV GS_HOST=0.0.0.0 GS_PORT=5000 PYTHONUNBUFFERED=1
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/api/health').status==200 else 1)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
