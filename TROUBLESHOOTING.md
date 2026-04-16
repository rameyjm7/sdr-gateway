# Troubleshooting

## Auth failures (401 Unauthorized)

- Confirm token is set for service shell:
  - `echo "$SDR_GATEWAY_API_TOKEN"`
- Verify endpoint directly:
  - `curl -s http://127.0.0.1:8080/auth/verify -H "Authorization: Bearer $SDR_GATEWAY_API_TOKEN"`

## No devices in `/devices`

- Check service status:
  - `scripts/sdr-gateway-service.sh status`
- Verify SDR tools manually:
  - HackRF: `hackrf_info`
  - Soapy: `SoapySDRUtil --find`
- If one backend crashes, others still list; inspect logs:
  - `scripts/sdr-gateway-service.sh logs`

## Stream starts but no IQ data

- Confirm selected `device_id` exists from `/devices`.
- Verify radio gain/sample-rate/frequency ranges are valid.
- Check websocket auth (`?token=` or bearer header).

## TX starts but no signal observed

- Ensure TX-capable device (`hackrf`, `bladerf`, `sidekiq`).
- Validate IQ payload shape: base64 of interleaved int8 I/Q bytes.
- Try lower sample rate and longer repeat count for first sanity check.

## Soapy import issues

- In active venv:
  - `python -c "import SoapySDR; print('ok')"`
- If missing, install bindings and plugin modules for your distro.

## CI failures

- Run local gate:
  - `make ci`
- If `mypy` fails, start by annotating edited code paths and avoid `Any` sprawl.
