"""URDF (as exported by Onshape) -> MuJoCo MJCF with physical joint properties.

Why our own converter instead of MuJoCo's URDF importer:
  * Onshape cannot fix ("ground") parts via the API, so its URDF root is often the wrong
    part. We re-root the kinematic tree at any link: link frames stay where they are and
    joints are placed explicitly inside the bodies (MJCF allows this, URDF does not).
  * Onshape adds massless helper links for mate connectors - they become <site>s.
  * Damping, friction, springs, armature, limits, actuators, tendons (springs between
    bodies), sensors, floor/contact settings come from a simple config.

Config (all optional; angles in degrees, lengths in metres unless noted):
{
  "name": "pendel",
  "base": "lagerbock",            # root link (default: Onshape's root)
  "floating": false,              # free joint on the base (mobile robots)
  "timestep": 0.001, "gravity": [0, 0, -9.81], "integrator": "implicitfast",
  "floor": true, "collisions": true, "friction": [1.0, 0.005, 0.0001],
  "joint_defaults": {"damping": 0.0, "frictionloss": 0.0, "armature": 0.0},
  "joints": {
    "pendelgelenk": {"damping": 0.002, "frictionloss": 0.0005, "stiffness": 0.0,
                     "springref": 0, "armature": 1e-5, "range": [-90, 90],
                     "actuator": {"type": "position", "kp": 5, "kv": 0.1,
                                  "ctrlrange": [-90, 90], "forcerange": [-2, 2]}}
  },
  "springs": [{"name": "feder", "body1": "a", "point1": [x,y,z], "body2": "b",
               "point2": [x,y,z], "stiffness": 200, "damping": 1, "rest_length": 0.05}],
  "sensors": true
}
Points of springs are world coordinates (metres) of the exported pose.
"""

from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from .transforms import Pose, fmt, normalize

DUMMY_MASS = 1e-5  # kg; real parts are always heavier than 0.01 g
_MOVING = {"revolute": "hinge", "continuous": "hinge", "prismatic": "slide"}


@dataclass
class Visual:
    origin: Pose
    mesh: Optional[str]
    scale: Tuple[float, float, float]
    rgba: Optional[Tuple[float, ...]]


@dataclass
class Link:
    name: str
    mass: float = 0.0
    inertial_origin: Pose = dc_field(default_factory=Pose)
    inertia: Tuple[float, ...] = (0, 0, 0, 0, 0, 0)  # ixx iyy izz ixy ixz iyz
    has_inertial: bool = False
    visuals: List[Visual] = dc_field(default_factory=list)

    @property
    def is_dummy(self) -> bool:
        # no inertial at all, or a negligible mass (helper links, parts without material)
        return not self.has_inertial or self.mass <= DUMMY_MASS


@dataclass
class Joint:
    name: str
    type: str
    parent: str
    child: str
    origin: Pose
    axis: Tuple[float, float, float] = (1.0, 0.0, 0.0)
    lower: Optional[float] = None
    upper: Optional[float] = None
    damping: Optional[float] = None
    friction: Optional[float] = None


@dataclass
class UrdfModel:
    name: str
    links: Dict[str, Link]
    joints: List[Joint]


# --------------------------------------------------------------------------- parsing
def _floats(text: Optional[str], n: int, default: float = 0.0) -> Tuple[float, ...]:
    if not text:
        return tuple([default] * n)
    vals = [float(v) for v in text.split()]
    return tuple(vals + [default] * (n - len(vals)))[:n]


def _origin(el: Optional[ET.Element]) -> Pose:
    if el is None:
        return Pose()
    return Pose.from_urdf(_floats(el.get("xyz"), 3), _floats(el.get("rpy"), 3))


