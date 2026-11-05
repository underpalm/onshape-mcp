"""Technical drawings: create (ISO / first-angle / mm / A4-A0), dimension, annotate, export."""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import httpx
from mcp.types import ImageContent, Tool

from ..api.drawings import (
    EXPORT_FORMATS,
    SIZES,
    DrawingManager,
    diameter,
    line_to_line,
    note,
    plan_auto_dimensions,
)
from ..exports import save_bytes
from . import text

_DOC = {
    "documentId": {"type": "string"},
    "workspaceId": {"type": "string"},
}
_DRAWING = {**_DOC, "drawingId": {"type": "string", "description": "Element ID of the drawing"}}
_DRAWING_REQ = ["documentId", "workspaceId", "drawingId"]

TOOLS: List[Tool] = [
    Tool(
        name="create_drawing",
        description=(
            "Create a technical drawing of a part, Part Studio or assembly following German/ISO "
            "conventions: ISO standard, first-angle (European) projection, millimetres, comma "
            "decimal separator, border + title block, German language. Onshape places front, "
            "top, side and isometric views automatically. With autoDimension (default) the overall "
            "size and hole diameters are dimensioned. Returns the drawing id and its views."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_DOC,
                "elementId": {
                    "type": "string",
                    "description": "Part Studio or Assembly element ID",
                },
                "partId": {"type": "string", "description": "Only this part (from a Part Studio)"},
                "isAssembly": {"type": "boolean", "default": False},
                "name": {"type": "string", "description": "Drawing name"},
                "size": {"type": "string", "enum": SIZES, "default": "A4"},
                "autoDimension": {"type": "boolean", "default": True},
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
    Tool(
        name="get_drawing_views",
        description=(
            "List the views of a drawing (name, scale, orientation) and, optionally, the edges/circles "
            "of one view with their uniqueIds, for add_drawing_dimension."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_DRAWING,
                "viewId": {"type": "string", "description": "Show this view's geometry"},
            },
            "required": _DRAWING_REQ,
        },
    ),
    Tool(
        name="auto_dimension_drawing",
        description="Add overall dimensions and hole diameters to an existing drawing.",
        inputSchema={"type": "object", "properties": dict(_DRAWING), "required": _DRAWING_REQ},
    ),
    Tool(
        name="add_drawing_dimension",
        description=(
            "Add a dimension to a drawing view: 'distance' between two parallel edges "
            "(edge1, edge2 uniqueIds) or 'diameter' of a circle (edge uniqueId). Get uniqueIds with "
            "get_drawing_views(viewId=...)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_DRAWING,
                "viewId": {"type": "string"},
                "kind": {"type": "string", "enum": ["distance", "diameter"]},
                "edge1": {"type": "string"},
                "edge2": {"type": "string"},
            },
            "required": [*_DRAWING_REQ, "viewId", "kind", "edge1"],
        },
    ),
    Tool(
        name="add_drawing_note",
        description=(
            "Add a text note to a drawing. Position in mm on the sheet, origin bottom-left "
            "(A4 landscape = 297 x 210). Typical: material, tolerances ('Allgemeintoleranz ISO "
            "2768-m'), surface or print settings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_DRAWING,
                "text": {"type": "string"},
                "x": {"type": "number", "default": 20},
                "y": {"type": "number", "default": 20},
                "textHeight": {"type": "number", "default": 3.5},
            },
            "required": [*_DRAWING_REQ, "text"],
        },
    ),
    Tool(
        name="export_drawing",
        description=(
            "Export a drawing as PDF (default), DXF, DWG, SVG or PNG and save it locally "
            "(default folder ~/onshape-exports or ONSHAPE_EXPORT_DIR). With preview=true a PNG "
            "image of the drawing is returned so it can be checked visually."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_DRAWING,
                "format": {"type": "string", "enum": EXPORT_FORMATS, "default": "PDF"},
                "fileName": {"type": "string", "description": "File name without extension"},
                "directory": {"type": "string", "description": "Target folder"},
                "preview": {"type": "boolean", "default": False},
            },
            "required": _DRAWING_REQ,
        },
    ),
]

_NAMES = {t.name for t in TOOLS}


def _manager(ctx) -> DrawingManager:
    m = getattr(ctx, "_drawings", None)
    if m is None:
        m = DrawingManager(ctx.client)
        ctx._drawings = m
    return m


def _results_summary(result: Dict[str, Any]) -> str:
    results = result.get("results") or []
    ok = sum(1 for r in results if r.get("status") == "OK")
    errors = [r.get("errorDescription", "") for r in results if r.get("status") != "OK"]
    msg = f"{ok}/{len(results)} annotation(s) added"
    if errors:
        msg += "; errors: " + "; ".join(errors[:3])
    return msg


async def _auto_dimension(dm: DrawingManager, did: str, wid: str, eid: str) -> str:
    views = await dm.views(did, wid, eid)
    geoms = {
        v.view_id: await dm.geometry(did, wid, eid, v.view_id) for v in views if v.is_orthographic
    }
    plan = plan_auto_dimensions(views, geoms)
    if not plan:
        return "no dimensions found to add"
    return _results_summary(await dm.add_annotations(did, wid, eid, plan, "Auto dimensions"))


