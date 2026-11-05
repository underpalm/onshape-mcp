"""Export / download / import of geometry between documents and the local disk.

Used for:
  * copying geometry from documents that only allow viewing + export
    (no "copy" permission) into your own documents,
  * downloading exports (STL, 3MF, STEP, ...) to local files,
  * importing local CAD files (STEP, STL, Parasolid, ...) into Onshape.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from .client import OnshapeClient

# Element types that can be exported as geometry
_EXPORT_ENDPOINT = {
    "PARTSTUDIO": "partstudios",
    "ASSEMBLY": "assemblies",
}


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ()äöüÄÖÜß]+", "_", name).strip(" ._")
    return name or "export"


class TransferManager:
    """Moves geometry between Onshape and files."""

    def __init__(self, client: OnshapeClient):
        self.client = client

    # ------------------------------------------------------------------ status
    async def wait_for_translation(
        self, translation_id: str, timeout: float = 300.0, poll: float = 1.5
    ) -> Dict[str, Any]:
        """Poll a translation until it is DONE (or raise on FAILED / timeout)."""
        waited = 0.0
        while True:
            status = await self.client.get(f"/api/translations/{translation_id}")
            state = status.get("requestState")
            if state == "DONE":
                return status
            if state == "FAILED":
                raise RuntimeError(
                    f"Onshape translation failed: {status.get('failureReason', 'unknown reason')}"
                )
            if waited >= timeout:
                raise TimeoutError(
                    f"Translation {translation_id} not finished after {timeout:.0f}s"
                )
            await asyncio.sleep(poll)
            waited += poll

    # ------------------------------------------------------------------ export
    async def get_element_info(
        self, document_id: str, wvm: str, wvm_id: str, element_id: str
    ) -> Dict[str, Any]:
        elements = await self.client.get(
            f"/api/documents/d/{document_id}/{wvm}/{wvm_id}/elements",
            params={"elementId": element_id},
        )
        for e in elements:
            if e.get("id") == element_id:
                return e
        raise ValueError(f"Element {element_id} not found in document {document_id}")

    async def export_element(
        self,
        document_id: str,
        wvm: str,
        wvm_id: str,
        element_id: str,
        format_name: str = "STEP",
        part_id: Optional[str] = None,
        element_type: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bytes, str]:
        """Export a Part Studio / Assembly and return ``(file_bytes, filename)``.

        Blob elements (uploaded files such as STEP) are downloaded as-is.
        """
        info = await self.get_element_info(document_id, wvm, wvm_id, element_id)
        element_type = element_type or info.get("elementType")
        name = info.get("name", "export")

        if element_type == "BLOB":
            data = await self.client.get_bytes(
                f"/api/blobelements/d/{document_id}/{wvm}/{wvm_id}/e/{element_id}"
            )
            return data, _safe_filename(info.get("filename") or name)

        endpoint = _EXPORT_ENDPOINT.get(element_type or "")
        if endpoint is None:
            raise ValueError(
                f"Element '{name}' is of type {element_type}; only Part Studios, Assemblies "
                "and uploaded files (blobs) can be exported."
            )

        fmt = format_name.upper()
        body: Dict[str, Any] = {
            "formatName": fmt,
            "storeInDocument": False,
            "destinationName": _safe_filename(name),
        }
        if part_id:
            body["partIds"] = part_id
        if options:
            body.update(options)

        request = await self.client.post(
            f"/api/v11/{endpoint}/d/{document_id}/{wvm}/{wvm_id}/e/{element_id}/translations",
            data=body,
        )
        status = await self.wait_for_translation(request["id"])
        ids: List[str] = status.get("resultExternalDataIds") or []
        if not ids:
            raise RuntimeError("Export finished but returned no file")
        data = await self.client.get_bytes(
            f"/api/documents/d/{status.get('documentId', document_id)}/externaldata/{ids[0]}"
        )
        ext = {"PARASOLID": "x_t", "STEP": "step", "GLTF": "gltf"}.get(fmt, fmt.lower())
        if data[:2] == b"PK" and ext != "3mf":  # several files -> Onshape zips them
            ext = "zip"
        return data, f"{_safe_filename(name)}.{ext}"

    # ------------------------------------------------------------------ import
    async def import_file(
        self,
        document_id: str,
        workspace_id: str,
        file_bytes: bytes,
        filename: str,
        flatten_assemblies: bool = False,
        y_axis_is_up: bool = False,
        wait: bool = True,
    ) -> Dict[str, Any]:
        """Upload a CAD file into a document and let Onshape translate it.

        Creates new Part Studio / Assembly elements in the target workspace.
        Returns the finished translation status (``resultElementIds`` lists the
        new elements).
        """
        fields = {
            "translate": "true",
            "flattenAssemblies": "true" if flatten_assemblies else "false",
            "yAxisIsUp": "true" if y_axis_is_up else "false",
            "allowFaultyParts": "true",
        }
        request = await self.client.post_multipart(
            f"/api/translations/d/{document_id}/w/{workspace_id}",
            files={"file": (filename, file_bytes, "application/octet-stream")},
            data=fields,
        )
        if not wait:
            return request
        return await self.wait_for_translation(request["id"])

    async def import_local_file(
        self, document_id: str, workspace_id: str, path: str, flatten_assemblies: bool = False
    ) -> Dict[str, Any]:
        path = os.path.expanduser(path)
        with open(path, "rb") as fh:
            data = fh.read()
        return await self.import_file(
            document_id, workspace_id, data, os.path.basename(path), flatten_assemblies
        )

    async def transfer_element(
        self,
        source_document_id: str,
        source_wvm: str,
        source_wvm_id: str,
        source_element_id: str,
        target_document_id: str,
        target_workspace_id: str,
        format_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Export an element from any readable document and import it into yours.

        Part Studios travel as Parasolid (Onshape's native kernel format, best
        fidelity); assemblies as STEP so the assembly structure is kept.
        """
        info = await self.get_element_info(
            source_document_id, source_wvm, source_wvm_id, source_element_id
        )
        etype = info.get("elementType")
        if format_name is None:
            format_name = "PARASOLID" if etype == "PARTSTUDIO" else "STEP"
        logger.info(f"Transferring '{info.get('name')}' ({etype}) as {format_name}")
        data, filename = await self.export_element(
            source_document_id,
            source_wvm,
            source_wvm_id,
            source_element_id,
            format_name=format_name,
            element_type=etype,
        )
        status = await self.import_file(target_document_id, target_workspace_id, data, filename)
        return {
            "sourceName": info.get("name"),
            "sourceType": etype,
            "format": format_name if etype != "BLOB" else "original file",
            "bytes": len(data),
            "resultElementIds": status.get("resultElementIds") or [],
        }
