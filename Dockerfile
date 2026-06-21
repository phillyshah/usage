FROM python:3.11-slim

# System libraries for opencv, DataMatrix (libdmtx) and linear (zbar) decoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libdmtx0 libzbar0 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
