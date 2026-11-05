"""Tests for rigid groups, axis joints and the sub-assembly fixes of the MuJoCo export."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from onshape_mcp.sim.mjcf import _jointed_pairs, build_tree, parse_urdf, resolve_split_parts
from onshape_mcp.tools import ToolContext, assembly_tools, sim_tools
from onshape_mcp.tools.assembly_tools import _apply, _group_feature, _WorldFrame, resolve_path

ASM = {
    "rootAssembly": {
        "instances": [
            {"id": "Mservo", "name": "SCS215 v5 (3) <1>", "type": "Assembly", "elementId": "E1"},
            {"id": "Marm", "name": "LampArm (Wrist-Head) <1>", "type": "Part"},
        ],
        "occurrences": [],
    },
    "subAssemblies": [
        {"elementId": "E1", "instances": [{"id": "Mhorn", "name": "Horn <1>", "type": "Part"}]}
    ],
}


def test_resolve_path_by_name_id_and_prefix():
    assert resolve_path(ASM, "SCS215 v5 (3) <1>/Horn <1>") == ["Mservo", "Mhorn"]
    assert resolve_path(ASM, "Marm") == ["Marm"]
    assert resolve_path(ASM, "lamparm") == ["Marm"]
    with pytest.raises(ValueError, match="Available"):
        resolve_path(ASM, "Nope <1>")


def test_apply_transform_point_and_vector():
    t = [0, -1, 0, 0.1, 1, 0, 0, 0.2, 0, 0, 1, 0.3, 0, 0, 0, 1]  # rot z 90° + translation
    assert _apply(t, (1, 0, 0), True) == pytest.approx((0.1, 1.2, 0.3))
    assert _apply(t, (1, 0, 0), False) == pytest.approx((0, 1, 0))


def test_group_feature_json():
    f = _group_feature("Basis", [["A"], ["B", "C"]])["feature"]
    assert f["featureType"] == "mateGroup"
    q = f["parameters"][0]["queries"]
    assert [x["path"] for x in q] == [["A"], ["B", "C"]]


async def test_axis_joint_compensates_offset_and_flip(monkeypatch):
    calls = []

    async def fake_connector(ctx, did, wid, eid, name, path, face, z_offset_m=0.0):
        calls.append((name, z_offset_m))
        if name.endswith("MC1"):  # arm hole: 2.25 mm higher, axis pointing down
            return "mc1", _WorldFrame((0, 0, 0.04280), (0, 0, -1))
        return "mc2", _WorldFrame((0, 0, 0.04055 + z_offset_m), (0, 0, 1))

    monkeypatch.setattr(assembly_tools, "_add_connector", fake_connector)
    monkeypatch.setattr(assembly_tools, "_definition", AsyncMock(return_value=ASM))
    am = MagicMock()
    am.add_feature = AsyncMock(
        return_value={"feature": {"featureId": "mate1"}, "featureState": {"featureStatus": "OK"}}
    )
    am.delete_feature = AsyncMock()
    ctx = ToolContext(MagicMock(), MagicMock(), MagicMock(), am, MagicMock())
    out = await assembly_tools.handle(
        "create_axis_joint",
        {
            "documentId": "d",
            "workspaceId": "w",
            "elementId": "e",
            "name": "basis_drehung",
            "firstInstance": "Marm",
            "firstFaceId": "KFad",
            "secondInstance": "SCS215 v5 (3) <1>/Horn <1>",
            "secondFaceId": "LFylB",
        },
        ctx,
    )
    text = out[0].text
    assert "status OK" in text and "flipped" in text and "No part moved" in text
    assert calls[-1][1] == pytest.approx(0.00225)  # MC2 recreated, shifted onto MC1
    mate = am.add_feature.call_args.args[3]["feature"]
    assert {
        "btType": "BTMParameterBoolean-144",
        "parameterId": "primaryAxisAlignment",
        "value": True,
    } in mate["parameters"]


async def test_axis_joint_rejects_non_coaxial(monkeypatch):
    async def fake_connector(ctx, did, wid, eid, name, path, face, z_offset_m=0.0):
        return (
            name,
            _WorldFrame((0.01, 0, 0), (0, 0, 1))
            if name.endswith("MC1")
            else _WorldFrame((0, 0, 0), (0, 0, 1)),
        )

    monkeypatch.setattr(assembly_tools, "_add_connector", fake_connector)
    monkeypatch.setattr(assembly_tools, "_definition", AsyncMock(return_value=ASM))
    am = MagicMock()
    am.delete_feature = AsyncMock()
    ctx = ToolContext(MagicMock(), MagicMock(), MagicMock(), am, MagicMock())
    out = await assembly_tools.handle(
        "create_axis_joint",
        {
            "documentId": "d",
            "workspaceId": "w",
            "elementId": "e",
            "name": "j",
            "firstInstance": "Marm",
            "firstFaceId": "a",
            "secondInstance": "Marm",
            "secondFaceId": "b",
        },
        ctx,
    )
    assert "apart" in out[0].text and am.delete_feature.await_count == 2  # connectors cleaned up


def _link(name, mass=0.05):
    return (
        f'<link name="{name}"><inertial><mass value="{mass}"/>'
        f'<inertia ixx="1e-5" iyy="1e-5" izz="1e-5" ixy="0" ixz="0" iyz="0"/></inertial>'
        f'<visual><geometry><mesh filename="package://x/meshes/{name}.stl"/></geometry></visual></link>'
    )


# what Onshape exports when a mate references a horn inside the servo sub-assembly:
# the horn is a separate link, connected only to the arm - not to its servo
SPLIT_URDF = f"""<robot name="r">
  {_link("lamp_base", 0.1)}{_link("scs215_v5__3___1_")}{_link("horn_3", 0.001)}{_link("arm", 0.02)}
  <link name="root"/>
  <joint name="hang" type="fixed"><parent link="root"/><child link="lamp_base"/></joint>
  <joint name="basis_fastened_joint" type="fixed"><parent link="lamp_base"/><child link="scs215_v5__3___1_"/></joint>
  <joint name="basis_drehung" type="continuous"><origin xyz="0 0 0.04"/><axis xyz="0 0 1"/>
    <parent link="arm"/><child link="horn_3"/></joint>
  <joint name="hang2" type="fixed"><parent link="root"/><child link="arm"/></joint>
