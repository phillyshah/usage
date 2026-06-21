# Pin to Debian 12 (bookworm). On Debian 13 (trixie) the barcode lib was
# renamed libdmtx0 -> libdmtx0t64 (the time_t transition), which breaks the
# apt install below. Bookworm keeps the original package names.
FROM python:3.11-slim-bookworm

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
