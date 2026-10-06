# rhein-ruhr-fahrplan-mcp – schlanke Runtime
FROM python:3.13-slim

LABEL org.opencontainers.image.title="rhein-ruhr-fahrplan-mcp"
LABEL org.opencontainers.image.description="MCP-Server für ÖPNV-Fahrplandaten im Rhein-Ruhr-Gebiet (GTFS-Soll + EFA-Echtzeit)"
LABEL org.opencontainers.image.source="https://github.com/MarcProe/rhein-ruhr-fahrplan-mcp"

# curl für Notfall-Debugging (slim hat kein curl/wget)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ /app/src/
RUN pip install --no-cache-dir .

# Persistente GTFS-Datenbank
ENV FAHRPLAN_DB_PATH=/data/fahrplan.db
# Container-Betrieb: HTTP-Transport
ENV FAHRPLAN_MCP_TRANSPORT=http
ENV FAHRPLAN_MCP_HOST=0.0.0.0
ENV FAHRPLAN_MCP_PORT=8080
# WICHTIG: FAHRPLAN_AGENCY_IDS bewusst NICHT vorgegeben –
# ohne Konfiguration läuft der Container im Live-Only-Betrieb;
# die GTFS-Tools liefern dann einen Konfigurationshinweis.
VOLUME /data

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD python -m rhein_ruhr_fahrplan_mcp.healthcheck

CMD ["rhein-ruhr-fahrplan-mcp"]
