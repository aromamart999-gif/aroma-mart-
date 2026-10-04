FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN groupadd --system --gid 10001 amora \
    && useradd --system --uid 10001 --gid amora --home-dir /app amora \
    && mkdir -p /app/data /app/uploads \
    && chown -R 10001:10001 /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 . /app

USER 10001:10001
EXPOSE 4173
CMD ["python", "wsgi.py"]
