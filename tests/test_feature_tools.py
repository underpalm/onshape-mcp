"""Tests for the generic feature builder, sketch helpers, FS values and materials."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from onshape_mcp.api.fsvalues import eval_result, fs_to_py
from onshape_mcp.builders.generic import (
    FeatureSpecError,
    GenericFeatureBuilder,
    describe,
    index_specs,
)
from onshape_mcp.builders.sketch import SketchBuilder
from onshape_mcp.tools import ToolContext, feature_tools, physical_tools
from onshape_mcp.tools.feature_tools import _add_shape, _entity_query, _Projector, sketch_origin
from onshape_mcp.tools.physical_tools import MaterialLibrary, effective_density

pytestmark = pytest.mark.metric

SPECS = {
    "featureSpecs": [
        {
            "type": 1,
            "message": {
                "featureType": "shell",
                "featureTypeName": "Shell",
                "parameters": [
                    {"typeName": "BTParameterSpecQuery", "message": {"parameterId": "entities"}},
                    {
                        "typeName": "BTParameterSpecQuantity",
                        "message": {
                            "parameterId": "thickness",
                            "quantityType": "LENGTH",
                            "defaultValue": {"message": {"value": 0.0025}},
                        },
                    },
                    {"typeName": "BTParameterSpecBoolean", "message": {"parameterId": "isHollow"}},
                    {
                        "typeName": "BTParameterSpecEnum",
                        "message": {
                            "parameterId": "operationType",
                            "enumName": "NewBodyOperationType",
                            "options": [
                                {"message": {"option": "NEW"}},
                                {"message": {"option": "REMOVE"}},
                            ],
                        },
                    },
                    {
                        "typeName": "BTParameterSpecQuantity",
                        "message": {"parameterId": "angle", "quantityType": "ANGLE"},
                    },
                    {
                        "typeName": "BTParameterSpecQuantity",
                        "message": {"parameterId": "count", "quantityType": "INTEGER"},
                    },
                ],
            },
        }
    ]
}


@pytest.fixture
def shell_spec():
    return index_specs(SPECS)["shell"]


def test_generic_builder_types_values(shell_spec):
    b = GenericFeatureBuilder(shell_spec, {"top": "JDC"})
    body = b.build(
        "S",
        {
            "entities": ["JHK", {"sketch": "F1"}, "Top"],
            "thickness": 2,
            "isHollow": 1,
            "operationType": "remove",
            "angle": 30,
            "count": 4.0,
        },
    )
    params = {p["parameterId"]: p for p in body["feature"]["parameters"]}
    assert body["feature"]["featureType"] == "shell"
    assert params["thickness"]["expression"] == "2 mm"
    assert params["angle"]["expression"] == "30 deg"
    assert params["count"]["expression"] == "4" and params["count"]["isInteger"]
    assert params["isHollow"]["value"] is True
    assert params["operationType"]["value"] == "REMOVE"
    queries = params["entities"]["queries"]
    assert (
        queries[0]["btType"] == "BTMIndividualSketchRegionQuery-140"
        and queries[0]["featureId"] == "F1"
    )
    assert queries[1]["deterministicIds"] == ["JHK", "JDC"]


def test_generic_builder_rejects_bad_values(shell_spec):
    b = GenericFeatureBuilder(shell_spec)
    with pytest.raises(FeatureSpecError, match="Unknown parameter"):
        b.build("S", {"nope": 1})
    with pytest.raises(FeatureSpecError, match="not one of"):
        b.build("S", {"operationType": "EXPLODE"})
    with pytest.raises(FeatureSpecError, match="not resolved"):
        b.build("S", {"entities": "Front"})


def test_describe_lists_parameters(shell_spec):
    d = describe(shell_spec)
    assert "thickness: Quantity length [mm], default 2.5" in d
    assert "operationType: Enum one of ['NEW', 'REMOVE']" in d


def test_fs_values_conversion():
    raw = {
        "btType": "BTFSValueArray",
        "value": [
            {
                "btType": "BTFSValueMap",
                "value": [
                    {
                        "key": {"btType": "BTFSValueString", "value": "id"},
                        "value": {"btType": "BTFSValueString", "value": "JHK"},
                    },
                    {
                        "key": {"btType": "BTFSValueString", "value": "r"},
                        "value": {"btType": "BTFSValueWithUnits", "value": 0.002},
                    },
                ],
            }
        ],
    }
    assert fs_to_py(raw) == [{"id": "JHK", "r": 0.002}]
    with pytest.raises(ValueError, match="FeatureScript error"):
        eval_result({"result": None, "notices": [{"message": "boom", "level": "ERROR"}]})


def test_entity_query_filters():
    q = _entity_query(
        {"entityType": "FACE", "createdBy": "F1", "geometry": "plane", "facing": "-z"}
    )
    assert q.startswith(
        'qParallelPlanes(qGeometry(qSketchFilter(qConstructionFilter(qCreatedBy(makeId("F1")'
    )
    assert "vector(0, 0, -1)" in q
    assert "qBodyType" in _entity_query({"entityType": "BODY"})
    assert _entity_query({"query": "qEverything()"}) == "(qEverything())"


def test_sketch_origin_and_world_projection():
    cs = {"origin": [0.02, 0.01, 0.0], "normal": [0, 0, -1], "x": [1, 0, 0]}
    assert sketch_origin(cs) == (0, 0, 0)
    proj = _Projector(cs)
    assert proj([8, 10, 0]) == pytest.approx((8, -10))
    side = {"origin": [0.04, 0.01, 0.005], "normal": [1, 0, 0], "x": [0, 1, 0]}
    assert sketch_origin(side) == pytest.approx((0.04, 0, 0))
    assert _Projector(side)([40, 6, 4]) == pytest.approx((6, 4))


def test_slot_and_center_rectangle_shapes():
    sk = SketchBuilder(name="S", plane_id="p")
    ident = lambda p: (float(p[0]), float(p[1]))  # noqa: E731
    _add_shape(sk, {"type": "slot", "start": [0, 0], "end": [10, 0], "width": 4}, ident)
    _add_shape(sk, {"type": "centerRectangle", "center": [0, 0], "width": 10, "height": 4}, ident)
    _add_shape(sk, {"type": "circle", "center": [0, 0], "diameter": 6}, ident)
    assert len(sk.entities) >= 4 + 4 + 1
    with pytest.raises(ValueError, match="Unknown shape"):
        _add_shape(sk, {"type": "blob"}, ident)


def test_effective_density():
    assert effective_density(1250, 100) == pytest.approx(1250)
    assert effective_density(1250, 20, 0.35) == pytest.approx(1250 * (0.35 + 0.65 * 0.2))


LIB = {
    "name": "Onshape Material Library",
    "propertyDefinitions": [
        {"name": "DENS", "type": "REAL", "units": "kg/m^3", "displayName": "Density"}
    ],
    "materials": [
        {
            "id": "PLA",
            "displayName": "PLA",
            "category": "Plastic",
            "propertyValues": {"DENS": "1250"},
        },
        {
            "id": "Aluminum - 6061",
            "displayName": "Aluminum - 6061",
            "propertyValues": {"DENS": "2720"},
        },
    ],
}


async def test_material_lookup_and_value():
    lib = MaterialLibrary(MagicMock())
    lib._lib = LIB
    assert (await lib.find("Aluminium"))["id"] == "Aluminum - 6061"
    v = await lib.material_value(await lib.find("pla"), None)
    assert v["id"] == "PLA" and v["properties"][0] == {
        "name": "DENS",
        "value": "1250",
        "type": "REAL",
        "units": "kg/m^3",
        "displayName": "Density",
        "category": "",
        "description": "",
    }
    custom = await lib.material_value(await lib.find("PLA"), 600)
    assert custom["displayName"] == "PLA (600 kg/m³)" and custom["properties"][0]["value"] == "600"
    with pytest.raises(ValueError):
        await lib.find("Unobtainium")


async def test_set_part_material_all_parts():
    client = MagicMock()
    client.get = AsyncMock(
        return_value=[
            {"partId": "JHD", "bodyType": "solid"},
            {"partId": "JKD", "bodyType": "sheet"},
        ]
    )
    client.post = AsyncMock(return_value={"status": "SUCCEEDED"})
    ctx = ToolContext(client, MagicMock(), MagicMock(), MagicMock(), MagicMock())
    ctx._materials = MaterialLibrary(client)
    ctx._materials._lib = LIB
    out = await physical_tools.handle(
        "set_part_material",
        {
            "documentId": "d",
            "workspaceId": "w",
            "elementId": "e",
            "material": "PLA",
            "infillPercent": 20,
        },
        ctx,
    )
    assert "set on 1 part(s): JHD" in out[0].text and "20% infill" in out[0].text
    body = client.post.call_args.kwargs["data"]["properties"][0]
    assert body["propertyId"] == physical_tools.MATERIAL_PROPERTY_ID


async def test_feature_tool_reports_info_status():
    pm = MagicMock()
    pm.add_feature = AsyncMock(
        return_value={
            "feature": {"featureId": "F9", "name": "X"},
            "featureState": {"featureStatus": "INFO"},
        }
    )
    client = MagicMock()
    client.get = AsyncMock(return_value=SPECS)
    ctx = ToolContext(client, MagicMock(), pm, MagicMock(), MagicMock())
    out = await feature_tools.handle(
        "create_feature",
        {
            "documentId": "d",
            "workspaceId": "w",
            "elementId": "e",
            "featureType": "shell",
            "parameters": {"thickness": 1},
        },
        ctx,
    )
    assert "status: INFO" in out[0].text and "oppositeDirection" in out[0].text
