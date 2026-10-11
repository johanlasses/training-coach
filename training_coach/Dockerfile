ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /opt/training_coach
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
