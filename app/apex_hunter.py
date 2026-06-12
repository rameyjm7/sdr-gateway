from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

from app.models import (
    ApexAction,
    ApexMissionPlan,
    ApexMissionRequest,
    ApexResource,
    ApexResourceUpsert,
    SweepConfig,
)


@dataclass
class MissionRecord:
    mission_id: str
    objective: str
    actions: list[ApexAction]
    status: str
    notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ApexHunterService:
    """Fleet-level orchestrator for SDR + API resources."""

    def __init__(self, registry: Any, stream_manager: Any, sweep_manager: Any) -> None:
        self._registry = registry
        self._stream_manager = stream_manager
        self._sweep_manager = sweep_manager
        self._external_resources: dict[str, ApexResource] = {}
        self._missions: dict[str, MissionRecord] = {}

    def _discovered_sdr_resources(self) -> list[ApexResource]:
        resources: list[ApexResource] = []
        for dev in self._registry.list_devices():
            resources.append(
                ApexResource(
                    id=dev.id,
                    kind="sdr",
                    label=dev.label,
                    capabilities=[
                        "spectrum_scan",
                        "signal_hunt",
                        "stream_iq",
                        "tx_burst",
                    ],
                    metadata={
                        "driver": dev.driver,
                        "freq_min_hz": dev.freq_min_hz,
                        "freq_max_hz": dev.freq_max_hz,
                        "max_sample_rate_sps": dev.max_sample_rate_sps,
                    },
                )
            )
        return resources

    def list_resources(self) -> list[ApexResource]:
        merged = {r.id: r for r in self._discovered_sdr_resources()}
        merged.update(self._external_resources)
        return list(merged.values())

    def upsert_resource(self, request: ApexResourceUpsert) -> ApexResource:
        resource = ApexResource(
            id=request.id,
            kind=request.kind,
            label=request.label,
            capabilities=request.capabilities,
            metadata=request.metadata,
        )
        self._external_resources[resource.id] = resource
        return resource

    def delete_resource(self, resource_id: str) -> None:
        if resource_id not in self._external_resources:
            raise KeyError(resource_id)
        del self._external_resources[resource_id]

    def _choose_resources(self, selected_ids: list[str]) -> list[ApexResource]:
        all_resources = self.list_resources()
        if not selected_ids:
            return all_resources
        by_id = {r.id: r for r in all_resources}
        chosen: list[ApexResource] = []
        for resource_id in selected_ids:
            item = by_id.get(resource_id)
            if item is not None:
                chosen.append(item)
        return chosen

    @staticmethod
    def mission_templates() -> list[dict[str, Any]]:
        return [
            {
                "id": "sigint_wide_hunt",
                "name": "SIGINT Wide Hunt",
                "objective": "Find persistent and high-power unknown emitters across available SDR resources.",
                "constraints": [
                    "priority=unknown_emitters",
                    "start_freq_hz=700000000",
                    "stop_freq_hz=900000000",
                    "bin_width_hz=100000",
                    "report_top_n=10",
                ],
            },
            {
                "id": "commint_channel_map",
                "name": "COMMINT Channel Map",
                "objective": "Map occupied voice/data channels and identify persistent activity windows.",
                "constraints": [
                    "priority=channel_occupancy",
                    "start_freq_hz=136000000",
                    "stop_freq_hz=960000000",
                    "bin_width_hz=50000",
                    "report_top_n=20",
                ],
            },
            {
                "id": "ew_spectrum_pressure",
                "name": "EW Spectrum Pressure",
                "objective": "Detect concentrated spectral pressure and rapidly retask SDR fleet on hotspots.",
                "constraints": [
                    "priority=spectral_pressure",
                    "start_freq_hz=400000000",
                    "stop_freq_hz=3000000000",
                    "bin_width_hz=200000",
                    "report_top_n=12",
                ],
            },
        ]

    @staticmethod
    def _constraints_to_dict(constraints: list[str]) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for item in constraints:
            text = str(item).strip()
            if not text or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key:
                parsed[key] = value
        return parsed

    @staticmethod
    def _bounded_int(value: Any, default: int, min_value: int, max_value: int) -> int:
        try:
            num = int(float(value))
        except Exception:
            num = default
        return max(min_value, min(max_value, num))

    def _recommended_sweep(self, resource: ApexResource, constraint_map: dict[str, str]) -> dict[str, int]:
        freq_min = self._bounded_int(resource.metadata.get("freq_min_hz"), 70_000_000, 1_000_000, 20_000_000_000)
        freq_max = self._bounded_int(resource.metadata.get("freq_max_hz"), 3_000_000_000, freq_min + 1_000_000, 20_000_000_000)
        start_hz = self._bounded_int(constraint_map.get("start_freq_hz"), 700_000_000, freq_min, freq_max - 1_000_000)
        stop_hz = self._bounded_int(constraint_map.get("stop_freq_hz"), 900_000_000, start_hz + 1_000_000, freq_max)
        max_sps = self._bounded_int(resource.metadata.get("max_sample_rate_sps"), 2_000_000, 200_000, 120_000_000)
        default_bin = max(10_000, min(500_000, max_sps // 20))
        bin_width_hz = self._bounded_int(constraint_map.get("bin_width_hz"), default_bin, 5_000, 2_000_000)
        return {
            "start_freq_hz": start_hz,
            "stop_freq_hz": stop_hz,
            "bin_width_hz": bin_width_hz,
        }

    def _heuristic_plan(self, req: ApexMissionRequest, resources: list[ApexResource]) -> list[ApexAction]:
        objective = req.objective.lower()
        constraint_map = self._constraints_to_dict(req.constraints)
        actions: list[ApexAction] = []
        sdr_targets = [r for r in resources if r.kind == "sdr"]
        api_targets = [r for r in resources if r.kind == "api"]

        for resource in sdr_targets:
            if len(actions) >= req.max_actions:
                break
            target = resource.id
            sweep = self._recommended_sweep(resource, constraint_map)
            if "commint" in objective or "voice" in objective or "channel" in objective:
                actions.append(
                    ApexAction(
                        tool="start_sweep",
                        target=target,
                        params={**sweep, "priority": "commint", "confidence": 0.81},
                        rationale="Wideband comms-oriented sweep for channel occupancy baseline.",
                    )
                )
            elif "sigint" in objective or "hunt" in objective or "unknown" in objective:
                actions.append(
                    ApexAction(
                        tool="start_sweep",
                        target=target,
                        params={**sweep, "priority": "sigint", "confidence": 0.84},
                        rationale="Broad SIGINT scan to surface persistent and high-power emitters.",
                    )
                )
            elif "ew" in objective or "jamming" in objective or "interference" in objective:
                actions.append(
                    ApexAction(
                        tool="start_sweep",
                        target=target,
                        params={**sweep, "priority": "ew", "confidence": 0.79},
                        rationale="EW-oriented pressure scan to locate concentrated spectral activity.",
                    )
                )
            else:
                actions.append(
                    ApexAction(
                        tool="start_sweep",
                        target=target,
                        params={**sweep, "priority": "general", "confidence": 0.72},
                        rationale="Default tactical survey band sweep.",
                    )
                )

        for resource in api_targets:
            if len(actions) >= req.max_actions:
                break
            actions.append(
                ApexAction(
                    tool="call_external_api",
                    target=resource.id,
                    params={"operation": "status", "priority": "support", "confidence": 0.66},
                    rationale="Synchronize non-SDR resource status for coordinated operations.",
                )
            )

        return actions[: req.max_actions]

    def _langchain_plan(self, req: ApexMissionRequest, resources: list[ApexResource]) -> list[ApexAction] | None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_core.tools import tool
            from langchain_openai import ChatOpenAI
        except Exception:
            return None

        @tool
        def tool_list_resources(_: str = "") -> str:
            """Return available resources and capabilities."""
            return "\n".join(f"{r.id} [{r.kind}] {','.join(r.capabilities)}" for r in resources)

        @tool
        def tool_start_sweep(target_id: str) -> str:
            """Plan sweep usage for a target SDR resource."""
            return f"Plan sweep on {target_id}"

        @tool
        def tool_call_external_api(target_id: str) -> str:
            """Plan external API resource operation."""
            return f"Plan external API operation on {target_id}"

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are Apex Hunter. Produce concise actionable RF mission plans for multi-resource control.",
                ),
                (
                    "human",
                    "Objective: {objective}\nConstraints: {constraints}\nResources: {resource_ids}\n"
                    "Respond as compact lines formatted: TOOL|TARGET|RATIONALE",
                ),
            ]
        )
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.1, api_key=api_key)
        chain = prompt | llm
        resource_ids = ", ".join(r.id for r in resources)
        constraints = "; ".join(req.constraints) if req.constraints else "none"
        reply = chain.invoke(
            {"objective": req.objective, "constraints": constraints, "resource_ids": resource_ids}
        )
        text = getattr(reply, "content", "") or ""
        parsed: list[ApexAction] = []
        for line in str(text).splitlines():
            parts = [p.strip() for p in line.split("|", 2)]
            if len(parts) != 3:
                continue
            tool_name, target, rationale = parts
            if tool_name not in {"start_sweep", "call_external_api"}:
                continue
            parsed.append(ApexAction(tool=tool_name, target=target, rationale=rationale))
        return parsed[: req.max_actions] if parsed else None

    def plan(self, req: ApexMissionRequest) -> ApexMissionPlan:
        mission_id = str(uuid.uuid4())
        resources = self._choose_resources(req.resources)
        actions = self._langchain_plan(req, resources) or self._heuristic_plan(req, resources)
        notes = [
            "Apex Hunter produced a fleet plan over discovered SDRs and registered APIs.",
            "Use execute=true to run immediate sweep actions.",
        ]
        plan = ApexMissionPlan(
            mission_id=mission_id,
            objective=req.objective,
            resources=resources,
            actions=actions,
            status="planned",
            notes=notes,
        )
        self._missions[mission_id] = MissionRecord(
            mission_id=mission_id,
            objective=req.objective,
            actions=actions,
            status="planned",
            notes=notes,
        )
        return plan

    def run(self, req: ApexMissionRequest) -> ApexMissionPlan:
        plan = self.plan(req)
        notes = list(plan.notes)
        status = "planned"
        action_failures = 0
        if req.execute:
            status = "executing"
            for action in plan.actions:
                try:
                    if action.tool == "start_sweep" and action.target.startswith(("hackrf:", "airspy:", "bladerf:", "rtlsdr:", "sidekiq:")):
                        start_hz = int(action.params.get("start_freq_hz", 700_000_000))
                        stop_hz = int(action.params.get("stop_freq_hz", 900_000_000))
                        bin_width_hz = int(action.params.get("bin_width_hz", 100_000))
                        payload = SweepConfig(
                            device_id=action.target,
                            start_freq_hz=start_hz,
                            stop_freq_hz=stop_hz,
                            bin_width_hz=bin_width_hz,
                            lna_gain_db=16,
                            vga_gain_db=20,
                            amp_enable=False,
                        )
                        session = self._sweep_manager.start(payload)
                        notes.append(f"Started sweep {session.id} on {action.target}")
                    elif action.tool == "call_external_api":
                        notes.append(f"Queued external API task for {action.target}")
                    else:
                        notes.append(f"Skipped unsupported action: {action.tool} on {action.target}")
                except Exception as exc:
                    action_failures += 1
                    notes.append(f"Action failed ({action.tool} on {action.target}): {exc}")
            status = "degraded" if action_failures else "running"
            if action_failures:
                notes.append(f"{action_failures} action(s) failed during execution.")
        self._missions[plan.mission_id] = MissionRecord(
            mission_id=plan.mission_id,
            objective=plan.objective,
            actions=plan.actions,
            status=status,
            notes=notes,
        )
        return ApexMissionPlan(
            mission_id=plan.mission_id,
            objective=plan.objective,
            resources=plan.resources,
            actions=plan.actions,
            status=status,
            notes=notes,
        )

    def list_missions(self) -> list[ApexMissionPlan]:
        resources = self.list_resources()
        records = sorted(self._missions.values(), key=lambda m: m.created_at, reverse=True)
        return [
            ApexMissionPlan(
                mission_id=m.mission_id,
                objective=m.objective,
                resources=resources,
                actions=m.actions,
                status=m.status,
                notes=m.notes,
            )
            for m in records
        ]
