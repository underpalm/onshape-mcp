"""Tests for drawing helpers (auto dimension planning, annotations, export saving)."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from onshape_mcp.api.drawings import DrawingManager, ViewInfo, diameter, note, plan_auto_dimensions
from onshape_mcp.exports import save_bytes

FRONT = [1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0]  # screen x = model X, screen y = model Z
SIDE = [0, -1, 0, 0, 0, 0, 1, 0, -1, 0, 0, 0]  # screen x = -model Y, screen y = model Z
TOP = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]  # screen x = X, screen y = Y
ISO = [0.71, -0.71, 0, 0, 0.41, 0.41, 0.82, 0, -0.58, -0.58, 0.58, 0]


def rect(prefix, w, h):
    pts = [((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))]
    return [
        {"type": "line", "uniqueId": f"{prefix}{i}", "data": {"start": [*s, 0], "end": [*e, 0]}}
        for i, (s, e) in enumerate(pts)
    ]


def test_view_orientation():
    assert ViewInfo("f", matrix=FRONT).is_orthographic
    assert not ViewInfo("i", matrix=ISO).is_orthographic
    assert ViewInfo("f", matrix=FRONT).screen_axes() == (0, 2)
    assert ViewInfo("t", matrix=TOP).screen_axes() == (0, 1)


def test_plan_dimensions_each_axis_once_plus_holes():
    views = [
        ViewInfo("f", matrix=FRONT),
        ViewInfo("s", matrix=SIDE),
        ViewInfo("t", matrix=TOP),
        ViewInfo("i", matrix=ISO),
    ]
    hole = {
        "type": "circle",
        "uniqueId": "c1",
        "data": {"center": [0.008, 0.01, 0], "radius": 0.0016},
    }
    geoms = {
        "f": rect("f", 0.04, 0.01),
        "s": rect("s", 0.02, 0.01),
        "t": rect("t", 0.04, 0.02) + [hole, dict(hole)],
        "i": rect("i", 1, 1),
    }
    plan = plan_auto_dimensions(views, geoms)
    kinds = [a["type"] for a in plan]
    assert kinds.count("Onshape::Dimension::LineToLine") == 3  # X, Z (front) + Y (side)
    assert kinds.count("Onshape::Dimension::Diametric") == 1  # duplicate circle skipped
    assert all(
        a["lineToLineDimension"]["edge1"]["viewId"] in ("f", "s")
        for a in plan
        if "lineToLineDimension" in a
    )
    dia = plan[-1]["diametricDimension"]
    assert dia["textPosition"]["coordinate"][1] < 0  # text below the part outline


def test_diameter_leader_points_towards_text():
    d = diameter("v", "c", (0, 0), 1.0, (3, -3))["diametricDimension"]
    cx, cy = d["chordPoint"]["coordinate"][:2]
    assert cx > 0 and cy < 0
    assert d["farChordPoint"]["coordinate"][0] == pytest.approx(-cx)


def test_note_in_sheet_mm():
    n = note("Werkstoff: PLA", 20, 25)
    assert n["note"]["position"]["coordinate"] == [20, 25, 0.0] and n["note"]["textHeight"] == 3.5


def test_save_bytes_never_overwrites(tmp_path):
    p1 = save_bytes(b"a", "Teil 1.pdf", str(tmp_path))
    p2 = save_bytes(b"b", "Teil 1.pdf", str(tmp_path))
    assert p1 != p2 and os.path.basename(p2) == "Teil 1_1.pdf"
    assert open(p1, "rb").read() == b"a"


async def test_create_uses_iso_first_angle_mm():
    client = MagicMock()
    client.post = AsyncMock(return_value={"id": "dr1"})
    await DrawingManager(client).create("d", "w", "e", "Z", part_id="JHD", size="a3")
    body = client.post.call_args.kwargs["data"]
    assert body["projection"] == "first" and body["standard"] == "ISO"
    assert (
        body["units"] == "MILLIMETER"
        and body["size"] == "A3"
        and body["decimalSeparator"] == "COMMA"
    )
    assert body["partId"] == "JHD" and body["referenceType"] == 3
