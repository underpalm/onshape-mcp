"""Materials and mass properties (mass, centre of mass, inertia) - the basis for simulation."""

from __future__ import annotations

from urllib.parse import quote

import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from mcp.types import Tool

from ..standard_content import CACHE_DIR
from ..units import from_m, get_length_unit
from . import text

MATERIAL_PROPERTY_ID = "57f3fb8efa3416c06701d615"
# Onshape's public material library (document / version / element)
LIBRARY_REF = {
    "documentId": "2718281828459eacfeeda11f",
    "versionId": "7e0317fdf97739c9457998f0",
    "elementId": "6bbab304a1f64e7d640a2d7d",
}

# German / common names -> Onshape library names
ALIASES = {
    "stahl": "Steel",
    "baustahl": "Steel ASTM A36",
    "edelstahl": "Stainless Steel 304",
    "v2a": "Stainless Steel 304",
    "v4a": "Stainless Steel 316",
    "aluminium": "Aluminum - 6061",
    "alu": "Aluminum - 6061",
    "aluminum": "Aluminum - 6061",
    "messing": "Brass",
    "kupfer": "Copper",
    "bronze": "Bronze (8-14% Sn)",
    "nylon": "Nylon ",
    "pa": "Nylon ",
    "pa12": "Nylon ",
    "pom": "Acetal (Delrin)",
    "delrin": "Acetal (Delrin)",
    "acryl": "Acrylic",
    "plexiglas": "Acrylic",
    "pc": "Polycarbonate",
    "silikon": "Silicone Rubber",
    "gummi": "Silicone Rubber",
    "carbon": "Carbon fiber epoxy (61%)",
    "cfk": "Carbon fiber epoxy (61%)",
}

_PART_STUDIO = {
    "documentId": {"type": "string"},
    "workspaceId": {"type": "string"},
    "elementId": {"type": "string", "description": "Part Studio element ID"},
}

TOOLS: List[Tool] = [
    Tool(
        name="list_materials",
        description=(
            "Search Onshape's material library (189 materials: PLA, PETG, ABS, Nylon, aluminium "
            "alloys, steels, brass, ...) and show their densities. German names work too "
            "(Stahl, Edelstahl, Aluminium, Messing)."
        ),
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
    Tool(
        name="set_part_material",
        description=(
            "Assign a material to parts so they get mass and inertia (needed for correct "
            "simulation / URDF / MJCF). Optional 'density' (kg/m³) overrides the library value, e.g. "
            "for 3D printed parts with infill (PLA 100% = 1250; ~20% infill often ≈ 500-700 "
            "depending on walls) or use 'infillPercent' + 'shellFraction' for an estimate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                **_PART_STUDIO,
                "partIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Part IDs (default: all parts of the Part Studio)",
                },
                "material": {
                    "type": "string",
                    "description": "Material name, e.g. 'PLA', 'Aluminium'",
                },
                "density": {"type": "number", "description": "Override density in kg/m³"},
                "infillPercent": {
                    "type": "number",
                    "description": "3D print infill in % (estimates an effective density)",
                },
                "shellFraction": {
                    "type": "number",
                    "description": "Volume share of solid walls/top/bottom for the infill estimate "
                    "(0-1, default 0.35 for typical small parts)",
                },
            },
            "required": ["documentId", "workspaceId", "elementId", "material"],
        },
    ),
    Tool(
        name="get_mass_properties",
        description=(
            "Mass, volume, centre of mass and inertia tensor of every part in a Part Studio (or of "
            "a whole Assembly). Warns about parts without material. Inertia is about the centre "
            "of mass in world axes, in kg·m² (SI, as used by URDF/MJCF)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "documentId": {"type": "string"},
                "workspaceId": {"type": "string"},
                "elementId": {
                    "type": "string",
                    "description": "Part Studio or Assembly element ID",
                },
                "isAssembly": {"type": "boolean", "default": False},
            },
            "required": ["documentId", "workspaceId", "elementId"],
        },
    ),
]

_NAMES = {t.name for t in TOOLS}


