FROM ubuntu:jammy AS python-builder

RUN apt-get update && \
    apt-get install -y python3 python3-venv && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv && \
    /venv/bin/pip install -U pip setuptools

COPY requirements.txt /app/requirements.txt
RUN /venv/bin/pip install --no-deps --requirement /app/requirements.txt

# Jinja2 complains if this is installed the "regular" way
# https://jinja.palletsprojects.com/en/3.1.x/api/#loaders
# So we install here instead as an editable installation and also copy over the app directory
COPY . /app
RUN /venv/bin/pip install --no-deps -e /app


FROM ubuntu:jammy

# Don't buffer stdout and stderr as it breaks realtime logging
ENV PYTHONUNBUFFERED 1

# Make httpx use the system trust roots
# By default, this means we use the CAs from the ca-certificates package
ENV SSL_CERT_FILE /etc/ssl/certs/ca-certificates.crt

# Create the user that will be used to run the app
ENV APP_UID 1001
ENV APP_GID 1001
ENV APP_USER app
ENV APP_GROUP app
RUN groupadd --gid $APP_GID $APP_GROUP && \
    useradd \
      --no-create-home \
      --no-user-group \
      --gid $APP_GID \
      --shell /sbin/nologin \
      --uid $APP_UID \
      $APP_USER

RUN apt-get update && \
    apt-get install --no-install-recommends --no-install-suggests -y ca-certificates python3 tini && \
    rm -rf /var/lib/apt/lists/*

COPY --from=python-builder /venv /venv
COPY --from=python-builder /app /app

USER $APP_UID
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/venv/bin/azimuth-apps-operator"]
