"""Start the recall-assistant API server.

Usage:
    uv run python scripts/serve.py [--host HOST] [--port PORT]
    PORT=9000 uv run python scripts/serve.py
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from recall_assistant.api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Recall Assistant API server")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind address (default 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Bind port (default 8000)",
    )
    args = parser.parse_args()

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