class MaterialLibrary:
    def __init__(self, client):
        self.client = client
        self._lib: Optional[Dict[str, Any]] = None

    async def load(self) -> Dict[str, Any]:
        if self._lib is not None:
            return self._lib
        path = os.path.join(CACHE_DIR, "material_library.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 24 * 3600:
            with open(path) as fh:
                self._lib = json.load(fh)
                return self._lib
        r = LIBRARY_REF
        self._lib = await self.client.get(
            f"/api/materials/libraries/d/{r['documentId']}/v/{r['versionId']}/e/{r['elementId']}"
        )
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self._lib, fh)
        return self._lib

    async def search(self, query: str = "") -> List[Dict[str, Any]]:
        lib = await self.load()
        q = query.strip().lower()
        if q in ALIASES:
            q = ALIASES[q].lower().strip()
        return [m for m in lib["materials"] if not q or q in m["displayName"].lower()]

    async def find(self, name: str) -> Dict[str, Any]:
        lib = await self.load()
        target = ALIASES.get(name.strip().lower(), name).strip().lower()
        for m in lib["materials"]:
            if m["displayName"].strip().lower() == target or m["id"].strip().lower() == target:
                return m
        hits = [m for m in lib["materials"] if target in m["displayName"].lower()]
        if len(hits) == 1:
            return hits[0]
        options = ", ".join(m["displayName"].strip() for m in hits[:15]) or "none"
        raise ValueError(f"Material '{name}' not found uniquely. Candidates: {options}")

    async def material_value(
        self, material: Dict[str, Any], density: Optional[float]
    ) -> Dict[str, Any]:
        lib = await self.load()
        defs = {d["name"]: d for d in lib["propertyDefinitions"]}
        values = dict(material.get("propertyValues", {}))
        if density is not None:
            values["DENS"] = f"{density:g}"
        props = []
        for key, value in values.items():
            d = defs.get(key, {"name": key, "type": "REAL", "units": "", "displayName": key})
            props.append(
                {
                    "name": key,
                    "value": str(value),
                    "type": d.get("type", "REAL"),
                    "units": d.get("units", ""),
                    "displayName": d.get("displayName", key),
                    "category": d.get("category", ""),
                    "description": d.get("description", ""),
                }
            )
        name = material["displayName"].strip()
        if density is not None:
            name = f"{name} ({density:g} kg/m³)"
        value: Dict[str, Any] = {
            "id": material["id"] if density is None else name,
            "displayName": name,
            "libraryName": lib.get("name", "Onshape Material Library"),
            "properties": props,
        }
        return value


def _library(ctx) -> MaterialLibrary:
    lib = getattr(ctx, "_materials", None)
    if lib is None:
        lib = MaterialLibrary(ctx.client)
        ctx._materials = lib
    return lib


def effective_density(
    full_density: float, infill_percent: float, shell_fraction: float = 0.35
) -> float:
    """Rough effective density of an FDM print: solid shells + partially filled core."""
    infill = max(0.0, min(100.0, infill_percent)) / 100.0
    shell = max(0.0, min(1.0, shell_fraction))
    return full_density * (shell + (1.0 - shell) * infill)


async def handle(name: str, args: Dict[str, Any], ctx) -> Optional[list]:
    if name not in _NAMES:
        return None
    lib = _library(ctx)
    try:
        if name == "list_materials":
            hits = await lib.search(args.get("query", ""))
            if not hits:
                return text("No material found.")
            lines = [f"{len(hits)} material(s) (density kg/m³):"]
            for m in hits[:80]:
                lines.append(
                    f"  - {m['displayName'].strip()}  [{m.get('category', '')}]  "
                    f"{m.get('propertyValues', {}).get('DENS', '?')}"
                )
            return text("\n".join(lines))

        if name == "set_part_material":
            return await _set_material(args, ctx, lib)

        if name == "get_mass_properties":
            return await _mass_properties(args, ctx)
    except httpx.HTTPStatusError as e:
        return text(f"{name} failed: HTTP {e.response.status_code}: {e.response.text[:300]}")
    except (ValueError, KeyError) as e:
        return text(f"{name} failed: {e}")
    return None


