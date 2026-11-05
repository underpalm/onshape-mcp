"""Tests for the standard content rule interpreter and the parts tools."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from onshape_mcp.standard_content import Q, parse_rules, resolve
from onshape_mcp.tools import ToolContext, parts_tools

RULES = r"""
FeatureScript 608;
import(path : "onshape/std/standardContentUtils.fs", version : "608.0");

// Data Source: made up for tests
const dataTables =
{
    "DATA_TEST-1_MAIN" :
    {
        "fields": [ "SIZE_DESIGNATION", "HEAD_DIAMETER", "PITCH" ],
        "rows": [
            ["M3", "5.50", "0.50"],
            ["M4", "7.00", "0.70"],   // trailing comma below
        ]
    } as DataTable,
    "DATA_TEST_LENGTHS" :
    {
        "fields": ["SIZE_DESIGNATION", "LENGTH"],
        "rows": [ ["M3", "6"], ["M3", "10"], ["M4", "8"], ["M4", "12"] ]
    } as DataTable
} as DataTables;

const defaultValues = [
    {"SizeId" : ""},
    {"LengthId" : ""},
    {"FaceId" : ""},
    {"doNotFilterParameterValues" : "false"}
] as ParameterSpecs;

function main()
{
    var filterArray = scMakeFilterArray(defaultValues);
    filterArray.parameters[0] = ProcessSize(filterArray, filterArray.parameters[0], defaultValues[0].SizeId);
    filterArray.parameters[1] = ProcessLength(filterArray, filterArray.parameters[1], defaultValues[1].LengthId, false);
    filterArray.parameters[2] = ProcessFace(filterArray, filterArray.parameters[2], defaultValues[2].FaceId);
    return filterArray;
}

function ProcessSize(currentFilterMap is ContentFilter, sizeMap is ParameterFilter, desiredDefaultValue is string) returns ParameterFilter
{
    sizeMap.DataTable = "DATA_TEST-1_MAIN";
    sizeMap.ParameterValues = scGetFieldValuesFromTable(dataTables, "DATA_TEST-1_MAIN", "SIZE_DESIGNATION", {});
    sizeMap.DisplayName = "Size";
    var headDiameter = scStringToValueWithUnits(scGetDefaultDataValue(dataTables, "HEAD_DIAMETER", sizeMap), millimeter);
    var pitchNum = stringToNumber(scGetDefaultDataValue(dataTables, "PITCH", sizeMap));
    var triangleHeight = (0.8660254 * pitchNum) * millimeter;
    sizeMap.ConfigurationParameters = [
        { "HeadDiameter" : headDiameter },
        { "TriangleHeight" : triangleHeight },
    ];
    return sizeMap;
}

function ProcessLength(currentFilterMap is ContentFilter, lengthMap is ParameterFilter, desiredDefaultValue is string, doNotFilter is boolean) returns ParameterFilter
{
    lengthMap.DataTable = "DATA_TEST_LENGTHS";
    var selectedBaseSize = scGetParameterDefaultDataValue(dataTables, "SizeId", "SIZE_DESIGNATION", currentFilterMap);
    lengthMap.ParameterValues = scGetFieldValuesFromTable(dataTables, "DATA_TEST_LENGTHS", "LENGTH", { "SIZE_DESIGNATION" : selectedBaseSize });
    lengthMap.DisplayName = "Length";
    lengthMap.ConfigurationParameters = [
        { "Length" : scStringToValueWithUnits(scGetDefaultValue(lengthMap), millimeter) }
    ];
    return lengthMap;
}

