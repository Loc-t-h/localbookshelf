# LocalBookshelf. Build: docker compose build
#
# The library is mounted read-only at /library -- see docker-compose.yml. That
# is not a convenience, it is the app's first design tenet enforced by the
# kernel instead of by careful coding.

FROM python:3.12-slim-bookworm AS build

WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies resolve from pyproject; PyMuPDF and Pillow both publish
# manylinux wheels for amd64 and arm64, so no compiler is needed here.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .


FROM python:3.12-slim-bookworm

# `unar` is the only external binary we need, and only for CBR comics.
RUN apt-get update \
 && apt-get install -y --no-install-recommends unar \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# UID/GID 1000 matches the first user account on a stock Ubuntu install, so a
# read-only bind mount of that user's books just works. Override with `user:`
# in compose if your library is owned by someone else.
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --create-home app \
 && mkdir -p /data /library \
 && chown -R app:app /data

USER app
WORKDIR /home/app
VOLUME ["/data"]
EXPOSE 8080

# Uses the app's own /healthz endpoint. No curl in the image for this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# No library path here: it comes from $LIBRARY_PATH, which compose pins to
# /library (the mount point) rather than the host path of the same name.
ENTRYPOINT ["localbookshelf", "--data-dir", "/data"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
