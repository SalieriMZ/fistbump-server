FROM python:3.13-slim

WORKDIR /app
COPY server.py .

ENV PYTHONUNBUFFERED=1

# Writable location for users.db, the HMAC/token secrets, bans, and logs.
# USER nobody cannot write /app or /opt, so persist state under a mounted
# volume — otherwise every restart regenerates the token secret and
# invalidates all outstanding REFRESH tokens.
ENV FISTBUMP_DATA_DIR=/data
RUN mkdir -p /data && chown nobody /data
VOLUME ["/data"]

# Canonical ports — must match fistbump.service / deploy.sh / server.py
# defaults: TCP API + matchmaking, UDP NAT-punch, and the UDP relay fallback
# (omitting --relay-port leaves CGNAT peers with no relay → matches silently
# fail).
EXPOSE 19000/tcp
EXPOSE 19001/udp
EXPOSE 19002/udp

USER nobody

ENTRYPOINT ["python", "-u", "server.py"]
CMD ["--host", "0.0.0.0", "--tcp-port", "19000", "--udp-port", "19001", "--relay-port", "19002"]