function ProcessFace(currentFilterMap is ContentFilter, faceMap is ParameterFilter, desiredDefaultValue is string) returns ParameterFilter
{
    faceMap.ParameterValues = scStaticParameterValues(["Washer face", "Chamfered"]);
    faceMap.displayName = "Bearing face";
    var defaultValue = scGetDefaultValue(faceMap);
    if (defaultValue == "Chamfered")
    {
        faceMap.ConfigurationParameters = [ { "Chamfer" : true }, { "Style" : "C" } ];
    }
    else
    {
        faceMap.ConfigurationParameters = [ { "Chamfer" : false }, { "Style" : "W" } ];
    }
    if (defaultValue != "Chamfered" && 1 < 2)
    {
        faceMap.ConfigurationParameters = [ { "Extra" : 2 * millimeter } ];
    }
    return faceMap;
}
"""


@pytest.fixture
def rules():
    return parse_rules(RULES)


def test_parse_structure(rules):
    ids = [p.parameter_id for p in rules.params]
    assert ids == ["SizeId", "LengthId", "FaceId"]
    assert rules.params[0].table == "DATA_TEST-1_MAIN"
    assert rules.params[2].static_values == ["Washer face", "Chamfered"]
    assert len(rules.params[2].branches) == 2  # two separate if-chains


def test_resolve_defaults(rules):
    r = resolve(rules, {})
    assert r.display == {"Size": "M3", "Length": "6", "Bearing face": "Washer face"}
    assert r.options["Length"] == ["6", "10"]
    cfg = r.configuration_string()
    assert "HeadDiameter=5.5 mm" in cfg
    assert "TriangleHeight=0.433013 mm" in cfg
    assert "Chamfer=false" in cfg and "Style=W" in cfg and "Extra=2 mm" in cfg


def test_resolve_choices_filter_dependent_options(rules):
    r = resolve(rules, {"size": "M4", "Length": 12, "Bearing face": "Chamfered"})
    assert r.selection == {"SizeId": "M4", "LengthId": "12", "FaceId": "Chamfered"}
    assert r.options["Length"] == ["8", "12"]
    assert r.configuration["HeadDiameter"] == Q(7.0, "mm")
    assert r.configuration["Chamfer"] is True and "Extra" not in r.configuration


def test_invalid_choice_lists_options(rules):
    with pytest.raises(ValueError, match="Options: 8, 12"):
        resolve(rules, {"Size": "M4", "Length": "10"})


def test_evaluator_never_executes_code():
    evil = RULES.replace(
        "var pitchNum = stringToNumber(",
        'var pitchNum = __import__("os").system("x") + stringToNumber(',
    )
    with pytest.raises(ValueError):
        resolve(parse_rules(evil), {})


def _ctx():
    client = MagicMock()
    asm_before = {"rootAssembly": {"instances": []}}
    asm_after = {
        "rootAssembly": {
            "instances": [{"id": "i1", "name": "Hex socket head cap screw M4 x 12 <1>"}]
        }
    }
    client.get = AsyncMock(side_effect=[asm_before, asm_after])
    client.post = AsyncMock(return_value={})
    return ToolContext(
        client=client,
        document_manager=MagicMock(),
        partstudio_manager=MagicMock(),
        assembly_manager=MagicMock(),
        transfer_manager=MagicMock(),
    )


async def test_insert_standard_part_posts_configuration(rules):
    from onshape_mcp.standard_content import ComponentSource, StandardComponent

    ctx = _ctx()
    sc = MagicMock()
    sc.resolve = AsyncMock(
        return_value=(
            StandardComponent("sc1", "Test screw ISO 0", "Bolts & screws", "x", "ISO"),
            ComponentSource("sd", "sv", "se", "JHD", RULES),
            resolve(rules, {"Size": "M4", "Length": "12"}),
        )
    )
    ctx._standard_content = sc
    out = await parts_tools.handle(
        "insert_standard_part",
        {"documentId": "d", "workspaceId": "w", "elementId": "e", "component": "Test screw"},
        ctx,
    )
    body = ctx.client.post.call_args.kwargs["data"]
    assert body["versionId"] == "sv" and body["partId"] == "JHD"
    assert "HeadDiameter=7 mm" in body["configuration"] and "Length=12 mm" in body["configuration"]
    assert "Inserted 1 x Test screw ISO 0" in out[0].text and "i1" in out[0].text
