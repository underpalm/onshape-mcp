"""Assembly kinematics: rigid groups and joints on an axis that do not move parts.

Onshape mates pull their two mate connectors onto each other. For imported geometry
(STEP) that is already in place this would make parts jump. ``create_axis_joint``
therefore measures both connectors, compensates the offset along the axis and the
axis direction, and verifies afterwards that nothing moved.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx
from mcp.types import Tool

from ..analysis.face_cs import extract_mc_coordinate_system
from ..builders.mate import MateBuilder, MateConnectorBuilder, MateType
from ..units import from_m, get_length_unit
from . import text

_ASM = {
    "documentId": {"type": "string"},
    "workspaceId": {"type": "string"},
    "elementId": {"type": "string", "description": "Assembly element ID"},
}
_OCC = {
    "type": "string",
    "description": "Instance path: instance IDs or names joined with '/', e.g. "
    "'SCS215 v5 (3) <1>/Horn <1>' for a part inside a sub-assembly",
}

TOOLS: List[Tool] = [
    Tool(
        name="create_rigid_group",
        description=(
            "Make several assembly instances (parts or sub-assemblies) one rigid body WITHOUT moving "
            "them (Onshape 'Group'). Use it to join parts that are screwed/glued together, e.g. a "
            "servo with the bracket it is mounted in. Exported to URDF/MJCF as one link."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_ASM,
                "name": {"type": "string"},
                "instances": {
                    "type": "array",
                    "items": _OCC,
                    "description": "Instance paths/names",
                },
            },
            "required": ["documentId", "workspaceId", "elementId", "instances"],
        },
    ),
    Tool(
        name="create_axis_joint",
        description=(
            "Create a revolute / slider / cylindrical joint between two instances on a common axis "
            "WITHOUT moving parts that are already in place (e.g. imported STEP models). Each side "
            "references a face: a cylindrical face (shaft, hole, servo horn) defines the axis; a "
            "planar face its normal. Offsets along the axis and opposite axis directions are "
            "compensated automatically, and the tool verifies that no part moved. The first "
            "instance moves relative to the second; the joint name becomes the URDF/MJCF joint name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_ASM,
                "name": {"type": "string"},
                "jointType": {
                    "type": "string",
                    "enum": ["REVOLUTE", "SLIDER", "CYLINDRICAL"],
                    "default": "REVOLUTE",
                },
                "firstInstance": _OCC,
                "firstFaceId": {"type": "string", "description": "Face of the moving instance"},
                "secondInstance": _OCC,
                "secondFaceId": {"type": "string", "description": "Face of the other instance"},
                "maxAxisDeviation": {
                    "type": "number",
                    "description": "Max. allowed distance between the two axes in {LEN} (default 0.5)",
                },
            },
            "required": [
                "documentId",
                "workspaceId",
                "elementId",
                "name",
                "firstInstance",
                "firstFaceId",
                "secondInstance",
                "secondFaceId",
            ],
        },
    ),
]

_NAMES = {t.name for t in TOOLS}


# --------------------------------------------------------------------------- helpers
async def _definition(ctx, did, wid, eid) -> Dict[str, Any]:
    return await ctx.client.get(
        f"/api/assemblies/d/{did}/w/{wid}/e/{eid}",
        params={"includeMateFeatures": "true", "includeMateConnectors": "true"},
    )


def resolve_path(asm: Dict[str, Any], spec: str) -> List[str]:
    """'A <1>/B <1>' (names) or 'Mabc/Mdef' (ids) -> list of instance ids."""
    root = asm.get("rootAssembly", {})
    levels: List[List[Dict[str, Any]]] = [root.get("instances", [])]
    subs = {s.get("elementId"): s for s in asm.get("subAssemblies", [])}
    path: List[str] = []
    instances = root.get("instances", [])
    for i, token in enumerate([t.strip() for t in spec.split("/") if t.strip()]):
        match = next((x for x in instances if x["id"] == token), None)
        if match is None:
            match = next((x for x in instances if x.get("name") == token), None)
        if match is None:
            low = token.lower()
            cands = [x for x in instances if x.get("name", "").lower().startswith(low)]
            if len(cands) == 1:
                match = cands[0]
        if match is None:
            names = ", ".join(x.get("name", x["id"]) for x in instances)
            raise ValueError(f"Instance '{token}' not found at level {i + 1}. Available: {names}")
        path.append(match["id"])
        sub = subs.get(match.get("elementId")) if match.get("type") == "Assembly" else None
        instances = sub.get("instances", []) if sub else []
        levels.append(instances)
    return path


def _transforms(asm: Dict[str, Any]) -> Dict[Tuple[str, ...], List[float]]:
    return {
        tuple(o["path"]): o["transform"] for o in asm.get("rootAssembly", {}).get("occurrences", [])
    }


def _max_motion(before, after) -> float:
    worst = 0.0
    for key, t0 in before.items():
        t1 = after.get(key)
        if t1 is None:
            continue
        worst = max(worst, max(abs(a - b) for a, b in zip(t0, t1)))
    return worst


class _WorldFrame:
    def __init__(self, origin, z_axis):
        self.origin_meters = origin
        self.z_axis = z_axis


def _apply(t: Sequence[float], v: Sequence[float], is_point: bool) -> Tuple[float, float, float]:
    """Apply a row-major 4x4 transform to a point or direction."""
    out = []
    for r in range(3):
        val = t[4 * r] * v[0] + t[4 * r + 1] * v[1] + t[4 * r + 2] * v[2]
        if is_point:
            val += t[4 * r + 3]
        out.append(val)
    return tuple(out)  # type: ignore[return-value]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


async def _add_connector(
    ctx, did, wid, eid, name, path, face, z_offset_m: float = 0.0
) -> Tuple[str, Any]:
    """Create a mate connector (planar: centroid, cylinder: mid axis point) and read its frame."""
    last_status = None
    for inference in ("CENTROID", "MID_AXIS_POINT"):
        b = MateConnectorBuilder(name=name, face_id=face, occurrence_path=path)
        b.inference_type = inference
        if z_offset_m:
            b.set_translation(0.0, 0.0, from_m(z_offset_m))
        r = await ctx.assembly_manager.add_feature(did, wid, eid, b.build())
        fid = r.get("feature", {}).get("featureId")
        last_status = (r.get("featureState") or {}).get("featureStatus")
        if last_status != "ERROR":
            asm = await _definition(ctx, did, wid, eid)
            cs = extract_mc_coordinate_system(asm, fid)
            if cs is None:
                raise RuntimeError(f"Could not read the frame of mate connector {name}")
            # the connector frame is given in the part's own coordinates -> world
            t = _transforms(asm).get(tuple(path))
            if t is None:
                raise RuntimeError(f"No occurrence transform for {'/'.join(path)}")
            return fid, _WorldFrame(_apply(t, cs.origin_meters, True), _apply(t, cs.z_axis, False))
        await ctx.assembly_manager.delete_feature(did, wid, eid, fid)
    raise ValueError(
        f"Face {face} on {'/'.join(path)} cannot hold a mate connector ({last_status})"
    )


def _group_feature(name: str, paths: List[List[str]]) -> Dict[str, Any]:
    return {
        "feature": {
            "btType": "BTMMateGroup-65",
            "featureType": "mateGroup",
            "name": name,
            "suppressed": False,
            "parameters": [
                {
                    "btType": "BTMParameterQueryWithOccurrenceList-67",
                    "parameterId": "occurrencesQuery",
                    "queries": [
                        {"btType": "BTMIndividualOccurrenceQuery-626", "path": p} for p in paths
                    ],
                }
            ],
        }
    }


# --------------------------------------------------------------------------- handlers
async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    try:
        if name == "create_rigid_group":
            asm = await _definition(ctx, did, wid, eid)
            paths = [resolve_path(asm, spec) for spec in args["instances"]]
            if len(paths) < 2:
                raise ValueError("A group needs at least two instances")
            before = _transforms(asm)
            r = await ctx.assembly_manager.add_feature(
                did, wid, eid, _group_feature(args.get("name") or "Group", paths)
            )
            status = (r.get("featureState") or {}).get("featureStatus", "?")
            moved = _max_motion(before, _transforms(await _definition(ctx, did, wid, eid)))
            msg = f"Rigid group '{args.get('name') or 'Group'}' with {len(paths)} instances: status {status}."
            if moved > 1e-6:
                msg += f" ⚠ parts moved by up to {from_m(moved):.3f} {get_length_unit()}"
            return text(msg)

        if name == "create_axis_joint":
            return await _axis_joint(args, ctx, did, wid, eid)
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except (ValueError, RuntimeError) as e:
        return text(f"{name} failed: {e}")
    return None


async def _axis_joint(args, ctx, did, wid, eid) -> list:
    asm = await _definition(ctx, did, wid, eid)
    before = _transforms(asm)
    p1 = resolve_path(asm, args["firstInstance"])
    p2 = resolve_path(asm, args["secondInstance"])
    jname = args["name"]
    created: List[str] = []
    try:
        mc1, cs1 = await _add_connector(
            ctx, did, wid, eid, f"{jname} - MC1", p1, args["firstFaceId"]
        )
        created.append(mc1)
        mc2, cs2 = await _add_connector(
            ctx, did, wid, eid, f"{jname} - MC2", p2, args["secondFaceId"]
        )
        created.append(mc2)

        z1, z2 = cs1.z_axis, cs2.z_axis
        if abs(abs(_dot(z1, z2)) - 1.0) > 1e-3:
            angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(_dot(z1, z2))))))
            raise ValueError(
                f"The two faces' axes are not parallel ({angle:.2f}° apart) - pick coaxial faces"
            )
        d = [a - b for a, b in zip(cs1.origin_meters, cs2.origin_meters)]  # from MC2 to MC1
        axial = _dot(d, z2)
        radial = math.sqrt(max(0.0, _dot(d, d) - axial * axial))
        limit = float(args.get("maxAxisDeviation", from_m(0.0005)))
        if from_m(radial) > limit:
            raise ValueError(
                f"The axes are {from_m(radial):.3f} {get_length_unit()} apart - the mate would move a part. "
                "Pick faces on the same axis (e.g. horn and the hole it sits in)."
            )
        if abs(axial) > 1e-9:  # shift MC2 along its own z onto MC1
            await ctx.assembly_manager.delete_feature(did, wid, eid, mc2)
            created.remove(mc2)
            mc2, cs2 = await _add_connector(
                ctx, did, wid, eid, f"{jname} - MC2", p2, args["secondFaceId"], axial
            )
            created.append(mc2)

        aligned = _dot(z1, z2) > 0
        mate = MateBuilder(
            name=jname, mate_type=MateType[args.get("jointType", "REVOLUTE").upper()]
        )
        mate.set_first_connector(mc1)
        mate.set_second_connector(mc2)
        body = mate.build()
        if not aligned:
            body["feature"]["parameters"].append(
                {
                    "btType": "BTMParameterBoolean-144",
                    "parameterId": "primaryAxisAlignment",
                    "value": True,
                }
            )
        r = await ctx.assembly_manager.add_feature(did, wid, eid, body)
        status = (r.get("featureState") or {}).get("featureStatus", "?")
        created.append(r.get("feature", {}).get("featureId"))
    except Exception:
        for fid in reversed(created):
            try:
                await ctx.assembly_manager.delete_feature(did, wid, eid, fid)
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        raise

    moved = _max_motion(before, _transforms(await _definition(ctx, did, wid, eid)))
    u = get_length_unit()
    msg = (
        f"{args.get('jointType', 'REVOLUTE').title()} joint '{jname}': status {status}. Axis offset "
        f"compensated {from_m(axial):.3f} {u}, axis direction {'same' if aligned else 'flipped'}, "
        f"axis deviation {from_m(radial):.4f} {u}."
    )
    if moved > 1e-5:
        msg += f"\n⚠ Parts moved by up to {moved:.4g} (transform units) - check the assembly."
    else:
        msg += "\nNo part moved."
    return text(msg)
