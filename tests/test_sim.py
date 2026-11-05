"""Tests for the URDF -> MJCF converter (re-rooting, physics config, MuJoCo compile)."""

import math
import xml.etree.ElementTree as ET

import pytest

from onshape_mcp.sim.exporter import _period
from onshape_mcp.sim.mjcf import build_mjcf, build_tree, parse_urdf, rotate_inertia
from onshape_mcp.sim.transforms import Pose, matrix_to_quat, rpy_to_matrix


def link(name, mass=0.1, com=(0, 0, 0)):
    return (
        f'<link name="{name}"><inertial><mass value="{mass}"/><origin xyz="{com[0]} {com[1]} {com[2]}" rpy="0 0 0"/>'
        f'<inertia ixx="1e-4" iyy="2e-4" izz="3e-4" ixy="0" ixz="0" iyz="0"/></inertial></link>'
    )


# root -(fixed)-> arm -(revolute about z at x=0.1)-> hand -(prismatic along x)-> slider
# plus a massless mate-connector helper link on "hand"
URDF = f"""<robot name="test">
  <link name="root"/>
  {link("arm", 0.5, (0.05, 0, 0))}
  {link("hand", 0.2, (0.02, 0, 0))}
  {link("slider", 0.1)}
  <link name="mc"><inertial><mass value="1e-09"/><inertia ixx="1e-14" iyy="1e-14" izz="1e-14" ixy="0" ixz="0" iyz="0"/></inertial></link>
  <joint name="root_joint" type="fixed"><parent link="root"/><child link="arm"/></joint>
  <joint name="Gelenk 1" type="revolute">
    <origin xyz="0.1 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/><parent link="arm"/><child link="hand"/>
    <limit lower="-1" upper="0.5" effort="1" velocity="1"/>
  </joint>
  <joint name="schiene" type="prismatic">
    <origin xyz="0.05 0 0.02" rpy="0 0 1.5707963"/><axis xyz="1 0 0"/><parent link="hand"/><child link="slider"/>
    <limit lower="0" upper="0.03" effort="1" velocity="1"/>
  </joint>
  <joint name="mc_joint" type="fixed"><origin xyz="0.01 0 0"/><parent link="hand"/><child link="mc"/></joint>
</robot>"""


def test_transforms_roundtrip():
    R = rpy_to_matrix(0.3, -0.2, 1.1)
    p = Pose(R, (1, 2, 3))
    q = p * p.inverse()
    assert q.p == pytest.approx((0, 0, 0), abs=1e-12)
    w, x, y, z = matrix_to_quat(rpy_to_matrix(0, 0, math.pi / 2))
    assert (w, z) == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))


def test_rotate_inertia_about_z():
    R = rpy_to_matrix(0, 0, math.pi / 2)
    out = rotate_inertia((1.0, 2.0, 3.0, 0, 0, 0), R)
    assert out[:3] == pytest.approx([2.0, 1.0, 3.0])


def test_parse_and_default_tree():
    model = parse_urdf(URDF)
    assert model.links["mc"].is_dummy and model.links["root"].is_dummy
    tree = build_tree(model)
    assert tree.base == "arm"
    assert [e.child for e in tree.children["arm"]] == ["hand"]
    assert tree.sites["hand"][0][0] == "mc"
    assert tree.world["slider"].p == pytest.approx((0.15, 0, 0.02))


def test_reroot_reverses_joints():
    tree = build_tree(parse_urdf(URDF), base="slider")
    edge = tree.children["slider"][0]
    assert edge.child == "hand" and edge.reversed
    assert tree.children["hand"][0].child == "arm" and tree.children["hand"][0].reversed


def _bodies(xml):
    root = ET.fromstring(xml)
    return {b.get("name"): b for b in root.iter("body")}


