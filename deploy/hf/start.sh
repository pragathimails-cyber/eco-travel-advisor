#!/bin/bash
# Start the action server, Rasa and nginx in one container.
# API keys are provided as HuggingFace Space "Secrets" (environment variables).
set -e
rasa run actions --actions actions --port 5055 &
rasa run --enable-api --port 5005 &
nginx -g "daemon off;"
