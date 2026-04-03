"""
veFaaS startup compatibility entrypoint.

Some platforms default to: `python -m uvicorn main:app --host 0.0.0.0 --port 8000`
Our actual application object lives in `app.py`.
"""

from app import app

