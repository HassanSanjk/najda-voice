"""
Entrypoint for running Najda Voice locally or on EC2.

Usage:
    python run.py

For production/demo on EC2, prefer running via scripts/start.sh,
which wraps this with logging and readiness checks.
"""

import uvicorn

from config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        reload=settings.app_env == "development",
    )
