from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import auth_enabled, require_http_auth, require_ws_auth
from app.config import get_settings
from app.models import (
    DeviceInfo,
    ErrorResponse,
    OkResponse,
    StreamConfig,
    StreamState,
    SweepConfig,
    SweepSample,
    SweepState,
    TxBurstConfig,
    TxState,
)
from app.observability import Metrics, configure_logging
from app.sdr.registry import BackendRegistry
from app.services import StreamManager, SweepManager, TxManager

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
logger = logging.getLogger("sdr_gateway.api")
metrics = Metrics()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
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
        sweep_manager.stop_all()
        stream_manager.stop_all()
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
tx_manager = TxManager(registry)

if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

ERROR_RESPONSES = {
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
}


def _raise_bad_request(exc: Exception) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise_not_found(message: str, exc: Exception | None = None) -> None:
    raise HTTPException(status_code=404, detail=message) from exc


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
        "tx": len(tx_manager.list_states()),
    }
    return snapshot


@app.get("/auth/verify")
def verify_auth(_: None = Depends(require_http_auth)):
    return {"ok": True, "auth_enabled": auth_enabled()}


@app.get("/devices", response_model=list[DeviceInfo])
def list_devices(_: None = Depends(require_http_auth)):
    devices = registry.list_devices()
    return [DeviceInfo(**d.__dict__) for d in devices]


@app.post(
    "/streams/start",
    response_model=StreamState,
    responses=ERROR_RESPONSES,
)
def start_stream(config: StreamConfig, _: None = Depends(require_http_auth)):
    try:
        session = stream_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
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


@app.get("/streams", response_model=list[StreamState])
def list_streams(_: None = Depends(require_http_auth)):
    return [StreamState(stream_id=s.id, status=s.status, config=s.config) for s in stream_manager.list_states()]


@app.websocket("/ws/iq/{stream_id}")
async def iq_stream(stream_id: str, websocket: WebSocket):
    if not await require_ws_auth(websocket):
        return
    try:
        stream_manager.get(stream_id)
    except KeyError as exc:
        _raise_not_found(f"Unknown stream_id {stream_id}", exc)

    await websocket.accept()
    try:
        while True:
            chunk = await stream_manager.read_chunk(stream_id)
            if not chunk:
                break
            await websocket.send_bytes(chunk)
    except WebSocketDisconnect:
        pass
    finally:
        # Ensure dropped/refreshing clients don't leave orphan SDR processes running.
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
        session = sweep_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
    except Exception as exc:
        _raise_bad_request(exc)

    return SweepState(sweep_id=session.id, status=session.status, config=session.config)


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
    "/tx/start",
    response_model=TxState,
    responses=ERROR_RESPONSES,
)
def start_tx(config: TxBurstConfig, _: None = Depends(require_http_auth)):
    try:
        session = tx_manager.start(config)
    except KeyError as exc:
        _raise_not_found(str(exc), exc)
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
