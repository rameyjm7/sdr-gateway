from __future__ import annotations

import asyncio
import json

import urllib.request
import websockets

API = "http://127.0.0.1:8080"
WS_BASE = "ws://127.0.0.1:8080"


def post_json(path: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


async def main():
    stream = post_json(
        "/streams/start",
        {
            "device_id": "mock:0",
            "center_freq_hz": 915_000_000,
            "sample_rate_sps": 2_000_000,
            "lna_gain_db": 16,
            "vga_gain_db": 20,
            "amp_enable": False,
        },
    )
    stream_id = stream["stream_id"]
    print("Started stream", stream_id)

    total = 0
    async with websockets.connect(f"{WS_BASE}/ws/iq/{stream_id}") as ws:
        for _ in range(20):
            chunk = await ws.recv()
            total += len(chunk)
    print("Received bytes:", total)

    post_json(f"/streams/{stream_id}/stop", {})
    print("Stopped stream")


if __name__ == "__main__":
    asyncio.run(main())
