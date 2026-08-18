"""GravityClaw Core server entry point."""

from __future__ import annotations

import argparse

import uvicorn


def run_gateway(*, host: str = "127.0.0.1", port: int = 8787, log_level: str = "info") -> None:
    uvicorn.run(
        "gravityclaw.api:create_app",
        host=host,
        port=port,
        log_level=log_level,
        factory=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    run_gateway(host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
