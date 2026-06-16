from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.apex_hunter import ApexHunterService
from app.auth import auth_enabled, require_http_auth, require_ws_auth
from app.config import get_settings
from app.models import (
    ApexMissionPlan,
    ApexMissionRequest,
    ApexResource,
    ApexResourceUpsert,
    DeviceInfo,
    ErrorResponse,
    IQSweepChunk,
    IQSweepConfig,
    IQSweepState,
    OkResponse,
    StreamConfig,
    StreamProbeConfig,
    StreamProbeState,
    StreamRetuneConfig,
    StreamState,
    SweepConfig,
    SweepSample,
    SweepPlanRequest,
    SweepPlanState,
    SweepState,
    TxBurstConfig,
    TxState,
    WiFiInterfaceInfo,
    WiFiMonitorConfig,
    WiFiMonitorEvent,
    WiFiMonitorState,
)
from app.observability import Metrics, configure_logging
from app.sdr.registry import BackendRegistry
from app.sdr.sidekiq_driver import ensure_sidekiq_driver_loaded
from app.services import DEFAULT_STREAM_CHUNK_BYTES, IQSweepManager, StreamManager, SweepManager, TxManager, WiFiMonitorManager

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
logger = logging.getLogger("sdr_gateway.api")
metrics = Metrics()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        ensure_sidekiq_driver_loaded()
        logger.info(
            "gateway_start",
            extra={
                "request_id": "-",
                "path": "startup",
                "method": "SYSTEM",
                "status_code": 200,
            },
        )
        yield
    finally:
        # Stop active sessions to prevent orphan SDR subprocesses during app/service stop.
        tx_manager.stop_all()
        iq_sweep_manager.stop_all()
        sweep_manager.stop_all()
        stream_manager.stop_all()
        wifi_monitor_manager.stop_all()
        logger.info(
            "gateway_stop",
            extra={
                "request_id": "-",
                "path": "shutdown",
                "method": "SYSTEM",
                "status_code": 200,
            },
        )


app = FastAPI(title="SDR Server", version="0.1.0", lifespan=_lifespan)
WEB_DIR = Path(__file__).resolve().parent / "web"

registry = BackendRegistry()
stream_manager = StreamManager(registry)
sweep_manager = SweepManager(registry)
iq_sweep_manager = IQSweepManager(stream_manager)
tx_manager = TxManager(registry)
apex_hunter = ApexHunterService(registry, stream_manager, sweep_manager)
wifi_monitor_manager = WiFiMonitorManager()

if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": "Bad request",
        "content": {"application/json": {"example": {"detail": "sample_rate_sps 90000000 exceeds device max 20000000"}}},
    },
    401: {
        "model": ErrorResponse,
        "description": "Unauthorized",
        "content": {"application/json": {"example": {"detail": "Unauthorized"}}},
    },
    404: {
        "model": ErrorResponse,
        "description": "Not found",
        "content": {"application/json": {"example": {"detail": "Unknown stream_id <id>"}}},
    },
    409: {
        "model": ErrorResponse,
        "description": "Conflict",
        "content": {"application/json": {"example": {"detail": "Device hackrf:0 is already in use by stream <id>"}}},
    },
}


def _raise_bad_request(exc: Exception) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise_not_found(message: str, exc: Exception | None = None) -> None:
    raise HTTPException(status_code=404, detail=message) from exc


def _device_occupancy() -> dict[str, dict[str, str | bool]]:
    occupied: dict[str, dict[str, str | bool]] = {}

    for session in stream_manager.list_states():
        occupied[session.config.device_id] = {
            "occupied": True,
            "occupied_by": "stream",
            "occupied_id": session.id,
        }

    for session in sweep_manager.list_states():
        occupied[session.config.device_id] = {
            "occupied": True,
            "occupied_by": "sweep",
            "occupied_id": session.id,
        }

    for session in iq_sweep_manager.list_states():
        occupied[session.config.device_id] = {
            "occupied": True,
            "occupied_by": "iq_sweep",
            "occupied_id": session.id,
        }

    for session in tx_manager.list_states():
        if session.status == "running":
            occupied[session.config.device_id] = {
                "occupied": True,
                "occupied_by": "tx",
                "occupied_id": session.id,
            }

    return occupied


