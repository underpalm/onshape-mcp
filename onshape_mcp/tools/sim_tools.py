"""MuJoCo simulation export: Onshape assembly -> URDF -> MJCF with physics, plus a quick test run."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from mcp.types import Tool

from ..exports import export_dir, safe_name
from ..sim import exporter
from . import text

_CONFIG_DOC = (
    "Physics config (all optional; angles in degrees, lengths in metres, SI units): "
    "{'base': '<link>', 'floating': false, 'timestep': 0.001, 'gravity': [0,0,-9.81], "
    "'floor': true, 'collisions': true, 'friction': [1, 0.005, 0.0001], "
    "'joint_defaults': {'damping': 0.01}, "
    "'joints': {'<mate name>': {'damping': N·m·s/rad, 'frictionloss': N·m, 'stiffness': N·m/rad, "
    "'springref': deg, 'armature': kg·m², 'range': [min, max], 'actuator': {'type': "
    "'position'|'velocity'|'motor', 'kp': .., 'kv': .., 'ctrlrange': [..], 'forcerange': [..]}}}, "
    "'springs': [{'name', 'body1', 'point1': [x,y,z] m, 'body2', 'point2', 'stiffness': N/m, "
    "'damping': N·s/m, 'rest_length': m}]}"
)

TOOLS: List[Tool] = [
    Tool(
        name="export_mujoco",
        description=(
            "Export an Onshape ASSEMBLY for MuJoCo: Onshape's URDF export (masses + inertia from part "
            "materials, joints from mates: revolute/slider/fastened) is converted into an MJCF model "
            "(<name>.xml) next to <name>.urdf (ROS/RViz) and meshes/. The tree can be re-rooted at any "
            "part ('base'), helper links become sites, jointed parts don't collide, and damping, "
            "friction, springs, armature, limits, actuators and sensors are added from 'config'. The "
            "model is compiled with MuJoCo and summarised. Parts need materials (set_part_material). "
            + _CONFIG_DOC
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "documentId": {"type": "string"},
                "workspaceId": {"type": "string"},
                "elementId": {"type": "string", "description": "Assembly element ID"},
                "name": {"type": "string", "description": "Robot/model name (file names)"},
                "config": {"type": "object", "description": "Physics config, see description"},
                "resolution": {
                    "type": "string",
                    "enum": ["coarse", "medium", "fine"],
                    "default": "medium",
                },
                "directory": {
                    "type": "string",
                    "description": "Output folder (default ~/onshape-exports/<name>_mujoco)",
                },
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
    Tool(
        name="update_mujoco_model",
        description=(
            "Re-generate the MJCF of an exported model with a new physics config (damping, springs, "
            "limits, actuators, base, ...) without downloading from Onshape again. " + _CONFIG_DOC
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Folder of a previous export_mujoco",
                },
                "config": {"type": "object"},
                "merge": {
                    "type": "boolean",
                    "description": "Merge into the existing sim_config.json (default true)",
                    "default": True,
                },
            },
            "required": ["directory", "config"],
        },
    ),
    Tool(
        name="simulate_mujoco",
        description=(
            "Run a MuJoCo model headless for a few seconds to check it: set initial joint angles "
            "(degrees) / positions (metres) and actuator controls, and get per-joint min/max/end values "
            "and the oscillation period (e.g. to verify a pendulum or spring-mass system). Open it "
            "interactively with: python -m mujoco.viewer --mjcf=<file>."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "xmlPath": {"type": "string"},
                "duration": {"type": "number", "default": 3.0},
                "initial": {"type": "object", "description": "joint name -> start value"},
                "controls": {
                    "type": "object",
                    "description": "actuator name -> control value (SI, radians)",
                },
            },
            "required": ["xmlPath"],
        },
    ),
]

_NAMES = {t.name for t in TOOLS}


def _validation(xml_path: str) -> str:
    if not exporter.mujoco_available():
        return "MuJoCo is not installed - install with: uv sync --extra sim (model not compiled)."
    try:
        return exporter.summarize(xml_path)
    except Exception as e:  # MuJoCo compile errors
        return f"⚠ MuJoCo could not compile the model: {e}"


async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    try:
        if name == "export_mujoco":
            did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
            data = await exporter.download_urdf(
                ctx.client, ctx.transfer_manager, did, wid, eid, args.get("resolution", "medium")
            )
            config = dict(args.get("config") or {})
            if args.get("name"):
                config["name"] = args["name"]
            if not config.get("name"):
                try:
                    elements = await ctx.client.get(
                        f"/api/documents/d/{did}/w/{wid}/elements", params={"elementId": eid}
                    )
                    config["name"] = next((e["name"] for e in elements if e["id"] == eid), "robot")
                except httpx.HTTPStatusError:
                    config["name"] = "robot"
            folder = args.get("directory") or os.path.join(
                export_dir(None), safe_name(config["name"]) + "_mujoco"
            )
            hints = await exporter.split_part_hints(ctx.client, did, wid, eid)
            result = exporter.write_package(data, folder, config, split_hints=hints)
            report = exporter.model_report(
                result["model"], result["tree"], result["config"].get("attach")
            )
            return text(
                f"MuJoCo package written to {folder}:\n  MJCF: {result['xml']}\n  URDF: {result['urdf']}\n"
                f"  meshes/, sim_config.json\n\n{report}\n\n{_validation(result['xml'])}\n\n"
                f"View: python -m mujoco.viewer --mjcf={result['xml']}"
            )

        if name == "update_mujoco_model":
            folder = os.path.expanduser(args["directory"])
            config = dict(args.get("config") or {})
            cfg_path = os.path.join(folder, "sim_config.json")
            old: Dict[str, Any] = {}
            if os.path.exists(cfg_path):
                import json

                with open(cfg_path) as fh:
                    old = json.load(fh)
            # derived from the Onshape assembly, not a user setting -> always keep
            for key in ("attach", "name"):
                if key in old and key not in config:
                    config[key] = old[key]
            if args.get("merge", True) and old:
                joints = {**(old.get("joints") or {}), **(config.get("joints") or {})}
                old.update(config)
                old["joints"] = joints
                if "floor_height" not in config:
                    old.pop("floor_height", None)  # recomputed from the meshes
                config = old
            result = exporter.regenerate(folder, config)
            report = exporter.model_report(
                result["model"], result["tree"], result["config"].get("attach")
            )
            return text(f"Updated {result['xml']}\n\n{report}\n\n{_validation(result['xml'])}")

        if name == "simulate_mujoco":
            if not exporter.mujoco_available():
                return text("MuJoCo is not installed - install with: uv sync --extra sim")
            r = exporter.simulate(
                os.path.expanduser(args["xmlPath"]),
                float(args.get("duration", 3.0)),
                args.get("initial"),
                args.get("controls"),
            )
            lines = [
                f"Simulated {r['time']:.3f} s"
                + (
                    "  ⚠ UNSTABLE (NaN) - check masses, timestep, collisions"
                    if r["unstable"]
                    else ""
                )
            ]
            for jname, s in r["joints"].items():
                u = s["unit"]
                line = f"  - {jname}: start {s['start']:.3f} {u}, end {s['end']:.3f} {u}, range [{s['min']:.3f}, {s['max']:.3f}] {u}"
                if s.get("period"):
                    line += f", period {s['period']:.4f} s"
                lines.append(line)
            return text("\n".join(lines))
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except (ValueError, RuntimeError, TimeoutError, FileNotFoundError, KeyError) as e:
        return text(f"{name} failed: {e}")
    return None
