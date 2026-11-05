"""Tools for ready-made parts: ISO/DIN standard parts and parts from other documents."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
from mcp.types import Tool

from ..analysis.positioning import build_absolute_translation_matrix
from ..api.urls import parse_onshape_url
from ..standard_content import StandardContentManager
from . import text

_ASSEMBLY_PROPS: Dict[str, Any] = {
    "documentId": {"type": "string", "description": "Document ID of the target assembly"},
    "workspaceId": {"type": "string", "description": "Workspace ID of the target assembly"},
    "elementId": {"type": "string", "description": "Element ID of the target assembly"},
    "count": {
        "type": "integer",
        "description": "How many instances to insert (default 1)",
        "default": 1,
    },
    "position": {
        "type": "array",
        "items": {"type": "number"},
        "description": "Optional absolute position [x, y, z] in {LEN} for the inserted instance(s)",
    },
}

TOOLS: List[Tool] = [
    Tool(
        name="search_standard_parts",
        description=(
            "Search Onshape's standard parts library (ISO, DIN, ANSI, PEM: screws, bolts, nuts, "
            "washers, pins, retaining rings, keys, O-rings). Metric ISO/DIN parts are listed first. "
            "Example queries: 'socket head cap screw', '4762', 'washer DIN 125', 'nut 4032'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search words (all must match)"},
                "standard": {
                    "type": "string",
                    "description": "Optional filter: ISO, DIN, ANSI inch, PEM®, NAS",
                },
                "limit": {"type": "integer", "default": 30},
            },
        },
    ),
    Tool(
        name="get_standard_part_options",
        description=(
            "Show the selectable options (Size, Length, Type, ...) of a standard part, optionally "
            "after some choices are made (e.g. {'Size': 'M3'} lists the lengths available for M3)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "component": {
                    "type": "string",
                    "description": "Standard part name or id (from search_standard_parts)",
                },
                "options": {
                    "type": "object",
                    "description": "Choices made so far, e.g. {'Size': 'M3', 'Length': '10'}",
                },
            },
            "required": ["component"],
        },
    ),
    Tool(
        name="insert_standard_part",
        description=(
            "Insert an ISO/DIN/ANSI standard part (screw, nut, washer, pin, ...) into an assembly. "
            "Example: component='Hex socket head cap screw ISO 4762', options={'Size': 'M3', "
            "'Length': '10'}, count=4. Options not given use the first available value."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_ASSEMBLY_PROPS,
                "component": {"type": "string", "description": "Standard part name or id"},
                "options": {
                    "type": "object",
                    "description": "Choices, e.g. {'Size': 'M3', 'Length': '10'}",
                },
            },
            "required": ["documentId", "workspaceId", "elementId", "component"],
        },
    ),
    Tool(
        name="insert_part_from_document",
        description=(
            "Insert a part, a whole Part Studio or a sub-assembly from ANOTHER document (or the "
            "same one) into an assembly, e.g. a servo, motor, Raspberry Pi or bearing someone "
            "modelled. Uses a linked instance when allowed (needs a released version), otherwise "
            "imports the geometry into the target document first and inserts that copy."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_ASSEMBLY_PROPS,
                "sourceUrl": {
                    "type": "string",
                    "description": "Link to the source element (…/documents/<did>/w|v/<id>/e/<eid>)",
                },
                "sourceDocumentId": {"type": "string"},
                "sourceWorkspaceId": {"type": "string"},
                "sourceVersionId": {"type": "string"},
                "sourceElementId": {"type": "string"},
                "partId": {
                    "type": "string",
                    "description": "Only this part of the source Part Studio (default: whole element)",
                },
                "useLatestVersion": {
                    "type": "boolean",
                    "description": "For workspace links of other documents: link the latest released "
                    "version (default true)",
                    "default": True,
                },
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
]

_NAMES = {t.name for t in TOOLS}


async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    sc = _standard_content(ctx)
    try:
        if name == "search_standard_parts":
            hits = await sc.search(
                args.get("query", ""), args.get("standard"), int(args.get("limit", 30))
            )
            if not hits:
                return text("No standard parts found. Try fewer or other words (English names).")
            lines = [f"{len(hits)} standard part(s):"]
            lines += [f"  - {h.component}  [{h.standard} / {h.category}]  id: {h.id}" for h in hits]
            return text("\n".join(lines))

        if name == "get_standard_part_options":
            comp, _, res = await sc.resolve(args["component"], args.get("options") or {})
            lines = [f"**{comp.component}** ({comp.standard})", "Current selection:"]
            lines += [f"  {k}: {v}" for k, v in res.display.items()]
            lines.append("Available options (given the selection):")
            for k, values in res.options.items():
                shown = ", ".join(values[:80]) + (" …" if len(values) > 80 else "")
                lines.append(f"  {k}: {shown}")
            return text("\n".join(lines))

        if name == "insert_standard_part":
            return await _insert_standard(args, ctx, sc)

        if name == "insert_part_from_document":
            return await _insert_from_document(args, ctx)
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except (ValueError, RuntimeError, TimeoutError) as e:
        return text(f"{name} failed: {e}")
    return None


def _standard_content(ctx) -> StandardContentManager:
    sc = getattr(ctx, "_standard_content", None)
    if sc is None:
        sc = StandardContentManager(ctx.client)
        ctx._standard_content = sc
    return sc


async def _instances(ctx, did: str, wid: str, eid: str) -> List[Dict[str, Any]]:
    asm = await ctx.client.get(f"/api/assemblies/d/{did}/w/{wid}/e/{eid}")
    return asm.get("rootAssembly", {}).get("instances", [])


async def _insert_n(ctx, args: Dict[str, Any], body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Insert ``count`` instances and return the new instance records."""
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    count = max(1, int(args.get("count") or 1))
    before = {i["id"] for i in await _instances(ctx, did, wid, eid)}
    for _ in range(count):
        await ctx.client.post(f"/api/assemblies/d/{did}/w/{wid}/e/{eid}/instances", data=body)
    new = [i for i in await _instances(ctx, did, wid, eid) if i["id"] not in before]
    position = args.get("position")
    if position and new:
        x, y, z = (list(position) + [0, 0, 0])[:3]
        matrix = build_absolute_translation_matrix(x, y, z)
        await ctx.assembly_manager.transform_occurrences(
            did,
            wid,
            eid,
            [{"path": [i["id"]], "transform": matrix} for i in new],
            is_relative=False,
        )
    return new


