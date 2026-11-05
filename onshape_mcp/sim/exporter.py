"""Download Onshape's URDF export and turn it into a MuJoCo package on disk."""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import zipfile
from typing import Any, Dict, Optional

from ..tools.print_tools import stl_stats
from .mjcf import _canon, build_mjcf, build_tree, parse_urdf, resolve_split_parts

URDF_OPTIONS = {
    "formatName": "URDF",
    "storeInDocument": False,
    "urdfMeshFormat": "STL",
    "urdfStlEncoding": "BINARY",
}


async def download_urdf(
    client, transfer, did: str, wid: str, eid: str, resolution: str = "medium"
) -> bytes:
    r = await client.post(
        f"/api/assemblies/d/{did}/w/{wid}/e/{eid}/translations",
        data={**URDF_OPTIONS, "resolution": resolution},
    )
    status = await transfer.wait_for_translation(r["id"], timeout=600)
    ids = status.get("resultExternalDataIds") or []
    if not ids:
        raise RuntimeError("URDF export returned no file")
    return await client.get_bytes(f"/api/documents/d/{did}/externaldata/{ids[0]}")


def _mesh_lowest_z(model, tree, mesh_dir: str) -> Optional[float]:
    lowest = None
    for name, link in model.links.items():
        if name not in tree.world or link.is_dummy:
            continue
        for v in link.visuals:
            if not v.mesh:
                continue
            path = os.path.join(mesh_dir, os.path.basename(v.mesh.split("://")[-1]))
            if not os.path.exists(path):
                continue
            with open(path, "rb") as fh:
                n, mn, mx = stl_stats(fh.read())
            if not n:
                continue
            pose = tree.world[name] * v.origin
            for cx in (mn[0], mx[0]):
                for cy in (mn[1], mx[1]):
                    for cz in (mn[2], mx[2]):
                        z = pose.point((cx * v.scale[0], cy * v.scale[1], cz * v.scale[2]))[2]
                        lowest = z if lowest is None else min(lowest, z)
    return lowest


async def split_part_hints(client, did: str, wid: str, eid: str):
    """(mate name, sub-assembly instance name, part name) for mates whose connector sits on a
    part inside a sub-assembly - Onshape's URDF export cuts such parts out of the sub-assembly."""
    asm = await client.get(
        f"/api/assemblies/d/{did}/w/{wid}/e/{eid}", params={"includeMateFeatures": "true"}
    )
    root_names = {
        i["id"]: i.get("name", "") for i in asm.get("rootAssembly", {}).get("instances", [])
    }
    sub_names = {
        i["id"]: i.get("name", "")
        for sa in asm.get("subAssemblies", [])
        for i in sa.get("instances", [])
    }
    hints = []
    for feat in asm.get("rootAssembly", {}).get("features", []):
        if feat.get("featureType") != "mate":
            continue
        data = feat.get("featureData", {})
        for ent in data.get("matedEntities", []):
            path = ent.get("matedOccurrence") or []
            if len(path) >= 2:
                part = re.sub(r"\s*<\d+>$", "", sub_names.get(path[-1], ""))
                hints.append((data.get("name", ""), root_names.get(path[0], ""), part))
    return hints


def write_package(
    zip_bytes: bytes,
    out_dir: str,
    config: Optional[Dict[str, Any]] = None,
    split_hints=None,
) -> Dict[str, Any]:
    """Extract the URDF zip and write robot.urdf, robot.xml (MJCF), meshes/, sim_config.json."""
    config = dict(config or {})
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    urdf_name = next((n for n in z.namelist() if n.endswith(".urdf")), None)
    if urdf_name is None:
        raise RuntimeError("No .urdf file in the Onshape export")
    urdf_text = z.read(urdf_name).decode("utf-8")

    os.makedirs(out_dir, exist_ok=True)
    mesh_dir = os.path.join(out_dir, "meshes")
    if os.path.isdir(mesh_dir):
        shutil.rmtree(mesh_dir)
    os.makedirs(mesh_dir)
    for name in z.namelist():
        if "/meshes/" in name and not name.endswith("/"):
            with open(os.path.join(mesh_dir, os.path.basename(name)), "wb") as fh:
                fh.write(z.read(name))

    model = parse_urdf(urdf_text)
    if split_hints:
        auto = resolve_split_parts(model, split_hints)
        config["attach"] = {**auto, **(config.get("attach") or {})}
    robot_name = _canon(config.get("name") or model.name) or "robot"
    config.setdefault("name", robot_name)
    if config.get("floor", True) and config.get("floor_height") is None:
        tree = build_tree(model, config.get("base"), config.get("attach"))
        low = _mesh_lowest_z(model, tree, mesh_dir)
        if low is not None:
            config["floor_height"] = low - 0.001

    # URDF for ROS / RViz: package:// paths -> relative mesh paths
    urdf_local = re.sub(r'filename="package://[^"]*/meshes/', 'filename="meshes/', urdf_text)
    with open(os.path.join(out_dir, f"{robot_name}.urdf"), "w") as fh:
        fh.write(urdf_local)

    xml, tree = build_mjcf(model, config, mesh_dir="meshes")
    xml_path = os.path.join(out_dir, f"{robot_name}.xml")
    with open(xml_path, "w") as fh:
        fh.write(xml)
    with open(os.path.join(out_dir, "sim_config.json"), "w") as fh:
        json.dump(config, fh, indent=2)
    return {
        "xml": xml_path,
        "urdf": os.path.join(out_dir, f"{robot_name}.urdf"),
        "tree": tree,
        "model": model,
        "config": config,
    }