def parse_urdf(text: str) -> UrdfModel:
    root = ET.fromstring(text)
    links: Dict[str, Link] = {}
    for le in root.findall("link"):
        link = Link(le.get("name"))
        inertial = le.find("inertial")
        if inertial is not None:
            link.has_inertial = True
            m = inertial.find("mass")
            link.mass = float(m.get("value", 0)) if m is not None else 0.0
            link.inertial_origin = _origin(inertial.find("origin"))
            ie = inertial.find("inertia")
            if ie is not None:
                g = lambda k: float(ie.get(k, 0))  # noqa: E731
                link.inertia = (g("ixx"), g("iyy"), g("izz"), g("ixy"), g("ixz"), g("iyz"))
        for ve in le.findall("visual"):
            mesh = ve.find("geometry/mesh")
            color = ve.find("material/color")
            link.visuals.append(
                Visual(
                    origin=_origin(ve.find("origin")),
                    mesh=mesh.get("filename") if mesh is not None else None,
                    scale=_floats(mesh.get("scale") if mesh is not None else None, 3, 1.0),  # type: ignore[arg-type]
                    rgba=_floats(color.get("rgba"), 4, 1.0) if color is not None else None,
                )
            )
        links[link.name] = link
    joints = []
    for je in root.findall("joint"):
        limit = je.find("limit")
        dyn = je.find("dynamics")
        axis = je.find("axis")
        joints.append(
            Joint(
                name=je.get("name"),
                type=je.get("type"),
                parent=je.find("parent").get("link"),
                child=je.find("child").get("link"),
                origin=_origin(je.find("origin")),
                axis=normalize(_floats(axis.get("xyz") if axis is not None else "1 0 0", 3)),
                lower=float(limit.get("lower"))
                if limit is not None and limit.get("lower")
                else None,
                upper=float(limit.get("upper"))
                if limit is not None and limit.get("upper")
                else None,
                damping=float(dyn.get("damping"))
                if dyn is not None and dyn.get("damping")
                else None,
                friction=float(dyn.get("friction"))
                if dyn is not None and dyn.get("friction")
                else None,
            )
        )
    return UrdfModel(root.get("name", "robot"), links, joints)


# --------------------------------------------------------------------------- tree
@dataclass
class TreeEdge:
    joint: Joint
    parent: str  # in the NEW tree
    child: str
    reversed: bool


@dataclass
class Tree:
    base: str
    world: Dict[str, Pose]  # link frame in world at q = 0
    children: Dict[str, List[TreeEdge]]
    sites: Dict[str, List[Tuple[str, Pose]]]  # body -> [(site name, world pose)]
    loops: List[Joint]
    warnings: List[str]


