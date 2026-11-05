"""3D printing and CAD exports: STL (per part, mm), 3MF, STEP/Parasolid/OBJ/GLB - saved locally."""

from __future__ import annotations

from urllib.parse import quote

import math
import re
import struct
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import httpx
from mcp.types import Tool

from ..exports import export_dir, safe_name, save_bytes
from . import text

# chord tolerance (m) / angle tolerance (deg); "medium" = Onshape defaults
QUALITY = {
    "coarse": (0.00025, 15.0),
    "medium": (None, None),
    "fine": (0.0001, 10.0),  # 0.1 mm / 10°: well below FDM resolution, sane file sizes
}

_PS = {
    "documentId": {"type": "string"},
    "workspaceId": {"type": "string"},
    "elementId": {"type": "string", "description": "Part Studio element ID"},
}
_QUALITY_PROPS = {
    "quality": {"type": "string", "enum": list(QUALITY), "default": "medium"},
    "chordTolerance": {
        "type": "number",
        "description": "Max. surface deviation in mm (overrides quality)",
    },
    "angleTolerance": {"type": "number", "description": "Max. angle between facets in degrees"},
}
_OUT = {
    "directory": {
        "type": "string",
        "description": "Target folder (default ~/onshape-exports/<name>)",
    },
}

TOOLS: List[Tool] = [
    Tool(
        name="export_stl",
        description=(
            "Export parts of a Part Studio as binary STL in MILLIMETRES for 3D printing - one file per "
            "part (named after the part) or all parts in one file. Reports triangle count and size "
            "of each file and optionally checks that it fits the printer bed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PS,
                "partIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Default: all parts",
                },
                "singleFile": {"type": "boolean", "default": False},
                **_QUALITY_PROPS,
                "bedSize": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Printer build volume [x, y, z] in mm, e.g. [256, 256, 256]",
                },
                **_OUT,
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
    Tool(
        name="export_assembly_for_printing",
        description=(
            "Export every unique part of an assembly that has to be PRINTED as its own STL (mm), "
            "with the quantity in the file name, e.g. 'Arm (x2).stl'. Purchased items are skipped and "
            "returned as a shopping list: Onshape standard parts (screws, nuts, ...), parts with a "
            "Vendor set in Onshape (see mark_purchased_parts) and instances/sub-assemblies matching "
            "'exclude' name patterns, e.g. ['SCS215*', 'Raspberry*']."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "documentId": {"type": "string"},
                "workspaceId": {"type": "string"},
                "elementId": {"type": "string", "description": "Assembly element ID"},
                "includeStandardParts": {"type": "boolean", "default": False},
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Name patterns (wildcards) of purchased parts / sub-assemblies",
                },
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Name patterns of helper geometry to leave out completely",
                },
                "skipPurchased": {
                    "type": "boolean",
                    "description": "Skip parts with a Vendor in Onshape (default true)",
                    "default": True,
                },
                **_QUALITY_PROPS,
                "bedSize": {"type": "array", "items": {"type": "number"}},
                **_OUT,
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
    Tool(
        name="export_model",
        description=(
            "Export a Part Studio or Assembly as 3MF (3D printing with units), STEP, PARASOLID, IGES, "
            "OBJ, GLB/GLTF and save it locally. STEP keeps assembly structure; 3MF is preferred by "
            "modern slicers (PrusaSlicer, Bambu Studio, Cura)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "documentId": {"type": "string"},
                "workspaceId": {"type": "string"},
                "elementId": {"type": "string"},
                "format": {
                    "type": "string",
                    "enum": ["3MF", "STEP", "PARASOLID", "IGES", "OBJ", "GLB", "GLTF"],
                    "default": "3MF",
                },
                "partIds": {"type": "array", "items": {"type": "string"}},
                "resolution": {
                    "type": "string",
                    "enum": ["coarse", "medium", "fine"],
                    "default": "fine",
                },
                "fileName": {"type": "string"},
                **_OUT,
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
]

TOOLS.append(
    Tool(
        name="mark_purchased_parts",
        description=(
            "Mark parts of a Part Studio as purchased (bought, not printed/made) by setting the "
            "Onshape properties Vendor (and optionally Part number), e.g. all parts of a servo: "
            "vendor 'Feetech', partNumber 'STS3215'. export_assembly_for_printing then skips them "
            "and lists them in the shopping list; the info also appears in Onshape's BOM."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PS,
                "partIds": {"type": "array", "items": {"type": "string"}},
                "vendor": {"type": "string"},
                "partNumber": {"type": "string"},
            },
            "required": ["documentId", "workspaceId", "elementId", "partIds", "vendor"],
        },
    )
)

_NAMES = {t.name for t in TOOLS}


# --------------------------------------------------------------------------- STL helpers
def stl_stats(data: bytes) -> Tuple[int, List[float], List[float]]:
    """Return (triangles, min_xyz, max_xyz) of a binary or ASCII STL."""
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if len(data) == 84 + 50 * n:
            mn, mx = [math.inf] * 3, [-math.inf] * 3
            for i in range(n):
                off = 84 + i * 50 + 12
                for k in range(3):
                    v = struct.unpack_from("<3f", data, off + 12 * k)
                    for j in range(3):
                        mn[j] = min(mn[j], v[j])
                        mx[j] = max(mx[j], v[j])
            return n, mn, mx
    verts = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    if not verts:
        return 0, [0.0] * 3, [0.0] * 3
    pts = [tuple(float(c) for c in v) for v in verts]
    mn = [min(p[i] for p in pts) for i in range(3)]
    mx = [max(p[i] for p in pts) for i in range(3)]
    return len(pts) // 3, mn, mx


_3MF_UNITS = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}