# --------------------------------------------------------------------------- MuJoCo checks
def mujoco_available() -> bool:
    try:
        import mujoco  # noqa: F401

        return True
    except ImportError:
        return False


_JOINT_TYPES = {0: "free", 1: "ball", 2: "slide", 3: "hinge"}


def summarize(xml_path: str) -> str:
    """Compile the MJCF with MuJoCo and describe bodies, masses, joints and DOFs."""
    import mujoco

    m = mujoco.MjModel.from_xml_path(xml_path)
    lines = [
        f"MuJoCo {mujoco.__version__}: model compiled OK - {m.nbody - 1} bodies, {m.njnt} joints, "
        f"{m.nv} degrees of freedom, {m.nu} actuators, {m.ntendon} tendons, {m.neq} constraints",
        f"Total mass: {sum(m.body_mass) * 1000:.2f} g, timestep {m.opt.timestep} s, gravity {list(m.opt.gravity)}",
        "Bodies (mass, principal inertia kg·m²):",
    ]
    for b in range(1, m.nbody):
        name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, b)
        inertia = ", ".join(f"{v:.3e}" for v in m.body_inertia[b])
        flag = "  ⚠ no mass" if m.body_mass[b] < 1e-6 else ""
        lines.append(f"  - {name}: {m.body_mass[b] * 1000:.3f} g, I=({inertia}){flag}")
    if m.njnt:
        lines.append("Joints:")
    for j in range(m.njnt):
        name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_JOINT, j)
        typ = _JOINT_TYPES.get(int(m.jnt_type[j]), "?")
        dof = m.jnt_dofadr[j]
        parts = [f"{typ}", f"axis {tuple(round(float(a), 3) for a in m.jnt_axis[j])}"]
        if m.jnt_limited[j]:
            lo, hi = m.jnt_range[j]
            if typ == "hinge":
                parts.append(f"range [{math.degrees(lo):.1f}°, {math.degrees(hi):.1f}°]")
            else:
                parts.append(f"range [{lo * 1000:.1f}, {hi * 1000:.1f}] mm")
        else:
            parts.append("no limits")
        parts.append(
            f"damping {m.dof_damping[dof]:g}, friction {m.dof_frictionloss[dof]:g}, armature {m.dof_armature[dof]:g}, stiffness {m.jnt_stiffness[j]:g}"
        )
        lines.append(f"  - {name}: " + ", ".join(parts))
    return "\n".join(lines)


