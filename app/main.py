from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.models import DeviceInfo, StreamConfig, StreamState, SweepConfig, SweepSample, SweepState
from app.sdr.registry import BackendRegistry
from app.services import StreamManager, SweepManager

app = FastAPI(title="SDR Server", version="0.1.0")
WEB_DIR = Path(__file__).resolve().parent / "web"

registry = BackendRegistry()
stream_manager = StreamManager(registry)
sweep_manager = SweepManager(registry)

if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.get("/")
def web_index():
    if not WEB_DIR.exists():
        raise HTTPException(status_code=404, detail="Web UI not installed")
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/devices", response_model=list[DeviceInfo])
def list_devices():
    devices = registry.list_devices()
    return [DeviceInfo(**d.__dict__) for d in devices]


@app.post("/streams/start", response_model=StreamState)
def start_stream(config: StreamConfig):
    try:
        session = stream_manager.start(config)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StreamState(stream_id=session.id, status=session.status, config=session.config)


@app.post("/streams/{stream_id}/stop")
def stop_stream(stream_id: str):
    try:
        stream_manager.stop(stream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown stream_id {stream_id}") from exc
    return {"ok": True}


@app.get("/streams", response_model=list[StreamState])
def list_streams():
    return [StreamState(stream_id=s.id, status=s.status, config=s.config) for s in stream_manager.list_states()]


@app.websocket("/ws/iq/{stream_id}")
async def iq_stream(stream_id: str, websocket: WebSocket):
    try:
        stream_manager.get(stream_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown stream_id {stream_id}") from exc

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


@app.post("/sweeps/start", response_model=SweepState)
def start_sweep(config: SweepConfig):
    try:
        session = sweep_manager.start(config)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SweepState(sweep_id=session.id, status=session.status, config=session.config)


@app.post("/sweeps/{sweep_id}/stop")
def stop_sweep(sweep_id: str):
    try:
        sweep_manager.stop(sweep_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown sweep_id {sweep_id}") from exc
    return {"ok": True}


@app.get("/sweeps", response_model=list[SweepState])
def list_sweeps():
    return [SweepState(sweep_id=s.id, status=s.status, config=s.config) for s in sweep_manager.list_states()]


@app.get("/sweeps/{sweep_id}/samples", response_model=list[SweepSample])
def sweep_samples(sweep_id: str):
    try:
        samples = sweep_manager.recent_samples(sweep_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown sweep_id {sweep_id}") from exc
    return [SweepSample(**s) for s in samples]
