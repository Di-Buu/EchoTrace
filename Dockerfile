FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY apps/api/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY apps/api/app /app/app
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
