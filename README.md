# sdr-server

Network SDR control service for local radios (HackRF today), with:

- Device discovery (`/devices`)
- Start/stop IQ streaming (`/streams/*` + `/ws/iq/{stream_id}`)
- Start/stop spectrum sweep (`/sweeps/*`)
- Recent sweep samples (`/sweeps/{id}/samples`)
- Built-in web viewer at `/` for tuning and live sweep/persistence plotting

## Why this layout

This keeps high-rate SDR I/O in native HackRF tools (`hackrf_transfer`, `hackrf_sweep`, both C-based)
while using Python/FastAPI for orchestration and remote control.

## Requirements

- Python 3.10+
- `hackrf-tools` installed on the SDR host (`hackrf_info`, `hackrf_transfer`, `hackrf_sweep`)
- Mock backend is disabled by default; enable with `SDR_ENABLE_MOCK=1` when needed.

## Run

```bash
cd /home/jake/workspace/SDR/sdr-server
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Then open:

- `http://localhost:8080/`

## Core API

- `GET /devices`
- `POST /streams/start`
- `POST /streams/{stream_id}/stop`
- `GET /streams`
- `WS /ws/iq/{stream_id}`
- `POST /sweeps/start`
- `POST /sweeps/{sweep_id}/stop`
- `GET /sweeps/{sweep_id}/samples`

## Example stream start

```bash
curl -s http://localhost:8080/streams/start \
  -H 'content-type: application/json' \
  -d '{
    "device_id":"hackrf:0",
    "center_freq_hz":1090000000,
    "sample_rate_sps":2000000,
    "lna_gain_db":16,
    "vga_gain_db":20,
    "amp_enable":false
  }'
```

## Notes

- IQ format over websocket is raw interleaved signed 8-bit bytes: `I,Q,I,Q,...`
- This scaffold has an optional mock backend (`mock:0`) for testing without hardware (`SDR_ENABLE_MOCK=1`).
- For internet exposure, put this behind WireGuard/Tailscale instead of open ports.
- Web viewer uses `hackrf_sweep` under the hood, so it is best for spectrum browsing/tuning.
