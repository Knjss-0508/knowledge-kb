FROM docker.m.daocloud.io/library/python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY embedding-cpu/requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
    && pip install -r requirements.txt

COPY embedding-cpu/app /app/app

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