def _ensure_device_available(device_id: str) -> None:
    info = _device_occupancy().get(device_id)
    if not info:
        return
    owner = str(info.get("occupied_by", "session"))
    owner_id = str(info.get("occupied_id", ""))
    raise HTTPException(status_code=409, detail=f"Device {device_id} is already in use by {owner} {owner_id}".strip())


def _device_by_id(device_id: str):
    return next((device for device in registry.list_devices() if device.id == device_id), None)


def _plan_sweep_config(plan: SweepPlanRequest) -> tuple[SweepConfig, str]:
    device = _device_by_id(plan.device_id)
    if device is None:
        raise KeyError(f"Unknown device_id '{plan.device_id}'")

    if plan.start_freq_hz is not None or plan.stop_freq_hz is not None:
        if plan.start_freq_hz is None or plan.stop_freq_hz is None:
            raise ValueError("start_freq_hz and stop_freq_hz must be provided together")
        start_freq_hz = int(plan.start_freq_hz)
        stop_freq_hz = int(plan.stop_freq_hz)
    elif plan.center_freq_hz is not None or plan.span_hz is not None:
        if plan.center_freq_hz is None or plan.span_hz is None:
            raise ValueError("center_freq_hz and span_hz must be provided together")
        half_span = int(plan.span_hz) // 2
        start_freq_hz = int(plan.center_freq_hz) - half_span
        stop_freq_hz = int(plan.center_freq_hz) + half_span
    elif plan.frequencies_hz:
        frequencies = [int(freq) for freq in plan.frequencies_hz]
        start_freq_hz = min(frequencies) - int(plan.margin_hz)
        stop_freq_hz = max(frequencies) + int(plan.margin_hz)
    else:
        raise ValueError("provide start/stop, center/span, or frequencies_hz")

    start_freq_hz = max(int(device.freq_min_hz), start_freq_hz)
    stop_freq_hz = min(int(device.freq_max_hz), stop_freq_hz)
    if start_freq_hz >= stop_freq_hz:
        raise ValueError("planned sweep start must be lower than stop after device range clipping")

    engine = "hackrf_sweep" if device.driver == "hackrf" else "backend_sweep"
    if plan.strategy == "native_sweep" and device.driver != "hackrf":
        raise ValueError(f"native_sweep is only available for HackRF devices; {plan.device_id} is {device.driver}")

    return (
        SweepConfig(
            device_id=plan.device_id,
            start_freq_hz=start_freq_hz,
            stop_freq_hz=stop_freq_hz,
            bin_width_hz=plan.bin_width_hz,
            lna_gain_db=plan.lna_gain_db,
            vga_gain_db=plan.vga_gain_db,
            amp_enable=plan.amp_enable,
        ),
        engine,
    )


@app.middleware("http")
async def request_context_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id", "").strip() or str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000.0)
        logger.exception(
            "request_failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": duration_ms,
            },
        )
        metrics.record_request(request.method, request.url.path, 500, duration_ms)
        raise

    duration_ms = int((time.perf_counter() - started) * 1000.0)
    response.headers["x-request-id"] = request_id
    logger.info(
        "request_complete",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    metrics.record_request(request.method, request.url.path, response.status_code, duration_ms)
    return response


@app.get("/")
def web_index():
    if not WEB_DIR.exists():
        raise HTTPException(status_code=404, detail="Web UI not installed")
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "auth_enabled": auth_enabled()}


@app.get("/metrics", responses=ERROR_RESPONSES)
def get_metrics(_: None = Depends(require_http_auth)):
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics endpoint is disabled")
    snapshot = metrics.snapshot()
    snapshot["active_sessions"] = {
        "streams": len(stream_manager.list_states()),
        "sweeps": len(sweep_manager.list_states()),
        "iq_sweeps": len(iq_sweep_manager.list_states()),
        "tx": len(tx_manager.list_states()),
        "wifi_scans": len(wifi_monitor_manager.list_states()),
    }
    return snapshot


@app.get("/auth/verify")
def verify_auth(_: None = Depends(require_http_auth)):
    return {"ok": True, "auth_enabled": auth_enabled()}


