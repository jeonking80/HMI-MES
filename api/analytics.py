"""Vercel Python Function entrypoint for the analytics routes (same app, separate instances).

Real-time gateway intake, the 1 s screen poll and heavy analytics run in separate
functions so a CPU-bound report never delays equipment data or the live screen.
"""

from apps.api.serverless import vercel_application

app = vercel_application()
