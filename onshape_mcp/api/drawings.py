"""Onshape drawings: create (ISO, first-angle, mm), annotate, auto-dimension, export."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .transfer import TransferManager

FORMAT_VERSION = "2021-01-01"
EXPORT_FORMATS = ["PDF", "DXF", "DWG", "SVG", "PNG", "JPEG", "TIFF", "DRAWING_JSON"]
SIZES = ["A4", "A3", "A2", "A1", "A0"]


@dataclass
class ViewInfo:
    view_id: str
    name: str = ""
    matrix: List[float] = dc_field(default_factory=list)  # 3x4 row-major (model -> view)
    paper: Optional[List[float]] = None  # viewToPaperMatrix items (3x4)
    scale: str = ""

    @property
    def is_orthographic(self) -> bool:
        if len(self.matrix) < 12:
            return False
        rows = [self.matrix[0:3], self.matrix[4:7]]
        return all(sum(1 for c in r if abs(c) > 1e-6) == 1 for r in rows)

    def screen_axes(self) -> Tuple[int, int]:
        """Model axis index (0=X,1=Y,2=Z) shown horizontally / vertically in this view."""
        rx, ry = self.matrix[0:3], self.matrix[4:7]
        return max(range(3), key=lambda i: abs(rx[i])), max(range(3), key=lambda i: abs(ry[i]))


class DrawingManager:
    def __init__(self, client):
        self.client = client
        self.transfer = TransferManager(client)

    # ------------------------------------------------------------------ create
    async def create(
        self,
        document_id: str,
        workspace_id: str,
        element_id: str,
        name: str,
        part_id: Optional[str] = None,
        is_assembly: bool = False,
        size: str = "A4",
        language: str = "de",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "drawingName": name,
            "elementId": element_id,
            "standard": "ISO",
            "projection": "first",  # European / first-angle projection (DIN ISO 5456-2)
            "size": size.upper(),
            "units": "MILLIMETER",
            "border": True,
            "titleblock": True,
            "language": language,
            "decimalSeparator": "COMMA",
        }
        if part_id:
            body.update(partId=part_id, referenceType=3)
        elif is_assembly:
            body["referenceType"] = 2
        return await self.client.post(
            f"/api/drawings/d/{document_id}/w/{workspace_id}/create", data=body
        )

    # ------------------------------------------------------------------ inspect
    async def views(self, did: str, wid: str, eid: str) -> List[ViewInfo]:
        raw = await self.client.get(f"/api/drawings/d/{did}/w/{wid}/e/{eid}/views")
        items = raw.get("items", raw) if isinstance(raw, dict) else raw
        views = {
            v["viewId"]: ViewInfo(v["viewId"], matrix=v.get("viewMatrix") or []) for v in items
        }
        try:  # names / paper placement only come with the drawing JSON
            sheet = (await self.drawing_json(did, wid, eid))["sheets"][0]
            for v in sheet.get("views", []):
                info = views.get(v.get("viewId"))
                if info:
                    info.name = v.get("name", "")
                    info.paper = (v.get("viewToPaperMatrix") or {}).get("items")
                    sc = v.get("scale") or {}
                    if sc.get("numerator"):
                        info.scale = f"{sc['numerator']:g}:{sc['denumerator']:g}"
        except Exception as e:  # pragma: no cover - names are cosmetic
            logger.warning(f"Could not read drawing JSON: {e}")
        return list(views.values())

    async def geometry(self, did: str, wid: str, eid: str, view_id: str) -> List[Dict[str, Any]]:
        g = await self.client.get(
            f"/api/drawings/d/{did}/w/{wid}/e/{eid}/views/{view_id}/jsongeometry"
        )
        return [b for b in g.get("bodyData", []) if b.get("visible", True)]

    async def drawing_json(self, did: str, wid: str, eid: str) -> Dict[str, Any]:
        data, _ = await self.export(did, wid, eid, "DRAWING_JSON")
        return json.loads(data)

    # ------------------------------------------------------------------ modify
    async def modify(
        self, did: str, wid: str, eid: str, requests: List[Dict[str, Any]], description: str
    ) -> Dict[str, Any]:
        r = await self.client.post(
            f"/api/drawings/d/{did}/w/{wid}/e/{eid}/modify",
            data={"description": description, "jsonRequests": requests},
        )
        delay = 1.0
        for _ in range(40):
            status = await self.client.get(f"/api/drawings/modify/status/{r['id']}")
            if status.get("requestState") in ("DONE", "FAILED"):
                try:
                    return json.loads(status.get("output") or "{}")
                except json.JSONDecodeError:
                    return {"status": status.get("requestState"), "raw": status.get("output")}
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)
        raise TimeoutError("Drawing modification did not finish")

    async def add_annotations(
        self, did: str, wid: str, eid: str, annotations: List[Dict[str, Any]], description: str
    ) -> Dict[str, Any]:
        return await self.modify(
            did,
            wid,
            eid,
            [
                {
                    "messageName": "onshapeCreateAnnotations",
                    "formatVersion": FORMAT_VERSION,
                    "annotations": annotations,
                }
            ],
            description,
        )

    # ------------------------------------------------------------------ export
    async def export(self, did: str, wid: str, eid: str, fmt: str = "PDF") -> Tuple[bytes, str]:
        fmt = fmt.upper()
        r = await self.client.post(
            f"/api/drawings/d/{did}/w/{wid}/e/{eid}/translations",
            data={"formatName": fmt, "storeInDocument": False},
        )
        status = await self.transfer.wait_for_translation(r["id"])
        ids = status.get("resultExternalDataIds") or []
        if not ids:
            raise RuntimeError("Drawing export returned no file")
        data = await self.client.get_bytes(f"/api/documents/d/{did}/externaldata/{ids[0]}")
        ext = {"DRAWING_JSON": "json", "JPEG": "jpg"}.get(fmt, fmt.lower())
        if data[:2] == b"PK" and ext not in ("zip",):
            ext = "zip"
        return data, ext


# ---------------------------------------------------------------------- annotation builders
def edge_ref(view_id: str, unique_id: str) -> Dict[str, Any]:
    return {"type": "Onshape::Reference::Edge", "uniqueId": unique_id, "viewId": view_id}


def point_ref(view_id: str, xy, unique_id: Optional[str] = None) -> Dict[str, Any]:
    ref: Dict[str, Any] = {
        "type": "Onshape::Reference::Point",
        "coordinate": [float(xy[0]), float(xy[1]), 0.0],
        "viewId": view_id,
    }
    if unique_id:
        ref["uniqueId"] = unique_id
    return ref


def line_to_line(view_id: str, uid1: str, uid2: str, text_xy) -> Dict[str, Any]:
    return {
        "type": "Onshape::Dimension::LineToLine",
        "lineToLineDimension": {
            "edge1": edge_ref(view_id, uid1),
            "edge2": edge_ref(view_id, uid2),
            "textPosition": point_ref(view_id, text_xy),
        },
    }


def diameter(view_id: str, uid: str, center, radius: float, text_xy) -> Dict[str, Any]:
    """Diameter dimension; the leader runs through the circle towards ``text_xy``."""
    cx, cy = center[0], center[1]
    dx, dy = text_xy[0] - cx, text_xy[1] - cy
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    return {
        "type": "Onshape::Dimension::Diametric",
        "diametricDimension": {
            "chordPoint": point_ref(view_id, (cx + radius * ux, cy + radius * uy), uid),
            "farChordPoint": point_ref(view_id, (cx - radius * ux, cy - radius * uy), uid),
            "textPosition": point_ref(view_id, text_xy),
        },
    }


def note(text: str, x_mm: float, y_mm: float, height_mm: float = 3.5) -> Dict[str, Any]:
    return {
        "type": "Onshape::Note",
        "note": {
            "position": {"type": "Onshape::Reference::Point", "coordinate": [x_mm, y_mm, 0.0]},
            "contents": text,
            "textHeight": height_mm,
        },
    }


def _lines(geom: List[Dict[str, Any]]):
    for g in geom:
        if g.get("type") == "line":
            s, e = g["data"]["start"], g["data"]["end"]
            yield g["uniqueId"], (s[0], s[1]), (e[0], e[1])


def plan_auto_dimensions(
    views: List[ViewInfo], geometries: Dict[str, List[Dict[str, Any]]], max_holes: int = 8
) -> List[Dict[str, Any]]:
    """Overall size of the part (each model axis once) + hole diameters.

    Uses the outermost parallel edges of the orthographic views. Coordinates in the
    view geometry are model metres in the view plane.
    """
    annotations: List[Dict[str, Any]] = []
    done_axes: set = set()
    eps = 1e-7
    for view in views:
        if not view.is_orthographic:
            continue
        geom = geometries.get(view.view_id, [])
        lines = list(_lines(geom))
        if not lines:
            continue
        xs = [c for _, s, e in lines for c in (s[0], e[0])]
        ys = [c for _, s, e in lines for c in (s[1], e[1])]
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        size = max(xmax - xmin, ymax - ymin, 1e-3)
        off = max(0.18 * size, 0.004)
        ax_h, ax_v = view.screen_axes()

        def longest(cands):
            return max(cands, key=lambda c: abs(c[2] - c[1]))[0] if cands else None

        if ax_h not in done_axes and xmax - xmin > eps:
            left = longest(
                [
                    (u, s[1], e[1])
                    for u, s, e in lines
                    if abs(s[0] - e[0]) < eps and abs(s[0] - xmin) < eps
                ]
            )
            right = longest(
                [
                    (u, s[1], e[1])
                    for u, s, e in lines
                    if abs(s[0] - e[0]) < eps and abs(s[0] - xmax) < eps
                ]
            )
            if left and right and left != right:
                annotations.append(
                    line_to_line(view.view_id, left, right, ((xmin + xmax) / 2, ymax + off))
                )
                done_axes.add(ax_h)
        if ax_v not in done_axes and ymax - ymin > eps:
            bottom = longest(
                [
                    (u, s[0], e[0])
                    for u, s, e in lines
                    if abs(s[1] - e[1]) < eps and abs(s[1] - ymin) < eps
                ]
            )
            top = longest(
                [
                    (u, s[0], e[0])
                    for u, s, e in lines
                    if abs(s[1] - e[1]) < eps and abs(s[1] - ymax) < eps
                ]
            )
            if bottom and top and bottom != top:
                annotations.append(
                    line_to_line(view.view_id, bottom, top, (xmax + off, (ymin + ymax) / 2))
                )
                done_axes.add(ax_v)

    holes = 0
    seen: set = set()
    for view in views:
        if not view.is_orthographic or holes >= max_holes:
            continue
        geom = geometries.get(view.view_id, [])
        pts = [c for _, s, e in _lines(geom) for c in (s, e)]
        if not pts:
            continue
        ymin = min(p[1] for p in pts)
        size = max(
            max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - ymin, 1e-3
        )
        placed_x: List[float] = []
        for g in geom:
            if g.get("type") != "circle" or holes >= max_holes:
                continue
            c, r = g["data"]["center"], g["data"]["radius"]
            key = (view.view_id, round(c[0], 6), round(c[1], 6), round(r, 7))
            if key in seen:
                continue
            seen.add(key)
            # text below the view outline (outside the part), staggered when holes share an x
            level = sum(1 for x in placed_x if abs(x - c[0]) < 0.25 * size)
            placed_x.append(c[0])
            drop = (c[1] - ymin) + max(0.12 * size, 0.004) * (1 + level)
            # 45° leader down and to the right, ending below the view outline
            annotations.append(
                diameter(view.view_id, g["uniqueId"], c, r, (c[0] + drop, c[1] - drop))
            )
            holes += 1
    return annotations
