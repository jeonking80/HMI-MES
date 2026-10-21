"""Vercel Python Function entrypoint; credentials are injected only at runtime."""

from apps.api.serverless import vercel_application

app = vercel_application()
