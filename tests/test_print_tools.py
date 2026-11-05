"""Tests for 3D printing exports and client robustness (rate limits, redirects)."""

import io
import math
import struct
import zipfile
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from onshape_mcp.api.client import OnshapeClient, OnshapeCredentials
from onshape_mcp.tools.print_tools import _stl_params, fits_bed, normalize_3mf_to_mm, stl_stats


def binary_stl(triangles):
    data = bytearray(b"\0" * 80) + struct.pack("<I", len(triangles))
    for tri in triangles:
        data += struct.pack("<3f", 0, 0, 1)
        for v in tri:
            data += struct.pack("<3f", *v)
        data += b"\0\0"
    return bytes(data)


def test_stl_stats_binary_and_ascii():
    stl = binary_stl([[(0, 0, 0), (40, 0, 0), (0, 20, 10)], [(1, 1, 1), (2, 2, 2), (3, 3, 3)]])
    n, mn, mx = stl_stats(stl)
    assert n == 2 and mn == [0, 0, 0] and mx == [40, 20, 10]
    ascii_stl = b"solid x\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 5 0 0\nvertex 0 5 2\nendloop\nendfacet\nendsolid"
    assert stl_stats(ascii_stl) == (1, [0.0, 0.0, 0.0], [5.0, 5.0, 2.0])
    assert stl_stats(b"")[0] == 0


def test_fits_bed_any_orientation():
    assert fits_bed([300, 10, 10], [256, 256, 310])
    assert not fits_bed([300, 300, 10], [256, 256, 256])


def test_stl_params_quality_and_units():
    p = _stl_params({"quality": "fine"})
    assert p["units"] == "millimeter" and p["mode"] == "binary"
    assert p["chordTolerance"] == pytest.approx(0.0001)
    assert p["angleTolerance"] == pytest.approx(math.radians(10))
    assert "chordTolerance" not in _stl_params({})
    assert _stl_params({"chordTolerance": 0.1})["chordTolerance"] == pytest.approx(0.0001)
    with pytest.raises(ValueError):
        _stl_params({"chordTolerance": 0.001})


