# All-in-one image for cloud deployment (Render, etc.):
# runs the three regional node processes AND the gateway in one container.
# The gRPC ports (5001-5003) stay internal; only the gateway port is exposed.
#
#   docker build -t traffic-incident-system .
#   docker run -p 8000:8000 traffic-incident-system
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" grpcio protobuf httpx pydantic

COPY backend ./backend
COPY frontend ./frontend
COPY proto ./proto
COPY run.py .

# databases are created here on first start (ephemeral - fresh demo data per restart)
ENV DATA_DIR=/app/data

EXPOSE 8000
CMD ["python", "run.py"]