def _canon(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def resolve_split_parts(model: UrdfModel, hints: List[Tuple[str, str, str]]) -> Dict[str, str]:
    """Onshape's URDF export cuts a part out of its sub-assembly when a mate references it
    (e.g. a servo horn) and leaves it unconnected to the sub-assembly. ``hints`` are
    (mate name, sub-assembly instance name, part name) from the assembly definition; the
    result maps the cut-out link -> the sub-assembly link (re-attached rigidly)."""
    joints = {_canon(j.name): j for j in model.joints}
    degree: Dict[str, int] = {}
    for j in model.joints:
        if not model.links[j.parent].is_dummy and not model.links[j.child].is_dummy:
            degree[j.parent] = degree.get(j.parent, 0) + 1
            degree[j.child] = degree.get(j.child, 0) + 1
    out: Dict[str, str] = {}
    for mate, sub_name, part_name in hints:
        j = joints.get(_canon(mate))
        if j is None:
            continue
        try:
            sub_link = _find_link(model, sub_name)
        except ValueError:
            continue
        part_key = _canon(part_name)
        sides = [ln for ln in (j.parent, j.child) if _canon(ln).startswith(part_key)]
        if len(sides) != 1:
            sides = [ln for ln in (j.parent, j.child) if degree.get(ln, 0) == 1]
        if len(sides) == 1 and sides[0] != sub_link:
            out[sides[0]] = sub_link
    return out


def _find_link(model: UrdfModel, name: str) -> str:
    if name in model.links:
        return name
    key = _canon(name)
    for ln in model.links:
        if _canon(ln) == key or _canon(ln).startswith(key):
            return ln
    raise ValueError(f"Link '{name}' not found. Links: {', '.join(model.links)}")


def build_tree(
    model: UrdfModel, base: Optional[str] = None, attach: Optional[Dict[str, str]] = None
) -> Tree:
    # world poses (exported configuration = q 0)
    children_of: Dict[str, List[Joint]] = {}
    is_child = set()
    for j in model.joints:
        children_of.setdefault(j.parent, []).append(j)
        is_child.add(j.child)
    world: Dict[str, Pose] = {}
    stack = [ln for ln in model.links if ln not in is_child]
    for r in stack:
        world[r] = Pose()
    while stack:
        p = stack.pop()
        for j in children_of.get(p, []):
            world[j.child] = world[p] * j.origin
            stack.append(j.child)

    real = {n for n, link in model.links.items() if not link.is_dummy}
    warnings: List[str] = []
    extra: List[Joint] = []
    for child, parent in (attach or {}).items():
        c, p = _find_link(model, child), _find_link(model, parent)
        extra.append(Joint(f"{_canon(c)}_attached", "fixed", p, c, world[p].inverse() * world[c]))
    sites: Dict[str, List[Tuple[str, Pose]]] = {}
    edges: List[Joint] = []
    for j in model.joints + extra:
        if j.parent in real and j.child in real:
            edges.append(j)
        elif j.type == "fixed" and (j.parent in real) != (j.child in real):
            body, dummy = (j.parent, j.child) if j.parent in real else (j.child, j.parent)
            # mate connector helpers -> sites (skip the export's artificial "root")
            if model.links[dummy].has_inertial and _canon(dummy) != "root":
                sites.setdefault(body, []).append((_canon(dummy), world[dummy]))

    if base:
        base = _find_link(model, base)
    else:  # Onshape's choice: real link attached to the empty root
        candidates = [j.child for j in model.joints if j.parent not in real and j.child in real]
        base = candidates[0] if candidates else sorted(real)[0]
    if base not in real:
        raise ValueError(f"'{base}' has no geometry/mass and cannot be the base")

    adj: Dict[str, List[Joint]] = {}
    for j in edges:
        adj.setdefault(j.parent, []).append(j)
        adj.setdefault(j.child, []).append(j)
    children: Dict[str, List[TreeEdge]] = {}
    visited = {base}
    used = set()
    queue = deque([base])
    while queue:
        cur = queue.popleft()
        for j in adj.get(cur, []):
            if j.name in used:
                continue
            other = j.child if j.parent == cur else j.parent
            if other in visited:
                continue
            used.add(j.name)
            visited.add(other)
            children.setdefault(cur, []).append(TreeEdge(j, cur, other, reversed=(j.child == cur)))
            queue.append(other)
    for j in edges:
        if j.type not in _MOVING and j.type != "fixed":
            warnings.append(
                f"Joint '{j.name}' has URDF type '{j.type}', which this converter does not support - "
                "the parts are welded together. Use revolute/slider mates (a ball joint can be built "
                "from three revolute mates)."
            )
    loops = [j for j in edges if j.name not in used and j.parent in visited and j.child in visited]
    for j in loops:
        warnings.append(
            f"Joint '{j.name}' closes a kinematic loop - modelled as an equality constraint."
        )
    unreachable = real - visited
    if unreachable:
        warnings.append(
            "Not connected to the base (no mate) and therefore left out: "
            + ", ".join(sorted(unreachable))
        )
    return Tree(base, world, children, sites, loops, warnings)


# --------------------------------------------------------------------------- MJCF
def _deg(v: float) -> float:
    return math.radians(v)


def _joint_cfg(config: Dict[str, Any], joint: Joint) -> Dict[str, Any]:
    cfg = dict(config.get("joint_defaults") or {})
    joints = config.get("joints") or {}
    for key, value in joints.items():
        if _canon(key) == _canon(joint.name):
            cfg.update(value or {})
    return cfg


def _mesh_name(path: str) -> str:
    return _canon(os.path.splitext(os.path.basename(path))[0])


def build_mjcf(
    model: UrdfModel, config: Optional[Dict[str, Any]] = None, mesh_dir: str = "meshes"
) -> Tuple[str, Tree]:
    config = config or {}
    tree = build_tree(model, config.get("base"), config.get("attach"))
    name = config.get("name") or model.name

    mj = ET.Element("mujoco", model=name)
    ET.SubElement(
        mj, "compiler", angle="radian", meshdir=mesh_dir, autolimits="true", inertiafromgeom="false"
    )
    option = ET.SubElement(
        mj,
        "option",
        timestep=str(config.get("timestep", 0.001)),
        gravity=fmt(config.get("gravity", [0, 0, -9.81])),
        integrator=str(config.get("integrator", "implicitfast")),
    )
    if not config.get("collisions", True):
        ET.SubElement(option, "flag", contact="disable")

    default = ET.SubElement(mj, "default")
    dclass = ET.SubElement(default, "default", {"class": "robot"})
    ET.SubElement(dclass, "geom", friction=fmt(config.get("friction", [1.0, 0.005, 0.0001])))
    vis = ET.SubElement(dclass, "default", {"class": "visual"})
    ET.SubElement(vis, "geom", type="mesh", contype="0", conaffinity="0", group="2")
    col = ET.SubElement(dclass, "default", {"class": "collision"})
    ET.SubElement(col, "geom", type="mesh", group="3", rgba="0.8 0.3 0.3 0.4")

    asset = ET.SubElement(mj, "asset")
    meshes_seen: Dict[str, str] = {}
    for link in model.links.values():
        for v in link.visuals:
            if v.mesh and v.mesh not in meshes_seen:
                mname = _mesh_name(v.mesh)
                meshes_seen[v.mesh] = mname
                ET.SubElement(
                    asset,
                    "mesh",
                    name=mname,
                    file=os.path.basename(v.mesh.split("://")[-1]),
                    scale=fmt(v.scale),
                )
    if config.get("floor", True):
        ET.SubElement(
            asset,
            "texture",
            name="grid",
            type="2d",
            builtin="checker",
            rgb1=".2 .3 .4",
            rgb2=".1 .2 .3",
            width="512",
            height="512",
        )
        ET.SubElement(
            asset, "material", name="grid", texture="grid", texrepeat="8 8", reflectance=".1"
        )

    world = ET.SubElement(mj, "worldbody")
    if config.get("floor", True):
        ET.SubElement(world, "light", pos="0 0 3", dir="0 0 -1", directional="true")
        floor_z = config.get("floor_height")
        if floor_z is None:
            floor_z = _lowest_point(model, tree) - 0.001
        ET.SubElement(
            world,
            "geom",
            name="floor",
            type="plane",
            size="0 0 0.05",
            material="grid",
            pos=f"0 0 {floor_z:.6g}",
        )

    actuators: List[Tuple[str, Dict[str, Any], str]] = []
    used_sites: set = set()
    joint_names: List[str] = []

    def add_body(
        parent_el: ET.Element, link_name: str, parent_pose: Pose, edge: Optional[TreeEdge]
    ):
        link = model.links[link_name]
        w = tree.world[link_name]
        rel = parent_pose.inverse() * w
        body = ET.SubElement(
            parent_el,
            "body",
            name=_canon(link_name),
            pos=fmt(rel.p),
            quat=fmt(rel.quat()),
            childclass="robot",
        )
        if edge is None and config.get("floating"):
            ET.SubElement(body, "freejoint", name=f"{_canon(link_name)}_free")
        if edge is not None and edge.joint.type in _MOVING:
            j = edge.joint
            # joint frame of the original joint = original child link frame
            orig_child = tree.world[j.child]
            anchor_world = orig_child.p
            axis_world = orig_child.vector(j.axis)
            sign = -1.0 if edge.reversed else 1.0
            inv = w.inverse()
            attrs = {
                "name": _canon(j.name),
                "type": _MOVING[j.type],
                "pos": fmt(inv.point(anchor_world)),
                "axis": fmt(normalize([sign * c for c in inv.vector(axis_world)])),
            }
            cfg = _joint_cfg(config, j)
            is_hinge = _MOVING[j.type] == "hinge"
            conv = _deg if is_hinge else (lambda v: v)
            lo, hi = j.lower, j.upper
            if j.type == "continuous":
                lo = hi = None
            if cfg.get("range") is not None:
                lo, hi = conv(cfg["range"][0]), conv(cfg["range"][1])
            if lo is not None and hi is not None:
                if edge.reversed:
                    lo, hi = -hi, -lo
                attrs["range"] = fmt([lo, hi])
            damping = cfg.get("damping", j.damping)
            friction = cfg.get("frictionloss", j.friction)
            for key, value in (
                ("damping", damping),
                ("frictionloss", friction),
                ("armature", cfg.get("armature")),
                ("stiffness", cfg.get("stiffness")),
            ):
                if value:
                    attrs[key] = f"{float(value):.9g}"
            if cfg.get("springref") is not None:
                attrs["springref"] = f"{conv(float(cfg['springref'])):.9g}"
            ET.SubElement(body, "joint", attrs)
            joint_names.append(attrs["name"])
            if cfg.get("actuator"):
                actuators.append((attrs["name"], cfg["actuator"], "hinge" if is_hinge else "slide"))

        if link.has_inertial and link.mass > 0:
            ET.SubElement(
                body,
                "inertial",
                pos=fmt(link.inertial_origin.p),
                mass=f"{link.mass:.9g}",
                fullinertia=fmt(rotate_inertia(link.inertia, link.inertial_origin.R)),
            )
        for i, v in enumerate(link.visuals):
            if not v.mesh:
                continue
            geom = {
                "mesh": meshes_seen[v.mesh],
                "pos": fmt(v.origin.p),
                "quat": fmt(v.origin.quat()),
            }
            vis_attrs = dict(geom, **{"class": "visual"})
            if v.rgba:
                vis_attrs["rgba"] = fmt(v.rgba, 4)
            ET.SubElement(body, "geom", vis_attrs, name=f"{_canon(link_name)}_visual_{i}")
            if config.get("collisions", True):
                ET.SubElement(
                    body,
                    "geom",
                    dict(geom, **{"class": "collision"}),
                    name=f"{_canon(link_name)}_collision_{i}",
                )
        for sname, spose in tree.sites.get(link_name, []):
            if sname in used_sites:
                sname = f"{sname}_{_canon(link_name)}"
            used_sites.add(sname)
            local = w.inverse() * spose
            ET.SubElement(
                body, "site", name=sname, pos=fmt(local.p), quat=fmt(local.quat()), size="0.002"
            )
        for spring in config.get("springs") or []:
            for idx in (1, 2):
                if _canon(spring.get(f"body{idx}", "")) == _canon(link_name):
                    ET.SubElement(
                        body,
                        "site",
                        name=f"{_canon(spring.get('name', 'spring'))}_{idx}",
                        pos=fmt(w.inverse().point(spring[f"point{idx}"])),
                        size="0.002",
                    )
        for child_edge in tree.children.get(link_name, []):
            add_body(body, child_edge.child, w, child_edge)

    add_body(world, tree.base, Pose(), None)

    # Bodies connected by a joint (and loop joints) touch at the bearing; MuJoCo uses convex
    # hulls for mesh collisions, so a pin in a hole would collide. Exclude these pairs.
    if config.get("collisions", True):
        pairs = _jointed_pairs(tree)
        if pairs:
            contact = ET.SubElement(mj, "contact")
            for a, b in pairs:
                ET.SubElement(contact, "exclude", body1=_canon(a), body2=_canon(b))

    if tree.loops:
        eq = ET.SubElement(mj, "equality")
        for j in tree.loops:
            anchor = tree.world[j.child].p
            ET.SubElement(
                eq,
                "connect",
                name=f"{_canon(j.name)}_loop",
                body1=_canon(j.parent),
                body2=_canon(j.child),
                anchor=fmt(tree.world[j.parent].inverse().point(anchor)),
            )

    springs = config.get("springs") or []
    if springs:
        tendon = ET.SubElement(mj, "tendon")
        for spring in springs:
            sname = _canon(spring.get("name", "spring"))
            attrs = {
                "name": sname,
                "stiffness": f"{float(spring.get('stiffness', 0)):.9g}",
                "damping": f"{float(spring.get('damping', 0)):.9g}",
                "width": "0.002",
                "rgba": "0.9 0.6 0.1 1",
            }
            if spring.get("rest_length") is not None:
                attrs["springlength"] = f"{float(spring['rest_length']):.9g}"
            sp = ET.SubElement(tendon, "spatial", attrs)
            ET.SubElement(sp, "site", site=f"{sname}_1")
            ET.SubElement(sp, "site", site=f"{sname}_2")

    if actuators:
        act = ET.SubElement(mj, "actuator")
        for jname, a, kind in actuators:
            conv = _deg if kind == "hinge" else (lambda v: v)
            typ = a.get("type", "position")
            attrs = {"name": f"{jname}_{typ}", "joint": jname}
            if typ == "position":
                attrs["kp"] = f"{float(a.get('kp', 1.0)):.9g}"
                if a.get("kv") is not None:
                    attrs["kv"] = f"{float(a['kv']):.9g}"
                if a.get("ctrlrange"):
                    attrs["ctrlrange"] = fmt([conv(a["ctrlrange"][0]), conv(a["ctrlrange"][1])])
            elif typ == "velocity":
                attrs["kv"] = f"{float(a.get('kv', 1.0)):.9g}"
                if a.get("ctrlrange"):
                    attrs["ctrlrange"] = fmt([conv(a["ctrlrange"][0]), conv(a["ctrlrange"][1])])
            elif typ == "motor":
                attrs["gear"] = f"{float(a.get('gear', 1.0)):.9g}"
                if a.get("ctrlrange"):
                    attrs["ctrlrange"] = fmt(a["ctrlrange"])
            else:
                raise ValueError(f"Unknown actuator type '{typ}' (position, velocity, motor)")
            if a.get("forcerange"):
                attrs["forcerange"] = fmt(a["forcerange"])
            ET.SubElement(act, typ, attrs)

    if config.get("sensors", True) and joint_names:
        sens = ET.SubElement(mj, "sensor")
        for jname in joint_names:
            ET.SubElement(sens, "jointpos", name=f"{jname}_pos", joint=jname)
            ET.SubElement(sens, "jointvel", name=f"{jname}_vel", joint=jname)

    ET.indent(mj, space="  ")
    return ET.tostring(mj, encoding="unicode"), tree


def _jointed_pairs(tree: Tree) -> List[Tuple[str, str]]:
    """Body pairs that must not collide: everything rigidly welded on one side of a joint
    against everything welded on the other side (servo case + bracket vs. arm, ...)."""
    group: Dict[str, str] = {}

    def find(x: str) -> str:
        while group.get(x, x) != x:
            x = group[x]
        return x

    moving: List[Tuple[str, str]] = []
    for parent, edges in tree.children.items():
        for e in edges:
            group.setdefault(parent, parent)
            group.setdefault(e.child, e.child)
            if e.joint.type in _MOVING:
                moving.append((parent, e.child))
            else:
                group[find(e.child)] = find(parent)
    moving.extend((j.parent, j.child) for j in tree.loops)
    members: Dict[str, List[str]] = {}
    for body in group:
        members.setdefault(find(body), []).append(body)
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for a, b in moving:
        for x in members.get(find(a), [a]):
            for y in members.get(find(b), [b]):
                key = tuple(sorted((x, y)))
                if x != y and key not in seen:
                    seen.add(key)
                    pairs.append((x, y))
    return pairs


def rotate_inertia(inertia, R) -> List[float]:
    """Inertia (ixx iyy izz ixy ixz iyz) given in a rotated frame -> parent frame: R I R^T."""
    ixx, iyy, izz, ixy, ixz, iyz = inertia
    full = [[ixx, ixy, ixz], [ixy, iyy, iyz], [ixz, iyz, izz]]
    rot = [[sum(R[i][k] * full[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    out = [[sum(rot[i][k] * R[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
    return [out[0][0], out[1][1], out[2][2], out[0][1], out[0][2], out[1][2]]


def _lowest_point(model: UrdfModel, tree: Tree) -> float:
    """Approximate lowest z of the model (link origins and centres of mass)."""
    zs = []
    for name, pose in tree.world.items():
        link = model.links.get(name)
        if link is None or link.is_dummy:
            continue
        zs.append(pose.p[2])
        zs.append(pose.point(link.inertial_origin.p)[2])
    return min(zs) if zs else 0.0