@app.get("/devices", response_model=list[DeviceInfo])
def list_devices(_: None = Depends(require_http_auth)):
    devices = registry.list_devices()
    occupancy = _device_occupancy()
    return [DeviceInfo(**d.__dict__, **occupancy.get(d.id, {})) for d in devices]


@app.get("/wifi/interfaces", response_model=list[WiFiInterfaceInfo], responses=ERROR_RESPONSES)
def list_wifi_interfaces(_: None = Depends(require_http_auth)):
    return [WiFiInterfaceInfo(**item) for item in wifi_monitor_manager.list_interfaces()]


@app.post("/wifi/scans/start", response_model=WiFiMonitorState, responses=ERROR_RESPONSES)
def start_wifi_scan(config: WiFiMonitorConfig, _: None = Depends(require_http_auth)):
    try:
        session = wifi_monitor_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except Exception as exc:
        _raise_bad_request(exc)
    return WiFiMonitorState(
        wifi_scan_id=session.id,
        status=session.status,
        config=session.config,
        event_count=session.event_count,
        returncode=session.returncode,
        current_channel=getattr(session, "current_channel", None),
    )


@app.post("/wifi/scans/{wifi_scan_id}/stop", response_model=OkResponse, responses=ERROR_RESPONSES)
def stop_wifi_scan(wifi_scan_id: str, _: None = Depends(require_http_auth)):
    try:
        wifi_monitor_manager.stop(wifi_scan_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown wifi_scan_id {wifi_scan_id}", exc)
    return OkResponse(ok=True)


@app.get("/wifi/scans", response_model=list[WiFiMonitorState], responses=ERROR_RESPONSES)
def list_wifi_scans(_: None = Depends(require_http_auth)):
    return [
        WiFiMonitorState(
            wifi_scan_id=session.id,
            status=session.status,
            config=session.config,
            event_count=session.event_count,
            returncode=session.returncode,
            current_channel=getattr(session, "current_channel", None),
        )
        for session in wifi_monitor_manager.list_states()
    ]


@app.get("/wifi/scans/{wifi_scan_id}/events", response_model=list[WiFiMonitorEvent], responses=ERROR_RESPONSES)
def wifi_scan_events(wifi_scan_id: str, limit: int = 100, _: None = Depends(require_http_auth)):
    try:
        return [WiFiMonitorEvent(**event) for event in wifi_monitor_manager.recent_events(wifi_scan_id, limit=limit)]
    except KeyError as exc:
        _raise_not_found(f"Unknown wifi_scan_id {wifi_scan_id}", exc)


@app.websocket("/ws/wifi/scans/{wifi_scan_id}")
async def wifi_scan_stream(wifi_scan_id: str, websocket: WebSocket):
    if not await require_ws_auth(websocket):
        return
    try:
        wifi_monitor_manager.get(wifi_scan_id)
    except KeyError:
        await websocket.close(code=1008, reason=f"Unknown wifi_scan_id {wifi_scan_id}")
        return
    await websocket.accept()
    sent = 0
    try:
        while True:
            events = wifi_monitor_manager.recent_events(wifi_scan_id, limit=1000)
            if sent > len(events):
                sent = 0
            for event in events[sent:]:
                await websocket.send_json(event)
            sent = len(events)
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass


@app.post(
    "/streams/start",
    response_model=StreamState,
    responses=ERROR_RESPONSES,
)
def start_stream(config: StreamConfig, _: None = Depends(require_http_auth)):
    logger.info(
        "stream_start_requested device_id=%s center_freq_hz=%s sample_rate_sps=%s lna_gain_db=%s vga_gain_db=%s",
        config.device_id,
        config.center_freq_hz,
        config.sample_rate_sps,
        config.lna_gain_db,
        config.vga_gain_db,
        extra={
            "request_id": "-",
            "method": "POST",
            "path": "/streams/start",
            "status_code": 0,
        },
    )
    try:
        _ensure_device_available(config.device_id)
        session = stream_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)

    return StreamState(stream_id=session.id, status=session.status, config=session.config)