def _views_text(views) -> str:
    lines = []
    for v in views:
        kind = "orthographic" if v.is_orthographic else "isometric/other"
        lines.append(
            f"  - {v.name or '?'} (viewId {v.view_id}), {kind}"
            + (f", scale {v.scale}" if v.scale else "")
        )
    return "\n".join(lines)


async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    dm = _manager(ctx)
    did, wid = args.get("documentId"), args.get("workspaceId")
    eid = args.get("drawingId")
    try:
        if name == "create_drawing":
            drawing = await dm.create(
                did,
                wid,
                args["elementId"],
                args.get("name") or "Zeichnung",
                part_id=args.get("partId"),
                is_assembly=bool(args.get("isAssembly")),
                size=args.get("size", "A4"),
            )
            eid = drawing["id"]
            lines = [
                f"Created drawing '{drawing.get('name')}' (drawingId: {eid}) - ISO, first-angle "
                f"projection, mm, {args.get('size', 'A4')}.",
                f"Open: https://cad.onshape.com/documents/{did}/w/{wid}/e/{eid}",
            ]
            if args.get("autoDimension", True):
                lines.append("Dimensions: " + await _auto_dimension(dm, did, wid, eid))
            lines.append("Views:\n" + _views_text(await dm.views(did, wid, eid)))
            return text("\n".join(lines))

        if name == "get_drawing_views":
            views = await dm.views(did, wid, eid)
            out = ["Views:", _views_text(views)]
            if args.get("viewId"):
                geom = await dm.geometry(did, wid, eid, args["viewId"])
                out.append(f"\nGeometry of view {args['viewId']} (coordinates in mm, model scale):")
                for g in geom[:120]:
                    d = g.get("data", {})
                    if g["type"] == "line":
                        s, e = d["start"], d["end"]
                        out.append(
                            f"  line {g['uniqueId']}: ({s[0] * 1000:.2f}, {s[1] * 1000:.2f}) -> "
                            f"({e[0] * 1000:.2f}, {e[1] * 1000:.2f})"
                        )
                    elif g["type"] in ("circle", "circularArc"):
                        c = d["center"]
                        out.append(
                            f"  {g['type']} {g['uniqueId']}: center ({c[0] * 1000:.2f}, {c[1] * 1000:.2f}), "
                            f"Ø{2 * d['radius'] * 1000:.2f}"
                        )
                    else:
                        out.append(f"  {g['type']} {g['uniqueId']}")
            return text("\n".join(out))

        if name == "auto_dimension_drawing":
            return text(await _auto_dimension(dm, did, wid, eid))

        if name == "add_drawing_dimension":
            view = args["viewId"]
            geom = {g["uniqueId"]: g for g in await dm.geometry(did, wid, eid, view)}
            e1 = geom.get(args["edge1"])
            if e1 is None:
                raise ValueError(f"Edge {args['edge1']} not found in view {view}")
            if args["kind"] == "diameter":
                c, r = e1["data"]["center"], e1["data"]["radius"]
                ann = diameter(view, args["edge1"], c, r, (c[0] + 4 * r, c[1] - 4 * r))
            else:
                e2 = geom.get(args.get("edge2") or "")
                if e2 is None:
                    raise ValueError("distance needs edge2 (a second, parallel edge)")
                pts = [
                    e1["data"]["start"],
                    e1["data"]["end"],
                    e2["data"]["start"],
                    e2["data"]["end"],
                ]
                cx = sum(p[0] for p in pts) / 4
                cy = max(p[1] for p in pts) + 0.005
                ann = line_to_line(view, args["edge1"], args["edge2"], (cx, cy))
            return text(
                _results_summary(await dm.add_annotations(did, wid, eid, [ann], "Dimension"))
            )

        if name == "add_drawing_note":
            ann = note(
                args["text"],
                float(args.get("x", 20)),
                float(args.get("y", 20)),
                float(args.get("textHeight", 3.5)),
            )
            return text(_results_summary(await dm.add_annotations(did, wid, eid, [ann], "Note")))

        if name == "export_drawing":
            fmt = args.get("format", "PDF").upper()
            data, ext = await dm.export(did, wid, eid, fmt)
            base = args.get("fileName") or f"drawing_{eid[:8]}"
            path = save_bytes(data, f"{base}.{ext}", args.get("directory"))
            content: list = text(f"Saved {fmt} ({len(data) / 1024:.0f} kB) to {path}")
            if args.get("preview"):
                png = data if fmt == "PNG" else (await dm.export(did, wid, eid, "PNG"))[0]
                content.append(
                    ImageContent(
                        type="image", data=base64.b64encode(png).decode(), mimeType="image/png"
                    )
                )
            return content
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except (ValueError, KeyError, RuntimeError, TimeoutError) as e:
        return text(f"{name} failed: {e}")
    return None
