# Architecture

## Overview

`sdr-gateway` is a FastAPI control plane that orchestrates SDR subprocesses and Soapy workers.

- API/control: FastAPI (`app/main.py`)
- Session managers: `StreamManager`, `SweepManager`, `TxManager` (`app/services.py`)
- Device backends: HackRF + Soapy-backed radios (`app/sdr/*`)
- Data plane:
  - RX stream bytes via websocket (`/ws/iq/{stream_id}`)
  - TX bursts via `/tx/start`
  - Sweep samples via `/sweeps/*`

## Main components

- `app/config.py`
  - Validates env config (`log level`, `metrics enabled`, token sanity)
- `app/auth.py`
  - HTTP + websocket token auth
- `app/observability.py`
  - Structured logging formatter
  - In-process metrics counters
- `app/main.py`
  - API routes, middleware, lifecycle cleanup

## Process model

- Each stream/sweep/tx session creates an isolated subprocess/worker.
- Managers track session IDs and process handles.
- Lifespan shutdown calls `stop_all()` on all managers to avoid orphaned processes.

## Error handling

- Endpoints expose standardized error payload: `{"detail": "..."}`
- Route handlers map manager/backend errors to 400/404 responses.

## Compatibility

- Designed for SDR-Shark integration:
  - `/devices`, `/streams/*`, `/ws/iq/*`, `/sweeps/*`, `/tx/*`
