# sdr-gateway

Network SDR control service for local radios (HackRF + SoapySDR streaming for Sidekiq/Airspy/bladeRF/RTL-SDR), with:

- Device discovery (`/devices`)
- Start/stop IQ streaming (`/streams/*` + `/ws/iq/{stream_id}`)
- Start/stop TX burst sessions (`/tx/*`)
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

## Configuration

Primary environment variables:

- `SDR_GATEWAY_API_TOKEN`: optional auth token; if set, HTTP/WS auth is enforced.
- `SDR_GATEWAY_LOG_LEVEL`: `DEBUG|INFO|WARNING|ERROR|CRITICAL` (default: `INFO`)
- `SDR_GATEWAY_LOG_JSON`: `1/0` to enable JSON logs (default: `0`)
- `SDR_GATEWAY_METRICS_ENABLED`: `1/0` to enable `/metrics` endpoint (default: `1`)

See `.env.example` for a baseline local config.

## Run

```bash
cd /home/jake/workspace/SDR/sdr-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

For reproducible installs (recommended):

```bash
pip install -r requirements.txt
pip install -e .
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

Request observability:

- Every HTTP response includes `x-request-id`.
- `/metrics` exposes in-process counters and active session counts (auth required).

## Core API

- `GET /devices`
- `POST /streams/start`
- `POST /streams/{stream_id}/stop`
- `GET /streams`
- `WS /ws/iq/{stream_id}`
- `POST /tx/start`
- `POST /tx/{tx_id}/stop`
- `GET /tx`
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

## TX burst API

`/tx/start` takes base64-encoded interleaved int8 IQ (`I,Q,I,Q,...`) and runs TX as a separate session, so TX and RX can run concurrently.

Example:

```bash
IQ_B64="$(python3 - <<'PY'
import base64
print(base64.b64encode(bytes([0,0, 40,0, 0,0, -40 & 0xFF,0])*4096).decode())
PY
)"

curl -s http://localhost:8080/tx/start \
  -H "Authorization: Bearer $SDR_GATEWAY_API_TOKEN" \
  -H 'content-type: application/json' \
  -d "{
    \"device_id\":\"hackrf:0\",
    \"center_freq_hz\":751000000,
    \"sample_rate_sps\":2000000,
    \"tx_gain_db\":30,
    \"amp_enable\":false,
    \"baseband_filter_hz\":2000000,
    \"iq_i8_b64\":\"$IQ_B64\",
    \"repeat\":32,
    \"timeout_seconds\":10
  }"
```

Current TX support:
- HackRF (`hackrf_transfer -t` backend)
- bladeRF (SoapySDR TX worker)
- Sidekiq (SoapySDR TX worker)
- Airspy / RTL-SDR: RX-only (TX returns a clear error)

## Testing, linting, and JUnit reports

Install dev tooling:

```bash
pip install -r requirements-dev.txt
pip install -e .
```

Local quality gates:

```bash
make lint
make type
make test
make test-junit
```

`make test-junit` writes:
- `reports/junit-unit.xml`
- `reports/coverage.xml`

It also enforces a minimum coverage threshold in CI.

Hardware test (optional):

```bash
RUN_SDR_HW_TESTS=1 SDR_GATEWAY_API_TOKEN="$SDR_GATEWAY_API_TOKEN" \
  pytest -q -s -rs -m hardware tests/test_tx_rx_hardware.py
```

## SDR-Shark compatibility

This gateway is compatible with [SDR-Shark](https://github.com/rameyjm7/SDR-Shark) and is designed as its SDR control/streaming backend.

- `SDR-Shark` consumes `/devices`, `/streams/*`, `/ws/iq/*`, and optional `/sweeps/*`.
- The new `/tx/*` API is available for TX workflows and can be integrated in `SDR-Shark` actions/features.
- For authenticated deployments, use the same `SDR_GATEWAY_API_TOKEN` in both services.

## Additional docs

- `CONTRIBUTING.md`
- `ARCHITECTURE.md`
- `TROUBLESHOOTING.md`
- `RELEASE_CHECKLIST.md`

## Notes

- IQ format over websocket is raw interleaved signed 8-bit bytes: `I,Q,I,Q,...`
- This scaffold has an optional mock backend (`mock:0`) for testing without hardware (`SDR_ENABLE_MOCK=1`).
- For internet exposure, use HTTPS (reverse proxy), restrict firewall sources, and prefer WireGuard/Tailscale over open public ports.
- Web viewer uses `hackrf_sweep` under the hood, so it is best for spectrum browsing/tuning.
- Sidekiq streaming uses a SoapySDR worker (`driver=sidekiq`) and is exposed as `sidekiq:<index>` in `/devices`.
- Airspy, bladeRF, and RTL-SDR streaming also use SoapySDR workers (`driver=airspy`, `driver=bladerf`, `driver=rtlsdr`) and are exposed as `airspy:<index>` / `bladerf:<index>` / `rtlsdr:<index>`.
- Sweep mode is still HackRF-only.
