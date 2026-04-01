FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/neopterygii/stagekit-wled-bridge"
LABEL org.opencontainers.image.description="YARG/RB3E Stage Kit to WLED Bridge"

WORKDIR /app
COPY . .

# No dependencies to install — pure stdlib

EXPOSE 36107/udp
EXPOSE 8080/tcp

CMD ["python", "-u", "main.py"]
