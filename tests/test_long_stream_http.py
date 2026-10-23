"""Real loopback HTTP test; no provider requests or real account credentials.

Extended smoke: python -m tests.test_long_stream_http --seconds 130
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import FastAPI, Request

import server
import streaming
from adapter import UpstreamTransientError


def run_smoke(seconds: float, heartbeat: float) -> dict:
    states = []

    def acquire(**kwargs):
        state = {"closed": False, "released": False}
        states.append(state)
        def tokens(session, prompt, **options):
            try:
                # Deliberately produce no upstream events longer than the
                # client's idle deadline. The server must send heartbeats.
                time.sleep(seconds)
                yield {"__type": "thinking", "content": "synthetic reasoning"}
                yield "synthetic answer"
                if "fail" in prompt:
                    raise UpstreamTransientError("synthetic disconnect")
            finally:
                state["closed"] = True
        return SimpleNamespace(
            acct=SimpleNamespace(id="synthetic-account"), parent_message_id=None,
            adapter=SimpleNamespace(chat_stream=tokens), prepare_prompt=lambda p: p,
            create_session=lambda *a, **kw: "synthetic-session",
            record_message_id=lambda *a, **kw: None,
            release=lambda: state.update(released=True),
        )

    app = FastAPI()
    @app.post("/v1/responses")
    async def responses(req: server.ResponsesRequest, request: Request):
        return await server.responses(req, request)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    async def clients(port):
        timeout = httpx.Timeout(max(2, heartbeat * 2), connect=5)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=timeout, trust_env=False) as client:
            async def consume(mode):
                started = last = time.monotonic()
                gaps, events = [], []
                async with client.stream("POST", "/v1/responses", json={"model": "glm-5.3-flash", "input": mode, "stream": True}) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            now = time.monotonic()
                            gaps.append(now - last)
                            last = now
                            events.append(json.loads(line[6:]))
                kinds = [event["type"] for event in events]
                assert "response.reasoning_summary_text.delta" in kinds
                assert "response.output_text.delta" in kinds
                assert kinds.count("response.in_progress") > 1
                if mode == "fail":
                    assert kinds[-1] == "response.failed"
                    assert "response.completed" not in kinds
                else:
                    assert kinds[-1] == "response.completed"
                    assert "error" not in kinds
                assert [event["sequence_number"] for event in events] == list(range(len(events)))
                return {"mode": mode, "elapsed_seconds": round(time.monotonic()-started, 3),
                        "max_event_gap_seconds": round(max(gaps), 3),
                        "progress_events": kinds.count("response.in_progress"), "terminal": kinds[-1]}
            tasks = [asyncio.create_task(consume(mode)) for mode in ("complete", "fail")]
            await asyncio.sleep(min(0.1, seconds/2))
            health_start = time.monotonic()
            assert (await client.get("/health")).status_code == 200
            health_latency = time.monotonic() - health_start
            assert health_latency < 1
            results = await asyncio.gather(*tasks)
            return {"requests": results, "health_latency_seconds": round(health_latency, 3)}

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, access_log=False, log_level="error")
        instance = uvicorn.Server(config)
        worker = threading.Thread(target=instance.run, kwargs={"sockets": [sock]}, daemon=True)
        with patch.object(server, "_acquire", acquire), \
             patch.object(server, "_check_api_auth", lambda req: None), \
             patch.object(server, "_check_rate_limit", lambda req: {}), \
             patch.object(server, "get_stats", lambda: SimpleNamespace(record=lambda *a, **kw: None)), \
             patch.object(streaming, "HEARTBEAT_INTERVAL", heartbeat):
            worker.start()
            try:
                deadline = time.monotonic() + 5
                while not instance.started:
                    assert time.monotonic() < deadline, "loopback server did not start"
                    time.sleep(0.01)
                result = asyncio.run(clients(port))
            finally:
                instance.should_exit = True
                worker.join(timeout=5)
            assert not worker.is_alive()
        assert len(states) == 2 and all(state["closed"] and state["released"] for state in states)
        result["accounts_released"] = len(states)
        result["real_upstream_requests"] = 0
        return result


def test_loopback_http_streams_have_heartbeats_and_terminal_events():
    run_smoke(seconds=0.15, heartbeat=0.02)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=130)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    print(json.dumps(run_smoke(args.seconds, min(15, args.seconds / 4)), indent=2))
