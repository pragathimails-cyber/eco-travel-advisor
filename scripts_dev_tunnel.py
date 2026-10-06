"""
scripts_dev_tunnel.py - expose the local Rasa server through a secure HTTPS
tunnel for development/testing only (e.g. to test on a phone).

Usage:
    pip install pyngrok
    export NGROK_AUTHTOKEN=...        # from your ngrok dashboard
    python scripts_dev_tunnel.py
Then set window.RASA_URL in frontend/config.js to the printed https URL.
"""
import os
from pyngrok import conf, ngrok

conf.get_default().auth_token = os.environ["NGROK_AUTHTOKEN"]   # never hard-code
tunnel = ngrok.connect(5005, "http")                             # HTTPS public URL
print("Rasa REST channel available at:", tunnel.public_url)
input("Tunnel open - press Enter to close it.\n")
ngrok.disconnect(tunnel.public_url)
