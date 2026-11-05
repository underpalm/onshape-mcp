"""Generic Part Studio modelling tools: entity lookup, any Onshape feature, rich sketches."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import httpx
from mcp.types import Tool

from ..api.fsvalues import eval_result
from ..builders.generic import FeatureSpecError, GenericFeatureBuilder, describe, index_specs
from ..builders.sketch import SketchBuilder
from ..units import from_m, get_length_unit
from . import text

_PS_PROPS: Dict[str, Any] = {
    "documentId": {"type": "string", "description": "Document ID"},
    "workspaceId": {"type": "string", "description": "Workspace ID"},
    "elementId": {"type": "string", "description": "Part Studio element ID"},
}
_PS_REQUIRED = ["documentId", "workspaceId", "elementId"]

_GEOMETRY = [
    "PLANE",
    "CYLINDER",
    "CONE",
    "SPHERE",
    "TORUS",
    "LINE",
    "CIRCLE",
    "ARC",
    "OTHER_SURFACE",
]

TOOLS: List[Tool] = [
    Tool(
        name="find_entities",
        description=(
            "Find faces / edges / bodies / vertices in a Part Studio and get their deterministic IDs "
            "plus geometry (plane normal+origin, cylinder radius+axis, size, position in {LEN}). "
            "Use the IDs for sketches on faces, fillets, chamfers, shells, mates, create_feature. "
            "Filters combine: createdBy (feature id), geometry type, facing direction, or a raw "
            "FeatureScript query such as 'qCreatedBy(makeId(\"F1\"), EntityType.EDGE)'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PS_PROPS,
                "entityType": {
                    "type": "string",
                    "enum": ["FACE", "EDGE", "BODY", "VERTEX"],
                    "default": "FACE",
                },
                "createdBy": {
                    "type": "string",
                    "description": "Only entities created by this feature id",
                },
                "geometry": {"type": "string", "enum": _GEOMETRY},
                "facing": {
                    "type": "string",
                    "description": "Planar faces whose outward normal points this way: +X,-X,+Y,-Y,+Z,-Z",
                },
                "partId": {
                    "type": "string",
                    "description": "Only entities of this part (e.g. 'JHD')",
                },
                "query": {
                    "type": "string",
                    "description": "Raw FeatureScript query (overrides filters)",
                },
                "includeConstruction": {
                    "type": "boolean",
                    "description": "Also list sketch / construction geometry and non-solid bodies",
                    "default": False,
                },
                "limit": {"type": "integer", "default": 50},
            },
            "required": _PS_REQUIRED,
        },
    ),
    Tool(
        name="describe_feature_type",
        description=(
            "Show the parameters of any Onshape Part Studio feature type (shell, hole, mirror, cPlane, "
            "helix, sweep, loft, rib, draft, moveFace, splitPart, transform, sheetMetalStart, ...). "
            "Without featureType, lists all available types."
        ),
        inputSchema={
            "type": "object",
            "properties": {**_PS_PROPS, "featureType": {"type": "string"}},
            "required": _PS_REQUIRED,
        },
    ),
    Tool(
        name="create_feature",
        description=(
            "Create ANY Onshape Part Studio feature by type with only the parameters you need "
            "(others keep Onshape defaults). Call describe_feature_type first. Values: numbers in "
            "{LEN} / degrees or expressions like '2 mm', '#var'; enum names; booleans; queries as "
            "entity ids (from find_entities), 'Top'/'Front'/'Right', or {'sketch': '<featureId>'}. "
            "Examples: shell {entities:['JHK'], thickness:2}; mirror {patternType:'PART', "
            "entities:['JHD'], mirrorPlane:'Right'}; cPlane {entities:['Top'], offset:25}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PS_PROPS,
                "featureType": {"type": "string"},
                "name": {"type": "string"},
                "parameters": {"type": "object", "description": "parameterId -> value"},
            },
            "required": [*_PS_REQUIRED, "featureType", "parameters"],
        },
    ),
    Tool(
        name="create_sketch",
        description=(
            "Create one sketch with several shapes on a standard plane (Top/Front/Right), on a "
            "planar face or on a construction plane (entity id from find_entities). Shapes: "
            "rectangle {corner1,corner2}, centerRectangle {center,width,height}, circle {center,"
            "radius|diameter}, line {start,end}, arc {center,radius,startAngle,endAngle}, polygon "
            "{center,sides,radius}, slot {start,end,width}. Add construction:true for construction "
            "geometry. Coordinates in {LEN}; 'local' = sketch-plane 2D coords, 'world' = 3D points "
            "[x,y,z] projected onto the plane (handy for faces). Closed shapes become regions for "
            "create_extrude / create_feature (e.g. extrude with operationType REMOVE for holes)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PS_PROPS,
                "name": {"type": "string", "default": "Sketch"},
                "plane": {
                    "type": "string",
                    "description": "Top, Front, Right, or the deterministic id of a planar face / plane",
                },
                "coordinates": {"type": "string", "enum": ["local", "world"], "default": "local"},
                "shapes": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of shapes, each with a 'type' field (see description)",
                },
            },
            "required": [*_PS_REQUIRED, "plane", "shapes"],
        },
    ),
]

_NAMES = {t.name for t in TOOLS}
_STD_PLANES = {"top", "front", "right"}


# --------------------------------------------------------------------------- helpers
async def _specs(ctx, did: str, wid: str, eid: str) -> Dict[str, Dict[str, Any]]:
    cache = getattr(ctx, "_feature_specs", None)
    if cache is None:
        raw = await ctx.client.get(f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/featurespecs")
        cache = index_specs(raw)
        ctx._feature_specs = cache
    return cache


async def _eval(
    ctx, did: str, wid: str, eid: str, script: str, queries: Optional[Dict[str, List[str]]] = None
):
    body: Dict[str, Any] = {"script": script}
    if queries:
        body["queries"] = queries
    response = await ctx.client.post(
        f"/api/v8/partstudios/d/{did}/w/{wid}/e/{eid}/featurescript", data=body
    )
    return eval_result(response)


_FACING = {
    "+X": (1, 0, 0),
    "-X": (-1, 0, 0),
    "+Y": (0, 1, 0),
    "-Y": (0, -1, 0),
    "+Z": (0, 0, 1),
    "-Z": (0, 0, -1),
}


def _entity_query(args: Dict[str, Any]) -> str:
    if args.get("query"):
        return f"({args['query']})"
    etype = args.get("entityType", "FACE").upper()
    if args.get("createdBy"):
        q = f'qCreatedBy(makeId("{args["createdBy"]}"), EntityType.{etype})'
    else:
        q = f"qEverything(EntityType.{etype})"
    if not args.get("includeConstruction"):
        q = f"qSketchFilter(qConstructionFilter({q}, ConstructionObject.NO), SketchObject.NO)"
    if etype == "BODY" and not args.get("includeConstruction"):
        q = f"qBodyType({q}, BodyType.SOLID)"
    if args.get("partId"):
        q = f'qOwnedByBody({q}, qTransient("{args["partId"]}"))'
    if args.get("geometry"):
        q = f"qGeometry({q}, GeometryType.{args['geometry'].upper()})"
    if args.get("facing"):
        x, y, z = _FACING[args["facing"].upper()]
        q = f"qParallelPlanes({q}, vector({x}, {y}, {z}), false)"
    return q


_FIND_SCRIPT = """function(context is Context, queries) {
  var out = [];
  var n = 0;
  for (var e in evaluateQuery(context, %QUERY%)) {
    n += 1;
    if (n > %LIMIT%) break;
    var info = {"id" : transientQueriesToStrings(e)};
    const nm = try silent(getProperty(context, {"entity" : e, "propertyType" : PropertyType.NAME}));
    if (nm != undefined) { info.name = nm; }
    const bx = try silent(evBox3d(context, {"topology" : e, "tight" : true}));
    if (bx != undefined) { info.min = bx.minCorner; info.max = bx.maxCorner; }
    const srf = try silent(evSurfaceDefinition(context, {"face" : e}));
    if (srf != undefined) {
      info.area = try silent(evArea(context, {"entities" : e}));
      if (srf is Plane) { info.geom = "PLANE"; info.normal = srf.normal; info.origin = srf.origin; }
      else if (srf is Cylinder) { info.geom = "CYLINDER"; info.radius = srf.radius; info.origin = srf.coordSystem.origin; info.axis = srf.coordSystem.zAxis; }
      else if (srf is Cone) { info.geom = "CONE"; info.origin = srf.coordSystem.origin; info.axis = srf.coordSystem.zAxis; }
      else if (srf is Sphere) { info.geom = "SPHERE"; info.radius = srf.radius; info.origin = srf.coordSystem.origin; }
      else { info.geom = "OTHER_SURFACE"; }
    } else {
      const crv = try silent(evCurveDefinition(context, {"edge" : e}));
      if (crv != undefined) {
        info.length = try silent(evLength(context, {"entities" : e}));
        if (crv is Line) { info.geom = "LINE"; info.direction = crv.direction; }
        else if (crv is Circle) { info.geom = "CIRCLE"; info.radius = crv.radius; info.origin = crv.coordSystem.origin; info.axis = crv.coordSystem.zAxis; }
        else { info.geom = "OTHER_CURVE"; }
      }
    }
    out = append(out, info);
  }
  return {"items" : out, "total" : size(evaluateQuery(context, %QUERY%))};
}"""


def _vec(v) -> str:
    return "(" + ", ".join(f"{c:.3g}" for c in v) + ")"


def _pt(v) -> str:
    return "(" + ", ".join(f"{from_m(c):.3f}" for c in v) + ")"


def _format_entities(result: Dict[str, Any]) -> str:
    items = result.get("items") or []
    total = int(result.get("total") or len(items))
    u = get_length_unit()
    lines = [
        f"Found {total} entit{'y' if total == 1 else 'ies'}"
        + (f" (showing {len(items)})" if total > len(items) else "")
        + f"; positions in {u}:"
    ]
    for it in items:
        parts = [
            f"id {it.get('id')}",
            f"'{it['name']}'" if it.get("name") else "",
            it.get("geom", ""),
        ]
        if "normal" in it:
            parts.append(f"normal {_vec(it['normal'])}")
        if "axis" in it:
            parts.append(f"axis {_vec(it['axis'])}")
        if "radius" in it and it["radius"] is not None:
            parts.append(f"r={from_m(it['radius']):.3f} (Ø{2 * from_m(it['radius']):.3f})")
        if "min" in it and "max" in it and it.get("geom") not in ("PLANE",):
            center = [(a + b) / 2 for a, b in zip(it["min"], it["max"])]
            parts.append(f"center {_pt(center)}")
        elif "origin" in it:
            parts.append(f"at {_pt(it['origin'])}")
        if "length" in it and it["length"] is not None:
            parts.append(f"length {from_m(it['length']):.3f}")
        if "area" in it and it["area"] is not None:
            parts.append(f"area {from_m(from_m(it['area'])):.2f} {u}²")
        if "min" in it and "max" in it:
            size = [from_m(b - a) for a, b in zip(it["min"], it["max"])]
            parts.append("size " + " x ".join(f"{s:.2f}" for s in size))
        lines.append("  - " + ", ".join(p for p in parts if p))
    return "\n".join(lines)


async def _plane_cs(ctx, did, wid, eid, plane_id: str) -> Dict[str, Any]:
    script = (
        'function(context is Context, queries) { const p = evPlane(context, {"face" : queries.p});'
        ' return {"origin" : p.origin, "normal" : p.normal, "x" : p.x}; }'
    )
    return await _eval(ctx, did, wid, eid, script, {"p": [plane_id]})


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sketch_origin(cs: Dict[str, Any]) -> Tuple[float, float, float]:
    """Origin of a sketch on a plane: Onshape uses the world origin projected onto the plane."""
    n = cs["normal"]
    d = sum(cs["origin"][i] * n[i] for i in range(3))
    return (d * n[0], d * n[1], d * n[2])


def _local_2d(p) -> Tuple[float, float]:
    return float(p[0]), float(p[1])


class _Projector:
    """Maps world points (active unit) to 2D sketch-plane coordinates (active unit)."""

    def __init__(self, cs: Dict[str, Any]):
        self.o = [from_m(c) for c in sketch_origin(cs)]
        self.x = cs["x"]
        self.y = _cross(cs["normal"], cs["x"])

    def __call__(self, p) -> Tuple[float, float]:
        if len(p) == 2:
            return float(p[0]), float(p[1])
        d = [p[i] - self.o[i] for i in range(3)]
        return (
            sum(d[i] * self.x[i] for i in range(3)),
            sum(d[i] * self.y[i] for i in range(3)),
        )


def _add_shape(sk: SketchBuilder, shape: Dict[str, Any], proj) -> None:
    kind = str(shape.get("type", "")).lower().replace("_", "")
    cons = bool(shape.get("construction", False))
    if kind == "rectangle":
        sk.add_rectangle(proj(shape["corner1"]), proj(shape["corner2"]))
    elif kind == "centerrectangle":
        cx, cy = proj(shape["center"])
        w, h = float(shape["width"]) / 2, float(shape["height"]) / 2
        sk.add_rectangle((cx - w, cy - h), (cx + w, cy + h))
    elif kind == "circle":
        r = float(shape["radius"]) if "radius" in shape else float(shape["diameter"]) / 2
        sk.add_circle(proj(shape["center"]), r, is_construction=cons)
    elif kind == "line":
        sk.add_line(proj(shape["start"]), proj(shape["end"]), is_construction=cons)
    elif kind == "arc":
        sk.add_arc(
            proj(shape["center"]),
            float(shape["radius"]),
            float(shape.get("startAngle", 0)),
            float(shape.get("endAngle", 180)),
            is_construction=cons,
        )
    elif kind == "polygon":
        sk.add_polygon(
            proj(shape["center"]), int(shape["sides"]), float(shape["radius"]), is_construction=cons
        )
    elif kind == "slot":
        (x1, y1), (x2, y2) = proj(shape["start"]), proj(shape["end"])
        r = float(shape["width"]) / 2
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            sk.add_circle((x1, y1), r, is_construction=cons)
            return
        ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
        nx, ny = -(y2 - y1) / length * r, (x2 - x1) / length * r
        sk.add_line((x1 + nx, y1 + ny), (x2 + nx, y2 + ny), is_construction=cons)
        sk.add_line((x1 - nx, y1 - ny), (x2 - nx, y2 - ny), is_construction=cons)
        sk.add_arc((x2, y2), r, ang - 90, ang + 90, is_construction=cons)
        sk.add_arc((x1, y1), r, ang + 90, ang + 270, is_construction=cons)
    else:
        raise ValueError(
            f"Unknown shape type '{shape.get('type')}'. Use rectangle, centerRectangle, circle, line, "
            "arc, polygon or slot."
        )


# --------------------------------------------------------------------------- handlers
async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    did, wid, eid = args.get("documentId"), args.get("workspaceId"), args.get("elementId")
    try:
        if name == "find_entities":
            script = _FIND_SCRIPT.replace("%QUERY%", _entity_query(args)).replace(
                "%LIMIT%", str(int(args.get("limit") or 50))
            )
            return text(_format_entities(await _eval(ctx, did, wid, eid, script)))

        if name == "describe_feature_type":
            specs = await _specs(ctx, did, wid, eid)
            ftype = args.get("featureType")
            if not ftype:
                rows = sorted(f"{k} ({v.get('featureTypeName', '')})" for k, v in specs.items())
                return text("Available feature types:\n" + "\n".join(f"  - {r}" for r in rows))
            if ftype not in specs:
                close = [k for k in specs if ftype.lower() in k.lower()]
                raise ValueError(f"Unknown feature type '{ftype}'. Similar: {close or 'none'}")
            return text(describe(specs[ftype]))

        if name == "create_feature":
            specs = await _specs(ctx, did, wid, eid)
            ftype = args["featureType"]
            if ftype not in specs:
                raise ValueError(f"Unknown feature type '{ftype}'. Use describe_feature_type.")
            builder = GenericFeatureBuilder(specs[ftype])
            params = args.get("parameters") or {}
            for plane in builder.needed_planes(params):
                builder.plane_ids[plane.lower()] = await ctx.partstudio_manager.get_plane_id(
                    did, wid, eid, plane.capitalize()
                )
            body = builder.build(args.get("name") or "", params)
            result = await ctx.partstudio_manager.add_feature(did, wid, eid, body)
            return text(_feature_result(result, ftype))

        if name == "create_sketch":
            plane = str(args["plane"])
            if plane.lower() in _STD_PLANES:
                plane_id = await ctx.partstudio_manager.get_plane_id(
                    did, wid, eid, plane.capitalize()
                )
            else:
                plane_id = plane
            proj = _local_2d
            cs = None
            if args.get("coordinates", "local") == "world" or plane.lower() not in _STD_PLANES:
                cs = await _plane_cs(ctx, did, wid, eid, plane_id)
                if args.get("coordinates", "local") == "world":
                    proj = _Projector(cs)
            sk = SketchBuilder(name=args.get("name") or "Sketch", plane_id=plane_id)
            for shape in args["shapes"]:
                _add_shape(sk, shape, proj)
            result = await ctx.partstudio_manager.add_feature(did, wid, eid, sk.build())
            msg = _feature_result(result, "sketch")
            if cs is not None:
                y = _cross(cs["normal"], cs["x"])
                msg += (
                    f"\nSketch plane: origin {_pt(sketch_origin(cs))} {get_length_unit()}, x-axis "
                    f"{_vec(cs['x'])}, y-axis {_vec(y)}, normal {_vec(cs['normal'])} (points out of "
                    "the face: to cut INTO the part from this sketch use oppositeDirection: true)."
                )
            return text(msg)
    except FeatureSpecError as e:
        return text(f"{name} failed: {e}")
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:400]}")
    except (ValueError, KeyError, TypeError) as e:
        return text(f"{name} failed: {e}")
    return None


def _feature_result(result: Dict[str, Any], kind: str) -> str:
    feature = result.get("feature", {})
    fid = feature.get("featureId", "?")
    state = (result.get("featureState") or {}).get("featureStatus", "UNKNOWN")
    msg = f"Created {kind} '{feature.get('name', '')}' (featureId: {fid}), status: {state}."
    if state == "INFO":
        msg += (
            " Onshape reports an info notice - often the operation did not touch any part "
            "(e.g. extrude/cut pointing away from the part: try oppositeDirection: true)."
        )
    elif state not in ("OK", "UNKNOWN"):
        msg += (
            " The feature has a problem (e.g. invalid references or a value out of range); check it "
            "in Onshape or delete it with delete_feature and retry."
        )
    return msg


__all__ = ["TOOLS", "handle"]