@app.post(
    "/streams/{stream_id}/stop",
    response_model=OkResponse,
    responses=ERROR_RESPONSES,
)
def stop_stream(stream_id: str, _: None = Depends(require_http_auth)):
    try:
        stream_manager.stop(stream_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown stream_id {stream_id}", exc)
    return OkResponse(ok=True)


@app.post(
    "/streams/{stream_id}/retune",
    response_model=StreamState,
    responses=ERROR_RESPONSES,
)
def retune_stream(stream_id: str, config: StreamRetuneConfig, _: None = Depends(require_http_auth)):
    try:
        session = stream_manager.retune(stream_id, StreamConfig(**config.model_dump()))
    except KeyError as exc:
        _raise_not_found(f"Unknown stream_id {stream_id}", exc)
    except Exception as exc:
        _raise_bad_request(exc)
    return StreamState(stream_id=session.id, status=session.status, config=session.config)


@app.get("/streams", response_model=list[StreamState])
def list_streams(_: None = Depends(require_http_auth)):
    return [StreamState(stream_id=s.id, status=s.status, config=s.config) for s in stream_manager.list_states()]


@app.post(
    "/streams/probe",
    response_model=StreamProbeState,
    responses=ERROR_RESPONSES,
)
async def probe_stream(config: StreamProbeConfig, _: None = Depends(require_http_auth)):
    try:
        _ensure_device_available(config.device_id)
        result = await stream_manager.probe(
            config,
            capture_count=config.capture_count,
            chunk_size=config.chunk_size,
        )
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)
    return StreamProbeState(**result)


@app.websocket("/ws/iq/{stream_id}")
async def iq_stream(stream_id: str, websocket: WebSocket):
    if not await require_ws_auth(websocket):
        return
    try:
        stream_manager.get(stream_id)
    except KeyError:
        await websocket.close(code=1008, reason=f"Unknown stream_id {stream_id}")
        return

    keep_stream = websocket.query_params.get("keep", "").strip().lower() in {"1", "true", "yes"}
    start_mode = websocket.query_params.get("start", "latest").strip().lower()
    if start_mode not in {"latest", "oldest"}:
        start_mode = "latest"
    cursor_id = stream_manager.create_cursor(stream_id, start=start_mode)
    await websocket.accept()
    empty_chunk_reported_at = 0.0
    try:
        while True:
            chunk = await stream_manager.read_chunk(stream_id, cursor_id=cursor_id)
            if not chunk:
                should_close = True
                try:
                    session = stream_manager.get(stream_id)
                    rc = session.process.poll()
                    should_close = rc is not None or session.status not in {"running", "retuning"}
                    stderr_text = ""
                    stderr = getattr(session.process, "stderr", None)
                    if rc is not None and stderr is not None:
                        raw = await asyncio.to_thread(stderr.read)
                        if isinstance(raw, bytes):
                            stderr_text = raw.decode("utf-8", errors="replace")[-1000:]
                        else:
                            stderr_text = str(raw)[-1000:]
                    now = time.monotonic()
                    if should_close or (now - empty_chunk_reported_at) >= 10.0:
                        logger.warning(
                            "stream_iq_empty_chunk stream_id=%s returncode=%s status=%s closing=%s stderr_tail=%s",
                            stream_id,
                            rc,
                            session.status,
                            int(should_close),
                            stderr_text.strip(),
                            extra={
                                "request_id": "-",
                                "method": "WS",
                                "path": f"/ws/iq/{stream_id}",
                                "status_code": 0,
                            },
                        )
                        empty_chunk_reported_at = now
                except Exception:
                    should_close = True
                if should_close:
                    break
                await asyncio.sleep(0.1)
                continue
            try:
                await websocket.send_bytes(chunk)
            except (WebSocketDisconnect, RuntimeError, AssertionError) as exc:
                logger.warning(
                    "stream_iq_send_failed stream_id=%s error=%s",
                    stream_id,
                    exc,
                    extra={
                        "request_id": "-",
                        "method": "WS",
                        "path": f"/ws/iq/{stream_id}",
                        "status_code": 0,
                    },
                )
                break
    except WebSocketDisconnect:
        logger.info(
            "stream_iq_websocket_disconnect stream_id=%s keep_stream=%s",
            stream_id,
            keep_stream,
            extra={
                "request_id": "-",
                "method": "WS",
                "path": f"/ws/iq/{stream_id}",
                "status_code": 0,
            },
        )
    finally:
        stream_manager.release_cursor(stream_id, cursor_id)
        if not keep_stream:
            # Ensure ordinary dropped/refreshing clients don't leave orphan SDR processes running.
            try:
                stream_manager.stop(stream_id)
            except KeyError:
                pass


