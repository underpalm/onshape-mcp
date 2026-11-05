"""Tests for URL parsing, copy tools and the transfer manager."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from onshape_mcp.api.transfer import TransferManager
from onshape_mcp.api.urls import parse_onshape_url
from onshape_mcp.tools import ToolContext, copy_tools, extension_tools

DID = "16c9706360b5ad34f9c8db49"
WID = "2edfa54c83253c120fbc9e58"
EID = "066b283a1dd44691595405f9"
URL = f"https://cad.onshape.com/documents/{DID}/w/{WID}/e/{EID}"


def test_parse_full_url():
    ref = parse_onshape_url(URL)
    assert (ref.document_id, ref.workspace_id, ref.element_id) == (DID, WID, EID)
    assert ref.version_id is None


def test_parse_version_and_document_only_urls():
    ref = parse_onshape_url(f"https://cad.onshape.com/documents/{DID}/v/{WID}/e/{EID}?x=1")
    assert ref.version_id == WID and ref.workspace_id is None
    ref = parse_onshape_url(f"https://cad.onshape.com/documents/{DID}")
    assert ref.document_id == DID and ref.wvm is None and ref.element_id is None


def test_parse_invalid_url():
    with pytest.raises(ValueError):
        parse_onshape_url("https://example.com/nothing")


def test_extension_tools_registered():
    names = {t.name for t in extension_tools()}
    assert {"inspect_onshape_link", "copy_document", "import_from_document"} <= names


def _ctx(client=None, transfer=None):
    client = client or MagicMock()
    docs = MagicMock()
    docs.get_default_workspace_id = AsyncMock(return_value=WID)
    return ToolContext(
        client=client,
        document_manager=docs,
        partstudio_manager=MagicMock(),
        assembly_manager=MagicMock(),
        transfer_manager=transfer or MagicMock(),
    )


def _http_error(code):
    request = httpx.Request("POST", "https://cad.onshape.com/x")
    return httpx.HTTPStatusError(
        "err", request=request, response=httpx.Response(code, request=request)
    )


async def test_inspect_reports_permissions():
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[
            {
                "name": "LeLamp",
                "owner": {"name": "Lab"},
                "permission": "ANONYMOUS_ACCESS",
                "permissionSet": ["ANONYMOUS_ACCESS", "EXPORT"],
            },
            [
                {"elementType": "PARTSTUDIO", "name": "LampBase", "id": EID},
                {"elementType": "BILLOFMATERIALS", "name": "BOM", "id": "b"},
            ],
        ]
    )
    out = await copy_tools.handle("inspect_onshape_link", {"url": URL}, _ctx(client))
    t = out[0].text
    assert "Full copy with history (copy_document / copy_element): NO" in t
    assert "import_from_document): YES" in t
    assert "LampBase" in t and "<- from link" in t and "BOM" not in t


async def test_copy_document_403_suggests_import():
    ctx = _ctx()
    ctx.document_manager.copy_document = AsyncMock(side_effect=_http_error(403))
    out = await copy_tools.handle("copy_document", {"url": URL}, ctx)
    assert "import_from_document" in out[0].text


async def test_copy_document_success():
    ctx = _ctx()
    ctx.document_manager.copy_document = AsyncMock(
        return_value={
            "newDocumentId": "n" * 24,
            "newWorkspaceId": "w" * 24,
            "name": "X",
            "public": False,
        }
    )
    out = await copy_tools.handle("copy_document", {"url": URL, "newName": "X"}, ctx)
    assert "private" in out[0].text and "n" * 24 in out[0].text
    ctx.document_manager.copy_document.assert_awaited_once_with(
        DID, WID, new_name="X", is_public=None
    )


async def test_import_from_document_uses_link_element():
    transfer = MagicMock()
    transfer.transfer_element = AsyncMock(
        return_value={
            "sourceName": "LampBase",
            "sourceType": "PARTSTUDIO",
            "format": "PARASOLID",
            "bytes": 2048,
            "resultElementIds": ["e1"],
        }
    )
    ctx = _ctx(transfer=transfer)
    out = await copy_tools.handle(
        "import_from_document", {"url": URL, "targetDocumentId": "t" * 24}, ctx
    )
    assert "OK  LampBase" in out[0].text
    transfer.transfer_element.assert_awaited_once_with(
        DID, "w", WID, EID, "t" * 24, WID, format_name=None
    )


async def test_import_cad_file_missing_file():
    out = await copy_tools.handle(
        "import_cad_file", {"filePath": "/nope/servo.step", "documentId": DID}, _ctx()
    )
    assert "File not found" in out[0].text


async def test_unknown_tool_returns_none():
    assert await copy_tools.handle("create_extrude", {}, _ctx()) is None


async def test_transfer_partstudio_uses_parasolid():
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[
            [{"id": EID, "name": "LampBase", "elementType": "PARTSTUDIO"}],  # info (transfer)
            [{"id": EID, "name": "LampBase", "elementType": "PARTSTUDIO"}],  # info (export)
            {"requestState": "DONE", "resultExternalDataIds": ["f1"], "documentId": DID},
            {"requestState": "DONE", "resultElementIds": ["new1"]},
        ]
    )
    client.post = AsyncMock(return_value={"id": "tr1"})
    client.get_bytes = AsyncMock(return_value=b"**PARASOLID")
    client.post_multipart = AsyncMock(return_value={"id": "tr2"})
    result = await TransferManager(client).transfer_element(DID, "w", WID, EID, "t" * 24, WID)
    assert result["format"] == "PARASOLID" and result["resultElementIds"] == ["new1"]
    assert client.post.call_args.kwargs["data"]["formatName"] == "PARASOLID"
    filename = client.post_multipart.call_args.kwargs["files"]["file"][0]
    assert filename == "LampBase.x_t"


async def test_translation_failure_raises():
    client = MagicMock()
    client.get = AsyncMock(return_value={"requestState": "FAILED", "failureReason": "bad file"})
    with pytest.raises(RuntimeError, match="bad file"):
        await TransferManager(client).wait_for_translation("x")