def normalize_3mf_to_mm(data: bytes) -> bytes:
    """Rewrite a 3MF so its model unit is millimetres (scales vertices and transforms).

    Onshape writes 3MF in metres; formally valid, but some slicers ignore the unit attribute
    and would show the part 1000x too small.
    """
    import io
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(data))
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename.lower().endswith(".model"):
                xml = content.decode("utf-8")
                m = re.search(r'<model\b[^>]*\bunit="([a-z]+)"', xml)
                factor = _3MF_UNITS.get(m.group(1), 1.0) if m else 1.0  # 3MF default is mm
                if factor != 1.0:

                    def scale(match):
                        return f'{match.group(1)}="{float(match.group(2)) * factor:.6g}"'

                    xml = re.sub(r'\b([xyz])="([-+0-9.eE]+)"', scale, xml)

                    def scale_transform(match):
                        vals = match.group(1).split()
                        if len(vals) == 12:
                            vals[9:12] = [f"{float(v) * factor:.6g}" for v in vals[9:12]]
                        return f'transform="{" ".join(vals)}"'

                    xml = re.sub(r'transform="([^"]+)"', scale_transform, xml)
                    xml = re.sub(r'(<model\b[^>]*\bunit=)"[a-z]+"', r'\1"millimeter"', xml, count=1)
                content = xml.encode("utf-8")
            out.writestr(item, content)
    return out_buf.getvalue()


def fits_bed(size: List[float], bed: List[float]) -> bool:
    """True if the part fits the bed in some axis-aligned orientation."""
    return all(a <= b + 1e-6 for a, b in zip(sorted(size), sorted(bed[:3])))