@app.post(
    "/sweeps/start",
    response_model=SweepState,
    responses=ERROR_RESPONSES,
)
def start_sweep(config: SweepConfig, _: None = Depends(require_http_auth)):
    try:
        _ensure_device_available(config.device_id)
        session = sweep_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)

    return SweepState(sweep_id=session.id, status=session.status, config=session.config)


@app.post(
    "/sweeps/plan/start",
    response_model=SweepPlanState,
    responses=ERROR_RESPONSES,
)
def start_planned_sweep(plan: SweepPlanRequest, _: None = Depends(require_http_auth)):
    try:
        config, engine = _plan_sweep_config(plan)
        _ensure_device_available(config.device_id)
        session = sweep_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)

    return SweepPlanState(
        sweep_id=session.id,
        status=session.status,
        engine=engine,
        config=session.config,
        label=plan.label,
    )


@app.post(
    "/sweeps/{sweep_id}/stop",
    response_model=OkResponse,
    responses=ERROR_RESPONSES,
)
def stop_sweep(sweep_id: str, _: None = Depends(require_http_auth)):
    try:
        sweep_manager.stop(sweep_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown sweep_id {sweep_id}", exc)
    return OkResponse(ok=True)


@app.get("/sweeps", response_model=list[SweepState])
def list_sweeps(_: None = Depends(require_http_auth)):
    return [SweepState(sweep_id=s.id, status=s.status, config=s.config) for s in sweep_manager.list_states()]


@app.get(
    "/sweeps/{sweep_id}/samples",
    response_model=list[SweepSample],
    responses=ERROR_RESPONSES,
)
def sweep_samples(sweep_id: str, _: None = Depends(require_http_auth)):
    try:
        samples = sweep_manager.recent_samples(sweep_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown sweep_id {sweep_id}", exc)
    return [SweepSample(**s) for s in samples]


@app.post(
    "/iq-sweeps/start",
    response_model=IQSweepState,
    responses=ERROR_RESPONSES,
)
def start_iq_sweep(config: IQSweepConfig, _: None = Depends(require_http_auth)):
    try:
        _ensure_device_available(config.device_id)
        session = iq_sweep_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)
    return IQSweepState(
        iq_sweep_id=session.id,
        stream_id=session.stream_id,
        status=session.status,
        config=session.config,
        current_center_freq_hz=session.current_center_freq_hz,
        point_index=session.point_index,
    )


@app.post(
    "/iq-sweeps/{iq_sweep_id}/stop",
    response_model=OkResponse,
    responses=ERROR_RESPONSES,
)
def stop_iq_sweep(iq_sweep_id: str, _: None = Depends(require_http_auth)):
    try:
        iq_sweep_manager.stop(iq_sweep_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown iq_sweep_id {iq_sweep_id}", exc)
    return OkResponse(ok=True)


@app.get("/iq-sweeps", response_model=list[IQSweepState])
def list_iq_sweeps(_: None = Depends(require_http_auth)):
    return [
        IQSweepState(
            iq_sweep_id=s.id,
            stream_id=s.stream_id,
            status=s.status,
            config=s.config,
            current_center_freq_hz=s.current_center_freq_hz,
            point_index=s.point_index,
        )
        for s in iq_sweep_manager.list_states()
    ]


@app.get(
    "/iq-sweeps/{iq_sweep_id}/chunk",
    response_model=IQSweepChunk,
    responses=ERROR_RESPONSES,
)
async def iq_sweep_chunk(iq_sweep_id: str, nbytes: int = DEFAULT_STREAM_CHUNK_BYTES, _: None = Depends(require_http_auth)):
    try:
        nbytes = max(1024, min(int(nbytes), 4 * 1024 * 1024))
        payload = await iq_sweep_manager.read_chunk(iq_sweep_id, nbytes=nbytes)
    except KeyError as exc:
        _raise_not_found(f"Unknown iq_sweep_id {iq_sweep_id}", exc)
    except Exception as exc:
        _raise_bad_request(exc)
    return IQSweepChunk(**payload)


@app.websocket("/ws/iq-sweeps/{iq_sweep_id}")
async def iq_sweep_stream(iq_sweep_id: str, websocket: WebSocket):
    if not await require_ws_auth(websocket):
        return
    try:
        iq_sweep_manager.get(iq_sweep_id)
    except KeyError:
        await websocket.close(code=1008, reason=f"Unknown iq_sweep_id {iq_sweep_id}")
        return

    nbytes = int(websocket.query_params.get("nbytes", str(DEFAULT_STREAM_CHUNK_BYTES)))
    nbytes = max(1024, min(nbytes, 4 * 1024 * 1024))
    keep_sweep = websocket.query_params.get("keep", "").strip().lower() in {"1", "true", "yes"}
    await websocket.accept()
    try:
        while True:
            payload = await iq_sweep_manager.read_chunk(iq_sweep_id, nbytes=nbytes)
            if not payload.get("byte_count"):
                break
            try:
                await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError, AssertionError) as exc:
                logger.warning(
                    "iq_sweep_send_failed iq_sweep_id=%s error=%s",
                    iq_sweep_id,
                    exc,
                    extra={
                        "request_id": "-",
                        "method": "WS",
                        "path": f"/ws/iq-sweeps/{iq_sweep_id}",
                        "status_code": 0,
                    },
                )
                break
    except WebSocketDisconnect:
        pass
    finally:
        if not keep_sweep:
            try:
                iq_sweep_manager.stop(iq_sweep_id)
            except KeyError:
                pass


@app.post(
    "/tx/start",
    response_model=TxState,
    responses=ERROR_RESPONSES,
)
def start_tx(config: TxBurstConfig, _: None = Depends(require_http_auth)):
    try:
        _ensure_device_available(config.device_id)
        session = tx_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)

    return TxState(tx_id=session.id, status=session.status, config=session.config, returncode=session.returncode)