async def _set_material(args, ctx, lib: MaterialLibrary) -> list:
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    material = await lib.find(args["material"])
    density = args.get("density")
    note = ""
    if density is None and args.get("infillPercent") is not None:
        base = float(material.get("propertyValues", {}).get("DENS", 0))
        density = round(
            effective_density(
                base, float(args["infillPercent"]), float(args.get("shellFraction", 0.35))
            )
        )
        note = f" (estimated for {args['infillPercent']:g}% infill from {base:g} kg/m³)"
    value = await lib.material_value(material, density)

    part_ids = args.get("partIds")
    if not part_ids:
        parts = await ctx.client.get(f"/api/parts/d/{did}/w/{wid}/e/{eid}")
        part_ids = [p["partId"] for p in parts if p.get("bodyType", "solid") == "solid"]
    done, failed = [], []
    for pid in part_ids:
        r = await ctx.client.post(
            f"/api/metadata/d/{did}/w/{wid}/e/{eid}/p/{quote(pid, safe='')}",
            data={"properties": [{"propertyId": MATERIAL_PROPERTY_ID, "value": value}]},
        )
        (done if r.get("status") == "SUCCEEDED" else failed).append(pid)
    dens = value_density(value)
    msg = f"Material '{value['displayName']}' ({dens} kg/m³){note} set on {len(done)} part(s): {', '.join(done)}"
    if failed:
        msg += f"\nFailed: {', '.join(failed)}"
    return text(msg)


def value_density(value: Dict[str, Any]) -> str:
    for p in value.get("properties", []):
        if p.get("name") == "DENS":
            return p.get("value", "?")
    return "?"


async def _mass_properties(args, ctx) -> list:
    did, wid, eid = args["documentId"], args["workspaceId"], args["elementId"]
    u = get_length_unit()
    if args.get("isAssembly"):
        mp = await ctx.client.get(f"/api/assemblies/d/{did}/w/{wid}/e/{eid}/massproperties")
        bodies = {"Assembly": mp}
    else:
        mp = await ctx.client.get(
            f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/massproperties",
            params={"massAsGroup": "false"},
        )
        bodies = mp.get("bodies", {})
        names = {
            p["partId"]: p.get("name", p["partId"])
            for p in await ctx.client.get(f"/api/parts/d/{did}/w/{wid}/e/{eid}")
        }
    lines: List[str] = []
    missing: List[str] = []
    total = 0.0
    for key, b in bodies.items():
        label = key if args.get("isAssembly") else f"{names.get(key, key)} ({key})"
        volume = b.get("volume", [0])[0]
        if not b.get("hasMass", False):
            missing.append(label)
            lines.append(
                f"**{label}**: NO MATERIAL - volume {from_m(from_m(from_m(volume))):.1f} {u}³, mass unknown"
            )
            continue
        mass = b["mass"][0]
        total += mass
        com = b.get("centroid", [0, 0, 0])[:3]
        inertia = b.get("inertia", [0] * 9)[:9]
        lines.append(
            f"**{label}**: mass {mass * 1000:.3f} g, volume {from_m(from_m(from_m(volume))):.1f} {u}³\n"
            f"  centre of mass ({u}): ({', '.join(f'{from_m(c):.3f}' for c in com)})\n"
            f"  inertia at COM (kg·m²): Ixx={inertia[0]:.4e} Iyy={inertia[4]:.4e} Izz={inertia[8]:.4e} "
            f"Ixy={inertia[1]:.4e} Ixz={inertia[2]:.4e} Iyz={inertia[5]:.4e}"
        )
    if not args.get("isAssembly") and len(bodies) > 1:
        lines.append(f"\nTotal mass of parts with material: {total * 1000:.3f} g")
    if missing:
        lines.append(
            f"\nWARNING: {len(missing)} part(s) have no material, so their mass/inertia are missing. "
            "Use set_part_material before exporting for simulation."
        )
    return text("\n".join(lines) or "No parts found.")