def _describe(new: List[Dict[str, Any]]) -> str:
    return "\n".join(f"  - {i.get('name')}  (instanceId: {i['id']})" for i in new)


async def _insert_standard(args, ctx, sc: StandardContentManager) -> list:
    comp, src, res = await sc.resolve(args["component"], args.get("options") or {})
    body = {
        "documentId": src.document_id,
        "versionId": src.version_id,
        "elementId": src.element_id,
        "partId": src.part_id,
        "isAssembly": False,
        "isWholePartStudio": False,
        "configuration": res.configuration_string(),
    }
    new = await _insert_n(ctx, args, body)
    chosen = ", ".join(f"{k} {v}" for k, v in res.display.items() if v)
    return text(
        f"Inserted {len(new)} x {comp.component} ({chosen}):\n{_describe(new)}\n"
        "Next: position them with mates (e.g. create_fastened_mate / cylindrical mate on the hole)."
    )


async def _insert_from_document(args, ctx) -> list:
    did, wid = args["documentId"], args["workspaceId"]
    if args.get("sourceUrl"):
        ref = parse_onshape_url(args["sourceUrl"])
        s_did, s_wvm, s_wvm_id, s_eid = ref.document_id, ref.wvm, ref.wvm_id, ref.element_id
    else:
        s_did = args.get("sourceDocumentId") or did
        s_eid = args.get("sourceElementId")
        if args.get("sourceVersionId"):
            s_wvm, s_wvm_id = "v", args["sourceVersionId"]
        else:
            s_wvm, s_wvm_id = "w", args.get("sourceWorkspaceId") or (wid if s_did == did else None)
    if not s_eid:
        raise ValueError("Source element missing: give sourceUrl with /e/<id> or sourceElementId.")
    if s_wvm in (None, "m") or not s_wvm_id:
        s_wvm, s_wvm_id = "w", await ctx.document_manager.get_default_workspace_id(s_did)

    info = await ctx.transfer_manager.get_element_info(s_did, s_wvm, s_wvm_id, s_eid)
    etype = info.get("elementType")
    if etype not in ("PARTSTUDIO", "ASSEMBLY"):
        raise ValueError(
            f"'{info.get('name')}' is a {etype}; only Part Studios and Assemblies can be inserted."
        )

    def body_for(
        doc: str, element: str, version: Optional[str], is_assembly: bool
    ) -> Dict[str, Any]:
        b: Dict[str, Any] = {"documentId": doc, "elementId": element, "isAssembly": is_assembly}
        if version:
            b["versionId"] = version
        if not is_assembly:
            if args.get("partId"):
                b.update(partId=args["partId"], isWholePartStudio=False)
            else:
                b["isWholePartStudio"] = True
        return b

    note = ""
    if s_did == did and s_wvm == "w":
        body = body_for(did, s_eid, None, etype == "ASSEMBLY")
    else:
        version = s_wvm_id if s_wvm == "v" else None
        if version is None and args.get("useLatestVersion", True):
            try:
                versions = await ctx.client.get(f"/api/documents/d/{s_did}/versions")
                version = versions[-1]["id"] if versions else None
            except httpx.HTTPStatusError:
                version = None
        if version:
            body = body_for(s_did, s_eid, version, etype == "ASSEMBLY")
            note = "Linked to the source document's released version (updates when you update the version)."
        else:
            # No linkable version -> copy geometry into this document first
            result = await ctx.transfer_manager.transfer_element(
                s_did, s_wvm, s_wvm_id, s_eid, did, wid
            )
            new_elements = result["resultElementIds"]
            if not new_elements:
                raise RuntimeError("Import produced no elements")
            elements = await ctx.client.get(f"/api/documents/d/{did}/w/{wid}/elements")
            by_id = {e["id"]: e for e in elements}
            chosen = next(
                (by_id[e] for e in new_elements if by_id.get(e, {}).get("elementType") == etype),
                by_id.get(new_elements[0]),
            )
            if args.get("partId"):
                note = "Note: partId refers to the source; after import the whole element was inserted. "
            args = {**args, "partId": None}
            body = body_for(did, chosen["id"], None, chosen["elementType"] == "ASSEMBLY")
            note += (
                f"The source cannot be linked (no released version visible to you), so its geometry was "
                f"imported into this document as '{chosen.get('name')}' and inserted from there."
            )
    new = await _insert_n(ctx, args, body)
    return text(
        f"Inserted {len(new)} instance(s) of '{info.get('name')}':\n{_describe(new)}\n{note}"
    )