def simulate(
    xml_path: str,
    duration: float = 3.0,
    initial: Optional[Dict[str, float]] = None,
    controls: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Run the model; initial joint positions in degrees (hinge) / metres (slide)."""
    import mujoco

    m = mujoco.MjModel.from_xml_path(xml_path)
    d = mujoco.MjData(m)
    for key, value in (initial or {}).items():
        j = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, _canon(key))
        if j < 0:
            raise ValueError(f"Joint '{key}' not found")
        q = m.jnt_qposadr[j]
        d.qpos[q] = math.radians(value) if m.jnt_type[j] == 3 else value
    for key, value in (controls or {}).items():
        a = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_ACTUATOR, key)
        if a < 0:
            raise ValueError(f"Actuator '{key}' not found")
        d.ctrl[a] = value
    mujoco.mj_forward(m, d)
    hinge_joints = [j for j in range(m.njnt) if m.jnt_type[j] in (2, 3)]
    traces = {j: [] for j in hinge_joints}
    times = []
    steps = int(duration / m.opt.timestep)
    unstable = False
    for _ in range(steps):
        mujoco.mj_step(m, d)
        if not all(math.isfinite(v) for v in d.qpos):
            unstable = True
            break
        times.append(d.time)
        for j in hinge_joints:
            traces[j].append(float(d.qpos[m.jnt_qposadr[j]]))
    result: Dict[str, Any] = {"unstable": unstable, "time": d.time, "joints": {}}
    for j, trace in traces.items():
        name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_JOINT, j)
        hinge = m.jnt_type[j] == 3
        conv = math.degrees if hinge else (lambda v: v * 1000)
        result["joints"][name] = {
            "unit": "deg" if hinge else "mm",
            "start": conv(trace[0]) if trace else None,
            "end": conv(trace[-1]) if trace else None,
            "min": conv(min(trace)) if trace else None,
            "max": conv(max(trace)) if trace else None,
            "period": _period(times, trace),
        }
    return result


def _period(times, trace) -> Optional[float]:
    """Oscillation period from upward crossings of the mean value."""
    if len(trace) < 10:
        return None
    mean = sum(trace) / len(trace)
    if max(trace) - min(trace) < 0.002:  # < ~0.1° / 2 mm peak-to-peak: noise, not an oscillation
        return None
    ups = []
    for i in range(1, len(trace)):
        a, b = trace[i - 1] - mean, trace[i] - mean
        if a < 0 <= b:
            t = times[i - 1] + (times[i] - times[i - 1]) * (-a / (b - a))
            ups.append(t)
    if len(ups) < 2:
        return None
    return (ups[-1] - ups[0]) / (len(ups) - 1)


def regenerate(out_dir: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the MJCF from the URDF + meshes already in ``out_dir`` (no Onshape calls)."""
    urdfs = [f for f in os.listdir(out_dir) if f.endswith(".urdf")]
    if not urdfs:
        raise FileNotFoundError(f"No .urdf in {out_dir} - run export_mujoco first")
    with open(os.path.join(out_dir, urdfs[0])) as fh:
        model = parse_urdf(fh.read())
    config = dict(config)
    config.setdefault("name", os.path.splitext(urdfs[0])[0])
    mesh_dir = os.path.join(out_dir, "meshes")
    if config.get("floor", True) and config.get("floor_height") is None:
        low = _mesh_lowest_z(
            model, build_tree(model, config.get("base"), config.get("attach")), mesh_dir
        )
        if low is not None:
            config["floor_height"] = low - 0.001
    xml, tree = build_mjcf(model, config, mesh_dir="meshes")
    xml_path = os.path.join(out_dir, f"{_canon(config['name'])}.xml")
    with open(xml_path, "w") as fh:
        fh.write(xml)
    with open(os.path.join(out_dir, "sim_config.json"), "w") as fh:
        json.dump(config, fh, indent=2)
    return {"xml": xml_path, "tree": tree, "model": model, "config": config}


def model_report(model, tree, attach=None) -> str:
    """Kinematic structure + problems found in the URDF (missing mass, loops, ...)."""
    lines = [f"Base (fixed to world unless floating): {tree.base}"]
    joints = []

    def walk(link: str, depth: int):
        for e in tree.children.get(link, []):
            j = e.joint
            kind = {
                "revolute": "revolute",
                "continuous": "revolute (no limits)",
                "prismatic": "slider",
                "fixed": "fixed",
            }.get(j.type, j.type)
            joints.append(
                f"{'  ' * depth}- {j.name}: {kind}  {e.parent} -> {e.child}"
                + (" (reversed)" if e.reversed else "")
            )
            walk(e.child, depth + 1)

    walk(tree.base, 1)
    if joints:
        lines.append("Joints (names for the config):")
        lines.extend(joints)
    for child, parent in (attach or {}).items():
        lines.append(
            f"Re-attached '{child}' rigidly to '{parent}' (cut out of its sub-assembly by the export)"
        )
    massless = [n for n, link in model.links.items() if link.visuals and link.is_dummy]
    if massless:
        lines.append(
            "⚠ Left out - no mass (no material in Onshape): "
            + ", ".join(massless)
            + ". Use set_part_material if they belong to the robot."
        )
    lines.extend("⚠ " + w for w in tree.warnings)
    return "\n".join(lines)