</robot>"""


def test_split_part_reattached_and_tree_complete():
    model = parse_urdf(SPLIT_URDF)
    attach = resolve_split_parts(model, [("basis_drehung", "SCS215 v5 (3) <1>", "horn")])
    assert attach == {"horn_3": "scs215_v5__3___1_"}
    tree = build_tree(model, "lamp_base", attach)
    assert not tree.warnings
    pairs = {tuple(sorted(p)) for p in _jointed_pairs(tree)}
    # the arm must not collide with ANY body of the rigid servo side
    assert {("arm", "horn_3"), ("arm", "scs215_v5__3___1_"), ("arm", "lamp_base")} <= pairs


def test_unreachable_edges_are_not_loops():
    tree = build_tree(parse_urdf(SPLIT_URDF), "lamp_base")  # without the fix
    assert not any("loop" in w for w in tree.warnings)
    assert any("Not connected" in w for w in tree.warnings)


async def test_update_keeps_attach_even_without_merge(tmp_path):
    (tmp_path / "r.urdf").write_text(SPLIT_URDF.replace("package://x/meshes/", "meshes/"))
    (tmp_path / "meshes").mkdir()
    (tmp_path / "sim_config.json").write_text(
        json.dumps(
            {
                "name": "r",
                "base": "lamp_base",
                "floor": False,
                "attach": {"horn_3": "scs215_v5__3___1_"},
            }
        )
    )
    out = await sim_tools.handle(
        "update_mujoco_model",
        {
            "directory": str(tmp_path),
            "config": {"base": "lamp_base", "floor": False},
            "merge": False,
        },
        ToolContext(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock()),
    )
    assert "Re-attached 'horn_3'" in out[0].text
    assert json.loads((tmp_path / "sim_config.json").read_text())["attach"] == {
        "horn_3": "scs215_v5__3___1_"
    }
