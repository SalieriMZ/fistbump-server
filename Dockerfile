FROM python:3.13-slim

WORKDIR /app
COPY server.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 9000/tcp
EXPOSE 9001/udp

USER nobody

ENTRYPOINT ["python", "-u", "server.py"]
CMD ["--host", "0.0.0.0", "--tcp-port", "9000", "--udp-port", "9001"]
