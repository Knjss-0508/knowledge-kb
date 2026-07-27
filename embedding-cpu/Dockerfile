FROM docker.m.daocloud.io/library/python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY embedding-cpu/requirements.txt .
RUN pip install --index-url "${PYTORCH_INDEX_URL}" torch==2.6.0 \
    && pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY embedding-cpu/app /app/app

EXPOSE 80

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]
