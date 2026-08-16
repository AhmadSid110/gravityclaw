"""GravityClaw Core server entry point."""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    uvicorn.run(
        "gravityclaw.api:create_app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        factory=True,
    )


if __name__ == "__main__":
    main()
