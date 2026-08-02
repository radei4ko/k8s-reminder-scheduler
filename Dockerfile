FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

# Not root: the same container image runs as the API, the worker, and the
# CronJob's seed job - none of the three need more than an ordinary user.
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /code
USER appuser

EXPOSE 8000

# The default command runs the API. The worker Deployment and the seed
# CronJob override this with their own command in their k8s manifests -
# same image, three different roles.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
