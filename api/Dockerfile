FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
WORKDIR /app
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# shared/ carries the structured logger used by both api and worker, so the
# build context is the repo root.
COPY shared/ ./shared/
COPY api/ .
RUN useradd --create-home --uid 10001 relay && chown -R relay:relay /app
USER relay
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
