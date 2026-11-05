"""Tools for inspecting, copying and importing other people's (or your own) Onshape work."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from mcp.types import Tool

from ..api.urls import parse_onshape_url
from . import text

_SOURCE_PROPS: Dict[str, Any] = {
    "url": {
        "type": "string",
        "description": "Onshape link, e.g. https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>. "
        "Alternative to documentId/workspaceId/elementId.",
    },
    "documentId": {"type": "string", "description": "Source document ID (if no url)"},
    "workspaceId": {"type": "string", "description": "Source workspace ID (optional)"},
    "versionId": {"type": "string", "description": "Source version ID (optional)"},
}

TOOLS: List[Tool] = [
    Tool(
        name="inspect_onshape_link",
        description=(
            "Open an Onshape link (or document ID) and show what it contains: document name, "
            "owner, what you are allowed to do (copy / export) and all elements (Part Studios, "
            "Assemblies, uploaded files) with their IDs. Use this first when the user shares a link."
        ),
        inputSchema={"type": "object", "properties": dict(_SOURCE_PROPS)},
    ),
    Tool(
        name="copy_document",
        description=(
            "Make a full copy of an Onshape document (with complete feature history, sketches, "
            "variables, assemblies and mates) in your account. Requires that the owner allows "
            "copying. If that fails with 'no permission', use import_from_document instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "newName": {
                    "type": "string",
                    "description": "Name of the copy (default: '<name> (Kopie)')",
                },
                "isPublic": {
                    "type": "boolean",
                    "description": "Make the copy public. Omit to use the server setting (default private).",
                },
            },
        },
    ),
    Tool(
        name="copy_element",
        description=(
            "Copy a single Part Studio / Assembly / Variable Studio (with feature history) from "
            "a source document into one of YOUR documents. Requires copy permission on the "
            "source; otherwise use import_from_document."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "elementId": {"type": "string", "description": "Source element ID (if not in url)"},
                "targetDocumentId": {"type": "string", "description": "Your target document ID"},
                "targetWorkspaceId": {
                    "type": "string",
                    "description": "Target workspace (default: main workspace)",
                },
            },
            "required": ["targetDocumentId"],
        },
    ),
    Tool(
        name="import_from_document",
        description=(
            "Copy GEOMETRY from any document you can view and export (also when copying is not "
            "allowed): exports Part Studios (Parasolid) / Assemblies (STEP, keeps structure) / "
            "uploaded files and imports them into your document. Parts keep their names but "
            "lose the feature history (sketches/extrudes); you can still add new features on "
            "top. Without targetDocumentId a new document is created."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_SOURCE_PROPS,
                "elementIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Elements to import (default: the element in the url)",
                },
                "targetDocumentId": {"type": "string", "description": "Your target document ID"},
                "targetWorkspaceId": {"type": "string", "description": "Target workspace"},
                "newDocumentName": {
                    "type": "string",
                    "description": "Name for a new target document (if no targetDocumentId)",
                },
                "format": {
                    "type": "string",
                    "enum": ["PARASOLID", "STEP"],
                    "description": "Override transfer format (default: Parasolid for Part "
                    "Studios, STEP for Assemblies)",
                },
            },
        },
    ),
    Tool(
        name="import_cad_file",
        description=(
            "Import a local CAD file (STEP, STP, IGES, Parasolid x_t/x_b, STL, OBJ, SLDPRT, ...) "
            "into one of your documents, e.g. a servo or motor model downloaded from the "
            "manufacturer. Creates a new Part Studio (and Assembly for multi-part files)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "filePath": {"type": "string", "description": "Absolute path to the local file"},
                "documentId": {"type": "string", "description": "Your target document ID"},
                "workspaceId": {"type": "string", "description": "Target workspace (optional)"},
                "flattenAssemblies": {
                    "type": "boolean",
                    "description": "Import everything into one Part Studio without an assembly",
                    "default": False,
                },
            },
            "required": ["filePath", "documentId"],
        },
    ),
]

_NAMES = {t.name for t in TOOLS}


async def _resolve_source(
    args: Dict[str, Any], ctx, need_element: bool = False
) -> Tuple[str, str, str, Optional[str]]:
    """Return (document_id, wvm, wvm_id, element_id) from url or explicit IDs."""
    element_id = args.get("elementId")
    if args.get("url"):
        ref = parse_onshape_url(args["url"])
        did, wvm, wvm_id = ref.document_id, ref.wvm, ref.wvm_id
        element_id = element_id or ref.element_id
    elif args.get("documentId"):
        did = args["documentId"]
        if args.get("versionId"):
            wvm, wvm_id = "v", args["versionId"]
        else:
            wvm, wvm_id = "w", args.get("workspaceId")
    else:
        raise ValueError("Provide either 'url' or 'documentId'.")
    if wvm is None or wvm_id is None or wvm == "m":
        wvm, wvm_id = "w", await ctx.document_manager.get_default_workspace_id(did)
    if need_element and not element_id:
        raise ValueError("No element given: pass elementId or a link that contains /e/<elementId>.")
    return did, wvm, wvm_id, element_id


def _http_error(action: str, e: httpx.HTTPStatusError) -> list:
    code = e.response.status_code
    if code in (403, 404):
        return text(
            f"{action} failed: no permission (HTTP {code}). The owner does not allow this "
            "for your account (or the document does not exist). If the document can be viewed "
            "and exported, use `import_from_document` to copy its geometry instead."
        )
    return text(f"{action} failed: Onshape API returned HTTP {code}: {e.response.text[:300]}")


async def _target_workspace(ctx, document_id: str, workspace_id: Optional[str]) -> str:
    return workspace_id or await ctx.document_manager.get_default_workspace_id(document_id)


async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    try:
        if name == "inspect_onshape_link":
            return await _inspect(args, ctx)
        if name == "copy_document":
            return await _copy_document(args, ctx)
        if name == "copy_element":
            return await _copy_element(args, ctx)
        if name == "import_from_document":
            return await _import_from_document(args, ctx)
        if name == "import_cad_file":
            return await _import_cad_file(args, ctx)
    except httpx.HTTPStatusError as e:
        return _http_error(name, e)
    except (ValueError, RuntimeError, TimeoutError, FileNotFoundError) as e:
        return text(f"{name} failed: {e}")
    return None


async def _inspect(args, ctx) -> list:
    did, wvm, wvm_id, element_id = await _resolve_source(args, ctx)
    doc = await ctx.client.get(f"/api/documents/{did}")
    perms = set(doc.get("permissionSet") or [])
    owner_perm = doc.get("permission") in ("OWNER", "FULL", "WRITE")
    can_copy = owner_perm or "COPY" in perms or doc.get("public", False) and "COPY" in perms
    can_export = owner_perm or "EXPORT" in perms
    elements = await ctx.client.get(f"/api/documents/d/{did}/{wvm}/{wvm_id}/elements")

    lines = [
        f"**{doc.get('name')}**  (owner: {doc.get('owner', {}).get('name', '?')})",
        f"documentId: {did}   {'workspaceId' if wvm == 'w' else 'versionId'}: {wvm_id}",
        f"Your access: {doc.get('permission')}  permissions: {', '.join(sorted(perms)) or '-'}",
        f"Full copy with history (copy_document / copy_element): {'YES' if can_copy else 'NO'}",
        f"Geometry copy via export (import_from_document): {'YES' if can_export else 'NO'}",
        "",
        "Elements:",
    ]
    order = {"ASSEMBLY": 0, "PARTSTUDIO": 1, "BLOB": 2, "VARIABLESTUDIO": 3}
    for e in sorted(
        elements, key=lambda e: (order.get(e.get("elementType"), 9), e.get("name", ""))
    ):
        if e.get("elementType") == "BILLOFMATERIALS":
            continue
        mark = "  <- from link" if e.get("id") == element_id else ""
        lines.append(
            f"  - [{e.get('elementType')}] {e.get('name')}  (elementId: {e.get('id')}){mark}"
        )
    return text("\n".join(lines))


async def _copy_document(args, ctx) -> list:
    did, wvm, wvm_id, _ = await _resolve_source(args, ctx)
    if wvm != "w":
        raise ValueError("Only workspaces can be copied as documents; use a workspace link.")
    result = await ctx.document_manager.copy_document(
        did, wvm_id, new_name=args.get("newName"), is_public=args.get("isPublic")
    )
    return text(
        f"Copied document as '{result['name']}' ({'public' if result['public'] else 'private'}).\n"
        f"New documentId: {result['newDocumentId']}\nNew workspaceId: {result['newWorkspaceId']}\n"
        f"Open: https://cad.onshape.com/documents/{result['newDocumentId']}"
        f"/w/{result['newWorkspaceId']}"
    )


async def _copy_element(args, ctx) -> list:
    did, wvm, wvm_id, element_id = await _resolve_source(args, ctx, need_element=True)
    target_ws = await _target_workspace(
        ctx, args["targetDocumentId"], args.get("targetWorkspaceId")
    )
    result = await ctx.document_manager.copy_element(
        did,
        element_id,
        args["targetDocumentId"],
        target_ws,
        source_workspace_id=wvm_id if wvm == "w" else None,
        source_version_id=wvm_id if wvm == "v" else None,
    )
    new_id = result.get("newElementId") or result.get("id") or result
    return text(f"Copied element into document {args['targetDocumentId']}. New element: {new_id}")


async def _import_from_document(args, ctx) -> list:
    did, wvm, wvm_id, element_id = await _resolve_source(args, ctx)
    element_ids = args.get("elementIds") or ([element_id] if element_id else [])
    if not element_ids:
        raise ValueError(
            "Which elements? Pass elementIds (see inspect_onshape_link) or a link with /e/<id>."
        )

    target_did = args.get("targetDocumentId")
    created = ""
    if not target_did:
        source_doc = await ctx.client.get(f"/api/documents/{did}")
        doc = await ctx.document_manager.create_document(
            name=args.get("newDocumentName") or f"{source_doc.get('name', 'Import')} (Import)"
        )
        target_did = doc.id
        created = f"Created new document '{doc.name}' ({target_did}).\n"
    target_ws = await _target_workspace(ctx, target_did, args.get("targetWorkspaceId"))

    lines = [created] if created else []
    for eid in element_ids:
        try:
            r = await ctx.transfer_manager.transfer_element(
                did, wvm, wvm_id, eid, target_did, target_ws, format_name=args.get("format")
            )
            lines.append(
                f"OK  {r['sourceName']} [{r['sourceType']}] via {r['format']} "
                f"({r['bytes'] / 1024:.0f} kB) -> new elements: {', '.join(r['resultElementIds'])}"
            )
        except httpx.HTTPStatusError as e:
            lines.append(f"ERR {eid}: HTTP {e.response.status_code} {e.response.text[:150]}")
        except (ValueError, RuntimeError, TimeoutError) as e:
            lines.append(f"ERR {eid}: {e}")
    lines.append(f"\nTarget: https://cad.onshape.com/documents/{target_did}/w/{target_ws}")
    lines.append("Note: imported geometry has no feature history; add new features on top of it.")
    return text("\n".join(lines))


async def _import_cad_file(args, ctx) -> list:
    path = os.path.expanduser(args["filePath"])
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    ws = await _target_workspace(ctx, args["documentId"], args.get("workspaceId"))
    status = await ctx.transfer_manager.import_local_file(
        args["documentId"], ws, path, flatten_assemblies=bool(args.get("flattenAssemblies"))
    )
    ids = status.get("resultElementIds") or []
    return text(
        f"Imported '{os.path.basename(path)}' into document {args['documentId']}.\n"
        f"New elements: {', '.join(ids) or '(none reported)'}"
    )
