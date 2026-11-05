"""Convert Onshape FeatureScript evaluation results (BTFSValue*) into plain Python."""

from __future__ import annotations

from typing import Any


def fs_to_py(value: Any) -> Any:
    """Recursively convert a BTFSValue structure into dict / list / float / str / bool.

    Quantities with units are returned as plain numbers in SI (metres, radians, kg ...),
    which is what Onshape returns internally.
    """
    if not isinstance(value, dict):
        return value
    bt = value.get("btType") or value.get("typeName") or ""
    inner = value.get("value")
    if "BTFSValueMap" in bt:
        out = {}
        for entry in inner or []:
            key = fs_to_py(entry.get("key"))
            out[str(key)] = fs_to_py(entry.get("value"))
        return out
    if "BTFSValueArray" in bt:
        return [fs_to_py(v) for v in inner or []]
    if "BTFSValueWithUnits" in bt or "BTFSValueNumber" in bt:
        return float(inner) if inner is not None else None
    if "BTFSValueString" in bt or "BTFSValueBoolean" in bt:
        return inner
    if "BTFSValueUndefined" in bt:
        return None
    if "BTFSValue" in bt:
        return fs_to_py(inner) if isinstance(inner, (dict, list)) else inner
    if "result" in value:  # whole eval response
        return fs_to_py(value["result"])
    return value


def eval_result(response: dict) -> Any:
    """Return the converted ``result`` of a featurescript eval response (raises on errors)."""
    messages = []
    for n in response.get("notices", []) or []:
        if isinstance(n, str):
            messages.append(n)
            continue
        msg = n.get("message")
        if isinstance(msg, dict):
            msg = msg.get("message")
        level = str(n.get("level", "")) + str(n.get("type", ""))
        if msg and ("ERROR" in level.upper() or response.get("result") is None):
            messages.append(str(msg))
    if response.get("result") is None and messages:
        raise ValueError("FeatureScript error: " + "; ".join(messages)[:800])
    return fs_to_py(response.get("result"))