def _stl_params(args: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {"units": "millimeter", "mode": "binary"}
    chord, angle = QUALITY.get(args.get("quality", "medium"), (None, None))
    if args.get("chordTolerance") is not None:
        chord = float(args["chordTolerance"]) / 1000.0
        if chord < 0.00002:
            raise ValueError("chordTolerance below 0.02 mm is not supported by Onshape")
    if args.get("angleTolerance") is not None:
        angle = float(args["angleTolerance"])
    if chord is not None:
        params["chordTolerance"] = chord
    if angle is not None:
        params["angleTolerance"] = math.radians(angle)
    return params


def _describe_file(path: str, data: bytes, bed: Optional[List[float]]) -> str:
    n, mn, mx = stl_stats(data)
    if n == 0:
        return f"  - {path}: EMPTY (export failed or tolerance too fine)"
    size = [b - a for a, b in zip(mn, mx)]
    line = f"  - {path}: {n} triangles, {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f} mm"
    if bed:
        line += "" if fits_bed(size, bed) else "  ⚠ does NOT fit the printer bed"
    return line


async def _doc_name(ctx, did: str) -> str:
    try:
        return (await ctx.client.get(f"/api/documents/{did}")).get("name", "onshape")
    except httpx.HTTPStatusError:
        return "onshape"


# --------------------------------------------------------------------------- handlers
async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    try:
        if name == "export_stl":
            return await _export_stl(args, ctx)
        if name == "export_assembly_for_printing":
            return await _export_assembly(args, ctx)
        if name == "export_model":
            return await _export_model(args, ctx)
        if name == "mark_purchased_parts":
            return await _mark_purchased(args, ctx)
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except (ValueError, RuntimeError, TimeoutError) as e:
        return text(f"{name} failed: {e}")
    return None


async def _export_stl(args, ctx) -> list:
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    params = _stl_params(args)
    parts = [
        p
        for p in await ctx.client.get(f"/api/parts/d/{did}/w/{wid}/e/{eid}")
        if p.get("bodyType", "solid") == "solid"
    ]
    if args.get("partIds"):
        wanted = set(args["partIds"])
        parts = [p for p in parts if p["partId"] in wanted]
    if not parts:
        raise ValueError("No solid parts found to export")
    folder = args.get("directory") or export_dir(None) + "/" + safe_name(await _doc_name(ctx, did))
    bed = args.get("bedSize")
    lines = []
    if args.get("singleFile") or len(parts) == 1:
        q = dict(params, partIds=",".join(p["partId"] for p in parts), grouping="true")
        data = await ctx.client.get_bytes(f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/stl", params=q)
        fname = (parts[0].get("name") if len(parts) == 1 else "parts") + ".stl"
        path = save_bytes(data, fname, folder)
        lines.append(_describe_file(path, data, bed))
    else:
        for p in parts:
            data = await ctx.client.get_bytes(
                f"/api/parts/d/{did}/w/{wid}/e/{eid}/partid/{quote(p['partId'], safe='')}/stl",
                params=params,
            )
            path = save_bytes(data, f"{p.get('name', p['partId'])}.stl", folder)
            lines.append(_describe_file(path, data, bed))
    return text(f"Exported {len(lines)} STL file(s) (binary, mm):\n" + "\n".join(lines))


VENDOR_PROPERTY_ID = "57f3fb8efa3416c06701d612"
PART_NUMBER_PROPERTY_ID = "57f3fb8efa3416c06701d60f"


def _clean(name: str) -> str:
    """'SCS215 v5 (3) <1>' -> 'SCS215 v5' (instance index and copy suffix removed)."""
    name = re.sub(r"\s*<\d+>$", "", name or "")
    return re.sub(r"\s*\(\d+\)$", "", name).strip()


def _matches(patterns: List[str], names: List[str]) -> Optional[int]:
    """Index of the first name (outermost first) that matches one of the patterns."""
    import fnmatch

    for i, n in enumerate(names):
        for p in patterns:
            if fnmatch.fnmatch(_clean(n).lower(), p.lower()) or fnmatch.fnmatch(
                n.lower(), p.lower()
            ):
                return i
    return None


async def _vendors(ctx, did, wid, keys) -> Dict[tuple, str]:
    """(documentId, elementId, partId) -> vendor, read per element in one call."""
    out: Dict[tuple, str] = {}
    by_element: Dict[tuple, str] = {}
    for pdid, peid, _pid, wvm in keys:
        by_element[(pdid, peid)] = wvm
    for (pdid, peid), wvm in by_element.items():
        params = {"linkDocumentId": did} if pdid != did else None
        try:
            md = await ctx.client.get(f"/api/metadata/d/{pdid}/{wvm}/e/{peid}/p", params=params)
        except httpx.HTTPStatusError:
            continue
        for item in md.get("items", []):
            vendor = next(
                (
                    p.get("value")
                    for p in item.get("properties", [])
                    if p.get("propertyId") == VENDOR_PROPERTY_ID
                ),
                None,
            )
            if vendor:
                out[(pdid, peid, item.get("partId"))] = str(vendor)
    return out


async def _export_assembly(args, ctx) -> list:
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    asm = await ctx.client.get(f"/api/assemblies/d/{did}/w/{wid}/e/{eid}")
    by_id: Dict[str, Dict[str, Any]] = {
        i["id"]: i for i in asm.get("rootAssembly", {}).get("instances", [])
    }
    for sub in asm.get("subAssemblies", []):
        for i in sub.get("instances", []):
            by_id.setdefault(i["id"], i)

    from .parts_tools import _standard_content

    standard_ids = {c.id for c in await _standard_content(ctx).list_components()}
    patterns = [p for p in args.get("exclude") or [] if p]
    ignore = [p for p in args.get("ignore") or [] if p]
    ignored = 0

    def wvm_of(inst) -> Optional[str]:
        if inst.get("documentId") == did:
            return f"w/{wid}"
        return f"v/{inst['documentVersion']}" if inst.get("documentVersion") else None

    leaves = []
    for occ in asm.get("rootAssembly", {}).get("occurrences", []):
        path = occ["path"]
        inst = by_id.get(path[-1])
        if not inst or inst.get("type") != "Part" or inst.get("suppressed"):
            continue
        leaves.append((path, inst))
    vendors: Dict[tuple, str] = {}
    if args.get("skipPurchased", True):
        keys = {
            (i.get("documentId"), i.get("elementId"), i.get("partId"), wvm_of(i))
            for _, i in leaves
            if wvm_of(i)
        }
        vendors = await _vendors(ctx, did, wid, keys)

    unique: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
    shopping: Dict[str, set] = {}  # name -> distinct occurrences (paths) to count
    for path, inst in leaves:
        names = [by_id[p].get("name", p) for p in path]
        vendor = vendors.get((inst.get("documentId"), inst.get("elementId"), inst.get("partId")))
        if _matches(ignore, names) is not None:  # helper geometry: neither printed nor bought
            ignored += 1
            continue
        hit = _matches(patterns, names)
        if hit is not None:  # excluded by pattern: count the matched level (e.g. the servo)
            label = _clean(names[hit])
            shopping.setdefault(label, set()).add(tuple(path[: hit + 1]))
            continue
        if vendor:  # marked as purchased in Onshape: count its sub-assembly if any
            level = 0 if len(path) > 1 else len(path) - 1
            label = _clean(names[level]) + (f" ({vendor})" if len(path) == 1 else "")
            shopping.setdefault(label, set()).add(tuple(path[: level + 1]))
            continue
        is_standard = inst.get("isStandardContent") or inst.get("documentId") in standard_ids
        if is_standard and not args.get("includeStandardParts"):
            shopping.setdefault(_clean(names[-1]), set()).add(tuple(path))
            continue
        key = (
            inst.get("documentId"),
            inst.get("elementId"),
            inst.get("partId"),
            inst.get("configuration", ""),
        )
        entry = unique.setdefault(key, {"inst": inst, "count": 0})
        entry["count"] += 1

    params = _stl_params(args)
    folder = args.get("directory") or export_dir(None) + "/" + safe_name(await _doc_name(ctx, did))
    lines = []
    for (pdid, peid, ppid, cfg), entry in unique.items():
        inst = entry["inst"]
        wvm = wvm_of(inst)
        if wvm is None:
            lines.append(f"  - {inst.get('name')}: skipped (no version of source document)")
            continue
        q = dict(params)
        if cfg and cfg != "default":
            q["configuration"] = cfg
        if pdid != did:
            q["linkDocumentId"] = did
        data = await ctx.client.get_bytes(
            f"/api/parts/d/{pdid}/{wvm}/e/{peid}/partid/{quote(ppid, safe='')}/stl", params=q
        )
        base = re.sub(r"\s*<\d+>$", "", inst.get("name", ppid))
        suffix = f" (x{entry['count']})" if entry["count"] > 1 else ""
        path = save_bytes(data, f"{base}{suffix}.stl", folder)
        lines.append(_describe_file(path, data, args.get("bedSize")))
    msg = f"Exported {len(lines)} unique part(s) to print as STL (mm) to {folder}:\n" + "\n".join(
        lines
    )
    if ignored:
        msg += f"\nIgnored {ignored} helper part(s)."
    if shopping:
        msg += "\n\nNot printed - buy these (shopping list):\n" + "\n".join(
            f"  - {len(occ)}x {name}" for name, occ in sorted(shopping.items())
        )
    return text(msg)


async def _export_model(args, ctx) -> list:
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    fmt = args.get("format", "3MF").upper()
    options: Dict[str, Any] = {}
    if fmt in ("3MF", "OBJ", "GLB", "GLTF"):
        options["resolution"] = args.get("resolution", "fine")
    if fmt in ("STEP", "IGES", "OBJ"):
        options.update(unit="millimeter", specifyUnits=True)
    data, filename = await ctx.transfer_manager.export_element(
        did,
        "w",
        wid,
        eid,
        format_name=fmt,
        part_id=",".join(args["partIds"]) if args.get("partIds") else None,
        options=options,
    )
    if fmt == "3MF" and data[:2] == b"PK":
        data = normalize_3mf_to_mm(data)
    if args.get("fileName"):
        filename = f"{args['fileName']}.{filename.rsplit('.', 1)[-1]}"
    folder = args.get("directory") or export_dir(None) + "/" + safe_name(await _doc_name(ctx, did))
    path = save_bytes(data, filename, folder)
    return text(f"Saved {fmt} ({len(data) / 1024:.0f} kB) to {path}")


async def _mark_purchased(args, ctx) -> list:
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    props = [{"propertyId": VENDOR_PROPERTY_ID, "value": args["vendor"]}]
    if args.get("partNumber"):
        props.append({"propertyId": PART_NUMBER_PROPERTY_ID, "value": args["partNumber"]})
    done, failed = [], []
    for pid in args["partIds"]:
        r = await ctx.client.post(
            f"/api/metadata/d/{did}/w/{wid}/e/{eid}/p/{quote(pid, safe='')}",
            data={"properties": props},
        )
        (done if r.get("status") == "SUCCEEDED" else failed).append(pid)
    msg = f"Marked {len(done)} part(s) as purchased from '{args['vendor']}'"
    if args.get("partNumber"):
        msg += f" (part number {args['partNumber']})"
    msg += f": {', '.join(done)}"
    if failed:
        msg += f"\nFailed: {', '.join(failed)}"
    return text(msg)
