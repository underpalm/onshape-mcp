"""Tests for the configurable (metric by default) length unit."""

import pytest

from onshape_mcp import units
from onshape_mcp.analysis.positioning import build_absolute_translation_matrix
from onshape_mcp.builders.extrude import ExtrudeBuilder
from onshape_mcp.builders.fillet import FilletBuilder
from onshape_mcp.builders.mate import build_transform_matrix
from onshape_mcp.builders.sketch import SketchBuilder, SketchPlane

pytestmark = pytest.mark.metric


def test_default_unit_is_mm(monkeypatch):
    monkeypatch.delenv("ONSHAPE_LENGTH_UNIT", raising=False)
    units.load_from_env()
    assert units.get_length_unit() == "mm"


@pytest.mark.parametrize(
    "raw,expected",
    [("mm", "mm"), ("Millimeter", "mm"), ("cm", "cm"), ("meters", "m"), ("inch", "in")],
)
def test_unit_aliases(raw, expected):
    units.set_length_unit(raw)
    assert units.get_length_unit() == expected


def test_invalid_unit_rejected():
    with pytest.raises(ValueError):
        units.set_length_unit("furlong")


def test_env_variable(monkeypatch):
    monkeypatch.setenv("ONSHAPE_LENGTH_UNIT", "cm")
    units.load_from_env()
    assert units.get_length_unit() == "cm"
    assert units.to_m(12) == pytest.approx(0.12)


def test_mm_conversion_roundtrip():
    assert units.to_m(25) == pytest.approx(0.025)
    assert units.from_m(0.025) == pytest.approx(25)
    assert units.length_expr(12.5) == "12.5 mm"
    assert units.fmt(3) == "3.00 mm"
    assert units.volume_unit() == "mm³"


def test_extrude_depth_expression_in_mm():
    feature = ExtrudeBuilder(name="E", sketch_feature_id="sk1", depth=10).build()
    params = feature["feature"]["parameters"]
    depth = next(p for p in params if p.get("parameterId") == "depth")
    assert depth["expression"] == "10 mm"


def test_fillet_radius_in_mm():
    feature = FilletBuilder(name="F", radius=2).add_edge("JHK").build()
    params = feature["feature"]["parameters"]
    radius = next(p for p in params if p.get("parameterId") == "radius")
    assert radius["expression"] == "2 mm"


def test_sketch_circle_converted_from_mm():
    sketch = SketchBuilder(name="S", plane=SketchPlane.FRONT, plane_id="p")
    sketch.add_circle(center=(10, 20), radius=5)
    circle = sketch.entities[0]["geometry"]
    assert circle["radius"] == pytest.approx(0.005)
    assert circle["xCenter"] == pytest.approx(0.010)
    assert circle["yCenter"] == pytest.approx(0.020)


def test_transform_and_position_matrices_in_mm():
    m = build_transform_matrix(tx=100, ty=0, tz=-50)
    assert m[3] == pytest.approx(0.1)
    assert m[11] == pytest.approx(-0.05)
    a = build_absolute_translation_matrix(10, 20, 30)
    assert (a[3], a[7], a[11]) == pytest.approx((0.01, 0.02, 0.03))


async def test_tool_descriptions_mention_mm():
    from onshape_mcp.server import list_tools

    tools = {t.name: t for t in await list_tools()}
    depth = tools["create_extrude"].inputSchema["properties"]["depth"]["description"]
    assert "millimeters (mm)" in depth
    assert "{LEN}" not in str([t.model_dump() for t in tools.values()])
    assert "inches" not in str([t.model_dump() for t in tools.values()])


@pytest.mark.parametrize("value,expected", [(None, False), ("true", True), ("0", False)])
def test_documents_public_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ONSHAPE_DOCUMENTS_PUBLIC", raising=False)
    else:
        monkeypatch.setenv("ONSHAPE_DOCUMENTS_PUBLIC", value)
    assert units.documents_public_by_default() is expected