@app.post(
    "/tx/{tx_id}/stop",
    response_model=OkResponse,
    responses=ERROR_RESPONSES,
)
def stop_tx(tx_id: str, _: None = Depends(require_http_auth)):
    try:
        tx_manager.stop(tx_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown tx_id {tx_id}", exc)
    return OkResponse(ok=True)


@app.get("/tx", response_model=list[TxState])
def list_tx(_: None = Depends(require_http_auth)):
    return [
        TxState(tx_id=s.id, status=s.status, config=s.config, returncode=s.returncode)
        for s in tx_manager.list_states()
    ]


@app.get("/apex/resources", response_model=list[ApexResource], responses=ERROR_RESPONSES)
def apex_list_resources(_: None = Depends(require_http_auth)):
    return apex_hunter.list_resources()


@app.post("/apex/resources", response_model=ApexResource, responses=ERROR_RESPONSES)
def apex_upsert_resource(payload: ApexResourceUpsert, _: None = Depends(require_http_auth)):
    try:
        return apex_hunter.upsert_resource(payload)
    except Exception as exc:
        _raise_bad_request(exc)


@app.post("/apex/resources/{resource_id}/delete", response_model=OkResponse, responses=ERROR_RESPONSES)
def apex_delete_resource(resource_id: str, _: None = Depends(require_http_auth)):
    try:
        apex_hunter.delete_resource(resource_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown resource_id {resource_id}", exc)
    return OkResponse(ok=True)


@app.post("/apex/plan", response_model=ApexMissionPlan, responses=ERROR_RESPONSES)
def apex_plan(payload: ApexMissionRequest, _: None = Depends(require_http_auth)):
    try:
        return apex_hunter.plan(payload)
    except Exception as exc:
        _raise_bad_request(exc)


@app.post("/apex/run", response_model=ApexMissionPlan, responses=ERROR_RESPONSES)
def apex_run(payload: ApexMissionRequest, _: None = Depends(require_http_auth)):
    try:
        return apex_hunter.run(payload)
    except Exception as exc:
        _raise_bad_request(exc)


@app.get("/apex/missions", response_model=list[ApexMissionPlan], responses=ERROR_RESPONSES)
def apex_missions(_: None = Depends(require_http_auth)):
    return apex_hunter.list_missions()


@app.get("/apex/templates", responses=ERROR_RESPONSES)
def apex_templates(_: None = Depends(require_http_auth)):
    return apex_hunter.mission_templates()