def test_normalize_3mf_to_mm():
    model = (
        '<?xml version="1.0"?><model unit="meter" xmlns="x"><resources><object id="1"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="0.04" y="0.02" z="0.01"/></vertices></mesh></object>'
        '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0.1 0 0.005"/></build></model>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("[Content_Types].xml", "<Types/>")
    out = zipfile.ZipFile(io.BytesIO(normalize_3mf_to_mm(buf.getvalue())))
    xml = out.read("3D/3dmodel.model").decode()
    assert 'unit="millimeter"' in xml
    assert 'x="40" y="20" z="10"' in xml
    assert 'transform="1 0 0 0 1 0 0 0 1 100 0 5"' in xml
    assert out.read("[Content_Types].xml") == b"<Types/>"


def _response(status, headers=None, content=b"ok"):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.content = content
    r.raise_for_status = MagicMock()
    return r


async def test_client_retries_on_rate_limit(monkeypatch):
    client = OnshapeClient(OnshapeCredentials(access_key="a", secret_key="b"))
    http = MagicMock()
    http.get = AsyncMock(side_effect=[_response(429, {"Retry-After": "0"}), _response(200)])
    client._client = http
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    resp = await client._send("get", "https://cad.onshape.com/api/x")
    assert resp.status_code == 200 and http.get.await_count == 2


async def test_get_bytes_follows_redirect_with_auth_only_to_onshape():
    client = OnshapeClient(OnshapeCredentials(access_key="a", secret_key="b"))
    http = MagicMock()
    http.get = AsyncMock(
        side_effect=[
            _response(307, {"location": "https://cad-euw1.onshape.com/modelexport?x=1"}),
            _response(302, {"location": "https://evil.example.com/file"}),
            _response(200, content=b"STL"),
        ]
    )
    client._client = http
    assert await client.get_bytes("/api/partstudios/d/1/w/2/e/3/stl") == b"STL"
    calls = http.get.await_args_list
    assert "Authorization" in calls[1].kwargs["headers"]
    assert "Authorization" not in calls[2].kwargs["headers"]


def test_http_url_join_relative():
    assert str(httpx.URL("https://cad.onshape.com/api/a").join("/b")) == "https://cad.onshape.com/b"


def test_clean_and_match_names():
    from onshape_mcp.tools.print_tools import _clean, _matches

    assert _clean("SCS215 v5 (3) <1>") == "SCS215 v5"
    assert _matches(["scs215*"], ["SCS215 v5 (3) <1>", "MOTOR <1>"]) == 0
    assert _matches(["*_axis"], ["Robot <1>", "x_axis <1>"]) == 1
    assert _matches(["raspberry*"], ["Arm <1>"]) is None


async def test_assembly_export_skips_purchased_and_lists_them(tmp_path):
    from onshape_mcp.tools import ToolContext, print_tools

    D = "d" * 24
    asm = {
        "rootAssembly": {
            "instances": [
                {"id": "S1", "name": "Servo (1) <1>", "type": "Assembly"},
                {"id": "S2", "name": "Servo (2) <1>", "type": "Assembly"},
                {
                    "id": "A",
                    "name": "Arm <1>",
                    "type": "Part",
                    "documentId": D,
                    "elementId": "E",
                    "partId": "P1",
                },
                {
                    "id": "A2",
                    "name": "Arm <2>",
                    "type": "Part",
                    "documentId": D,
                    "elementId": "E",
                    "partId": "P1",
                },
                {
                    "id": "X",
                    "name": "x_axis <1>",
                    "type": "Part",
                    "documentId": D,
                    "elementId": "E",
                    "partId": "PX",
                },
            ],
            "occurrences": [
                {"path": ["S1", "M1"]},
                {"path": ["S1", "C1"]},
                {"path": ["S2", "M1"]},
                {"path": ["S2", "C1"]},
                {"path": ["A"]},
                {"path": ["A2"]},
                {"path": ["X"]},
            ],
        },
        "subAssemblies": [
            {
                "instances": [
                    {
                        "id": "M1",
                        "name": "Motor <1>",
                        "type": "Part",
                        "documentId": D,
                        "elementId": "E",
                        "partId": "PM",
                    },
                    {
                        "id": "C1",
                        "name": "Case <1>",
                        "type": "Part",
                        "documentId": D,
                        "elementId": "E",
                        "partId": "PC",
                    },
                ]
            }
        ],
    }
    client = MagicMock()

    async def get(path, params=None):
        if "/metadata/" in path:
            vendor = {"propertyId": print_tools.VENDOR_PROPERTY_ID, "value": "Feetech"}
            return {
                "items": [
                    {"partId": "PM", "properties": [vendor]},
                    {"partId": "PC", "properties": [vendor]},
                ]
            }
        if "/documents/" in path:
            return {"name": "Robot"}
        return asm

    client.get = AsyncMock(side_effect=get)
    client.get_bytes = AsyncMock(return_value=binary_stl([[(0, 0, 0), (10, 0, 0), (0, 10, 5)]]))
    ctx = ToolContext(client, MagicMock(), MagicMock(), MagicMock(), MagicMock())
    sc = MagicMock()
    sc.list_components = AsyncMock(return_value=[])
    ctx._standard_content = sc
    out = await print_tools.handle(
        "export_assembly_for_printing",
        {
            "documentId": D,
            "workspaceId": "w",
            "elementId": "e",
            "ignore": ["*_axis"],
            "directory": str(tmp_path),
        },
        ctx,
    )
    t = out[0].text
    assert "Exported 1 unique part(s)" in t and "Arm (x2).stl" in t
    assert "2x Servo" in t and "Ignored 1 helper" in t
    assert client.get_bytes.await_count == 1
