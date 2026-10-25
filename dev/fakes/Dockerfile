ARG BUILD_FROM
FROM ${BUILD_FROM}
COPY certs/ /usr/local/share/ca-certificates/
RUN if ls /usr/local/share/ca-certificates/*.pem >/dev/null 2>&1; then \
      for f in /usr/local/share/ca-certificates/*.pem; do cat "$f" >> /etc/ssl/certs/ca-certificates.crt; done; fi
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt
RUN pip3 install --no-cache-dir fastapi==0.143.0 "uvicorn==0.54.0" "httpx>=0.28,<1" python-multipart
WORKDIR /fakes
COPY *.py ./
# bypass the base image's s6 init: it clears the environment, SUPERVISOR_TOKEN included (the add-on goes through with-contenv)
ENTRYPOINT []
CMD ["python3", "main.py"]
