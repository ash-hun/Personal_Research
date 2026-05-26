"""
Anthropic API proxy that captures and logs request bodies to JSONL.

Usage:
    uv run python module/proxy_logger.py

Configure Claude Code to route through proxy:
    ANTHROPIC_BASE_URL=http://localhost:8082  (in .env, then restart Claude Code)
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from aiohttp import web

PORT = int(os.getenv('PROXY_PORT', '8082'))
UPSTREAM = "https://api.anthropic.com"
LOG_FILE = Path("dataset/captured_requests.jsonl")

_SKIP_HEADERS = frozenset(
    ["host", "content-length", "content-encoding", "transfer-encoding"]
)


def _log_request(body_bytes: bytes, path: str) -> None:
    try:
        body = json.loads(body_bytes)
    except Exception:
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "path": path,
        "model": body.get("model", ""),
        "tool_count": len(body.get("tools", [])),
        "tools": body.get("tools", []),
        "tool_choice": body.get("tool_choice"),
        "stream": body.get("stream", False),
        "messages_count": len(body.get("messages", [])),
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(
        f"[proxy] {path} | model={entry['model']} "
        f"| tools={entry['tool_count']} | stream={entry['stream']}"
    )


def _fwd_headers(request: web.Request) -> dict:
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _SKIP_HEADERS
    }


def _resp_headers(upstream_headers) -> dict:
    return {
        k: v
        for k, v in upstream_headers.items()
        if k.lower() not in _SKIP_HEADERS
    }


async def handle(request: web.Request) -> web.StreamResponse:
    body_bytes = await request.read()
    _log_request(body_bytes, request.path)

    is_stream = False
    try:
        is_stream = json.loads(body_bytes).get("stream", False)
    except Exception:
        pass

    fwd_hdrs = _fwd_headers(request)

    async with httpx.AsyncClient(base_url=UPSTREAM, timeout=300) as client:
        if is_stream:
            response = web.StreamResponse()
            async with client.stream(
                request.method,
                request.path,
                content=body_bytes,
                headers=fwd_hdrs,
            ) as upstream:
                response.set_status(upstream.status_code)
                for k, v in _resp_headers(upstream.headers).items():
                    response.headers[k] = v
                await response.prepare(request)
                async for chunk in upstream.aiter_bytes():
                    await response.write(chunk)
                await response.write_eof()
            return response
        else:
            upstream = await client.request(
                request.method,
                request.path,
                content=body_bytes,
                headers=fwd_hdrs,
            )
            return web.Response(
                status=upstream.status_code,
                body=upstream.content,
                headers=_resp_headers(upstream.headers),
            )


async def main() -> None:
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.router.add_route("*", "/{path_info:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", PORT)
    await site.start()
    print(f"[proxy] Listening  → http://localhost:{PORT}")
    print(f"[proxy] Logging to → {LOG_FILE.resolve()}")
    print(f"[proxy] .env hint  → ANTHROPIC_BASE_URL=http://localhost:{PORT}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
