# Single-container image for HuggingFace Spaces (Docker SDK, port 7860).
# nginx serves the frontend and proxies /webhooks/* to Rasa, so only one
# public port is needed. The action server stays on localhost.
FROM rasa/rasa:3.6.20-full
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nginx && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY config.yml domain.yml endpoints.yml credentials.yml ./
COPY data ./data
COPY actions ./actions
COPY frontend /usr/share/nginx/html
COPY deploy/hf/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/hf/start.sh /app/start.sh
# Frontend calls the same origin; nginx forwards to Rasa.
RUN echo 'window.RASA_URL = "";' > /usr/share/nginx/html/config.js \
 && rasa train --fixed-model-name eco-travel && chmod +x /app/start.sh
ENV ACTION_ENDPOINT_URL=http://localhost:5055/webhook
EXPOSE 7860
ENTRYPOINT ["/app/start.sh"]