def test_mjcf_joint_attributes_and_reversed_range():
    cfg = {
        "base": "slider",
        "floor": False,
        "joints": {
            "gelenk_1": {
                "damping": 0.1,
                "frictionloss": 0.01,
                "armature": 1e-4,
                "stiffness": 2,
                "springref": 10,
            }
        },
    }
    xml, _ = build_mjcf(parse_urdf(URDF), cfg)
    joints = {j.get("name"): j for j in ET.fromstring(xml).iter("joint")}
    g = joints["gelenk_1"]
    assert g.get("type") == "hinge" and g.get("damping") == "0.1" and g.get("stiffness") == "2"
    assert float(g.get("springref")) == pytest.approx(math.radians(10))
    # reversed: [-upper, -lower] and negated axis
    assert [float(v) for v in g.get("range").split()] == pytest.approx([-0.5, 1.0])
    assert [float(v) for v in g.get("axis").split()] == pytest.approx([0, 0, -1])
    s = joints["schiene"]
    assert s.get("type") == "slide" and [float(v) for v in s.get("range").split()] == pytest.approx(
        [-0.03, 0]
    )
    excludes = {(e.get("body1"), e.get("body2")) for e in ET.fromstring(xml).iter("exclude")}
    assert ("slider", "hand") in excludes and ("hand", "arm") in excludes


def test_mjcf_actuator_spring_sensors():
    cfg = {
        "floor": False,
        "joints": {
            "Gelenk 1": {
                "range": [-45, 45],
                "actuator": {
                    "type": "position",
                    "kp": 3,
                    "ctrlrange": [-45, 45],
                    "forcerange": [-1, 1],
                },
            }
        },
        "springs": [
            {
                "name": "Feder",
                "body1": "arm",
                "point1": [0, 0, 0],
                "body2": "hand",
                "point2": [0.12, 0, 0],
                "stiffness": 100,
                "damping": 1,
                "rest_length": 0.02,
            }
        ],
    }
    xml, _ = build_mjcf(parse_urdf(URDF), cfg)
    root = ET.fromstring(xml)
    pos = root.find("actuator/position")
    assert pos.get("joint") == "gelenk_1" and pos.get("kp") == "3"
    assert [float(v) for v in pos.get("ctrlrange").split()] == pytest.approx(
        [-math.pi / 4, math.pi / 4]
    )
    spatial = root.find("tendon/spatial")
    assert spatial.get("stiffness") == "100" and spatial.get("springlength") == "0.02"
    assert {s.get("site") for s in spatial} == {"feder_1", "feder_2"}
    assert {s.get("name") for s in root.iter("jointpos")} == {"gelenk_1_pos", "schiene_pos"}


def test_period_estimate():
    times = [i * 0.001 for i in range(5000)]
    trace = [math.sin(2 * math.pi * t / 0.7) for t in times]
    assert _period(times, trace) == pytest.approx(0.7, rel=1e-3)


def test_mujoco_compiles_and_reroot_keeps_world_pose(tmp_path):
    mujoco = pytest.importorskip("mujoco")
    model = parse_urdf(URDF)
    positions = {}
    for base in ("arm", "slider"):
        xml, _ = build_mjcf(model, {"base": base, "floor": False})
        path = tmp_path / f"{base}.xml"
        path.write_text(xml)
        m = mujoco.MjModel.from_xml_path(str(path))
        d = mujoco.MjData(m)
        mujoco.mj_forward(m, d)
        positions[base] = {
            mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_BODY, b): tuple(d.xipos[b])
            for b in range(1, m.nbody)
        }
        assert m.nv == 2
    for name in ("arm", "hand", "slider"):  # centres of mass identical in both trees
        assert positions["arm"][name] == pytest.approx(positions["slider"][name], abs=1e-9)


def test_unsupported_joint_type_warns():
    urdf = URDF.replace('type="prismatic"', 'type="planar"')
    tree = build_tree(parse_urdf(urdf))
    assert any("planar" in w and "welded" in w for w in tree.warnings)
