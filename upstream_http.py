"""Shared bounded chat-stream timeout policy (not account health checks)."""
import os


def stream_timeout() -> tuple[int, int]:
    # curl_cffi uses connect + read as LOW_SPEED_TIME for stream=True.
    # Keep connection establishment short, but allow slow reasoning streams.
    try:
        budget = int(os.environ.get("UPSTREAM_STREAM_IDLE_TIMEOUT", "300"))
    except ValueError:
        budget = 300
    budget = min(1800, max(30, budget))
    return 15, budget - 15
