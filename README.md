# sdr-gateway

Network SDR control service for local radios (HackRF + SoapySDR streaming for Sidekiq/Airspy/bladeRF/RTL-SDR), with:

- Device discovery (`/devices`)
- Start/stop IQ streaming (`/streams/*` + `/ws/iq/{stream_id}`)
- Start/stop spectrum sweep (`/sweeps/*`)
- Recent sweep samples (`/sweeps/{id}/samples`)
- Built-in web viewer at `/` for tuning and live sweep/persistence plotting

## Why this layout

This keeps high-rate SDR I/O in native tools (`hackrf_transfer`, `hackrf_sweep`, SoapySDR drivers)
while using Python/FastAPI for orchestration and remote control.

## Requirements

- Python 3.10+
- `hackrf-tools` installed on the SDR host (`hackrf_info`, `hackrf_transfer`, `hackrf_sweep`)
- For Soapy radios (Sidekiq/Airspy/bladeRF/RTL-SDR): `SoapySDRUtil --find` must show matching drivers, and Python must import `SoapySDR`
- Mock backend is disabled by default; enable with `SDR_ENABLE_MOCK=1` when needed.

## Run

```bash
cd /home/jake/workspace/SDR/sdr-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Then open:

- `http://localhost:8080/`
- `http://localhost:8080/docs` (Swagger)
- `http://localhost:8080/openapi.json`

## Run as system service (systemd)

Use the helper script:

```bash
cd /home/jake/workspace/SDR/sdr-gateway
scripts/sdr-gateway-service.sh install
scripts/sdr-gateway-service.sh enable
scripts/sdr-gateway-service.sh start
```

Install with auth token (recommended):

```bash
cd /home/jake/workspace/SDR/sdr-gateway
export SDR_GATEWAY_API_TOKEN="$(openssl rand -base64 48)"
scripts/sdr-gateway-service.sh install
scripts/sdr-gateway-service.sh enable
scripts/sdr-gateway-service.sh start
```

The install helper writes `SDR_GATEWAY_API_TOKEN` to `/etc/default/sdr-gateway` by default.

Stop/restart/status:

```bash
scripts/sdr-gateway-service.sh stop
scripts/sdr-gateway-service.sh restart
scripts/sdr-gateway-service.sh status
```

Optional overrides (example):

```bash
SDR_GATEWAY_PORT=8090 SDR_GATEWAY_HOST=127.0.0.1 \
  scripts/sdr-gateway-service.sh install
```

## Authentication

When `SDR_GATEWAY_API_TOKEN` is set, all API and websocket endpoints require auth.

- HTTP: `Authorization: Bearer <token>` (or `X-API-Key: <token>`)
- WebSocket: `ws://.../ws/iq/{id}?token=<token>` (or bearer header)
- Web UI (`/`) includes a token field and stores it in browser localStorage.

Verify auth:

```bash
curl -s http://localhost:8080/auth/verify \
  -H "Authorization: Bearer $SDR_GATEWAY_API_TOKEN"
```

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
- For internet exposure, use HTTPS (reverse proxy), restrict firewall sources, and prefer WireGuard/Tailscale over open public ports.
- Web viewer uses `hackrf_sweep` under the hood, so it is best for spectrum browsing/tuning.
- Sidekiq streaming uses a SoapySDR worker (`driver=sidekiq`) and is exposed as `sidekiq:<index>` in `/devices`.
- Airspy, bladeRF, and RTL-SDR streaming also use SoapySDR workers (`driver=airspy`, `driver=bladerf`, `driver=rtlsdr`) and are exposed as `airspy:<index>` / `bladerf:<index>` / `rtlsdr:<index>`.
- Sweep mode is still HackRF-only.
