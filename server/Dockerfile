FROM python:3.13-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y git tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cashbacks \
    && useradd --uid 10001 --gid cashbacks --no-create-home --shell /usr/sbin/nologin cashbacks \
    && install -d --owner=cashbacks --group=cashbacks --mode=0755 /app /checkout
COPY server/requirements.txt /app/server/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /app/server/requirements.txt

COPY --chown=cashbacks:cashbacks \
    scripts/__init__.py scripts/cashbacks.py \
    /app/scripts/
COPY --chown=cashbacks:cashbacks \
    server/__init__.py server/app.py server/models.py server/repo_worker.py \
    /app/server/

USER cashbacks:cashbacks
WORKDIR /app
EXPOSE 8080
ENV CHECKOUT_DIR=/checkout

ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "server.app"]
