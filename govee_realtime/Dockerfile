ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-websocket-client

COPY engine/ /app/
COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
