# Dockerfile — owns how the FloorGuide backend becomes a Linux container image for App Platform.
# Instance 4 (deploy/docs) owns this file; the application it serves is instance 1's backend/app.py.

# Pinned base tag, not `slim` or `3.13-slim`: a floating tag would move under the build and
# resolve a different Python than the 3.13.5 this project was pinned against.
FROM python:3.13.5-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Non-root from the start. Nothing in this service writes outside /app, and root is not needed to serve HTTP.
#
# The chown of /app ITSELF is load-bearing, not tidiness. `WORKDIR /app` creates the directory as
# root:root 755, and `COPY --chown` below only sets ownership of the files placed INSIDE it. Without
# this, appuser cannot create new files in /app, and the app dies at import with
# `sqlite3.OperationalError: unable to open database file` when SqliteSaver opens ./checkpoints.db.
# backend/ingest.py needs the same write permission to create ./chroma at startup.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && chown appuser:appuser /app

# Dependencies in their own layer, before the code, so editing a route does not re-resolve the whole stack.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code, the corpus and the asset records. .dockerignore keeps the front end,
# node_modules, chroma/, *.db and every .env out; it deliberately keeps .git in, for the commit on /health.
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8080

# The contract's liveness signal. python:slim has no curl, so ask Python rather than install a package for it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=4).status==200 else 1)"

# sh -c so ${PORT} expands: App Platform sets the port through the environment.
# No --reload, ever, in an image.
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
