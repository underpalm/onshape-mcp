"""Generic Part Studio feature builder driven by Onshape's own feature specs.

Onshape describes every built-in feature (shell, hole, mirror, cPlane, helix,
sweep, loft, sheet metal, ...) through ``/partstudios/.../featurespecs``. This
module turns a simple ``{parameterId: value}`` mapping into the BTMFeature JSON
the API expects, typing every value according to that spec. Parameters that are
not given keep Onshape's defaults.

Value conventions (what tools accept):
  * quantities: numbers in the configured length unit / degrees, or an Onshape
    expression string such as ``"2.5 mm"``, ``"30 deg"`` or ``"#wall"``
  * booleans, enum names (e.g. ``"REMOVE"``), strings
  * queries: a deterministic id (``"JHK"``) or a list of them, a standard plane
    name (``"Top"``, ``"Front"``, ``"Right"``), ``{"sketch": "<featureId>"}``
    for the regions of a sketch, or ``{"ids": [...]}``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..units import get_length_unit

_PLANES = {"top", "front", "right"}


class FeatureSpecError(ValueError):
    pass


def _msg(x: Any) -> Dict[str, Any]:
    return x.get("message", x) if isinstance(x, dict) else {}


def index_specs(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """featureType -> spec message (with parameters keyed by id)."""
    out: Dict[str, Dict[str, Any]] = {}
    for item in raw.get("featureSpecs", raw if isinstance(raw, list) else []):
        spec = _msg(item)
        ftype = spec.get("featureType")
        if not ftype:
            continue
        params = {}
        for p in spec.get("parameters", []):
            m = _msg(p)
            params[m.get("parameterId")] = {"typeName": p.get("typeName", ""), **m}
        out[ftype] = {**spec, "parametersById": params}
    return out


def describe(spec: Dict[str, Any]) -> str:
    """Human/LLM readable summary of a feature type."""
    lines = [
        f"Feature type '{spec['featureType']}' ({spec.get('featureTypeName', '')})",
        (spec.get("featureTypeDescription") or "").strip(),
        "Parameters:",
    ]
    for pid, p in spec["parametersById"].items():
        t = p["typeName"].replace("BTParameterSpec", "")
        extra = ""
        if t == "Enum":
            opts = [
                _msg(o).get("option", o) if isinstance(o, dict) else o for o in p.get("options", [])
            ]
            extra = f" one of {opts}"
        elif t == "Quantity":
            q = p.get("quantityType", "")
            unit = {"LENGTH": get_length_unit(), "ANGLE": "deg"}.get(q, "")
            default = _msg(p.get("defaultValue")).get("value")
            extra = f" {q.lower()}" + (f" [{unit}]" if unit else "")
            if default is not None and q == "LENGTH":
                from ..units import from_m

                extra += f", default {from_m(default):g}"
            elif default is not None:
                extra += f", default {default:g}"
        elif t == "Boolean":
            extra = f" default {_msg(p.get('defaultValue')).get('value')}"
        elif t == "Query":
            extra = " (entity ids / plane name / {'sketch': featureId})"
            if p.get("maxNumberOfPicks") == 1:
                extra += " single"
        name = p.get("parameterName") or ""
        lines.append(f"  - {pid}: {t}{extra}  {('- ' + name) if name else ''}".rstrip())
    return "\n".join(line for line in lines if line is not None)


class GenericFeatureBuilder:
    def __init__(self, spec: Dict[str, Any], plane_ids: Optional[Dict[str, str]] = None):
        self.spec = spec
        self.plane_ids = {k.lower(): v for k, v in (plane_ids or {}).items()}

    # ------------------------------------------------------------------ values
    def _quantity(self, pid: str, p: Dict[str, Any], value: Any) -> Dict[str, Any]:
        qtype = p.get("quantityType", "")
        is_int = qtype == "INTEGER"
        if isinstance(value, str):
            expression = value
        elif isinstance(value, bool):
            raise FeatureSpecError(f"{pid}: expected a number, got a boolean")
        elif qtype == "LENGTH":
            expression = f"{value:g} {get_length_unit()}"
        elif qtype == "ANGLE":
            expression = f"{value:g} deg"
        elif is_int:
            expression = str(int(value))
        else:
            expression = f"{value:g}"
        return {
            "btType": "BTMParameterQuantity-147",
            "parameterId": pid,
            "expression": expression,
            "isInteger": is_int,
        }

    def _enum(self, pid: str, p: Dict[str, Any], value: Any) -> Dict[str, Any]:
        options = [
            _msg(o).get("option", o) if isinstance(o, dict) else o for o in p.get("options", [])
        ]
        v = str(value)
        if options and v not in options:
            upper = v.upper().replace(" ", "_")
            if upper in options:
                v = upper
            else:
                raise FeatureSpecError(f"{pid}: '{value}' is not one of {options}")
        return {
            "btType": "BTMParameterEnum-145",
            "parameterId": pid,
            "enumName": p.get("enumName", ""),
            "namespace": "",
            "value": v,
        }

    def _query(self, pid: str, value: Any) -> Dict[str, Any]:
        queries: List[Dict[str, Any]] = []
        items = value if isinstance(value, list) else [value]
        ids: List[str] = []
        for item in items:
            if isinstance(item, dict) and "sketch" in item:
                fid = item["sketch"]
                queries.append(
                    {
                        "btType": "BTMIndividualSketchRegionQuery-140",
                        "featureId": fid,
                        "filterInnerLoops": bool(item.get("filterInnerLoops", True)),
                        "queryString": f'query = qSketchRegion(id + "{fid}", true);',
                    }
                )
            elif isinstance(item, dict) and "ids" in item:
                ids.extend(item["ids"])
            elif isinstance(item, str) and item.lower() in _PLANES:
                if item.lower() not in self.plane_ids:
                    raise FeatureSpecError(f"{pid}: plane id for '{item}' not resolved")
                ids.append(self.plane_ids[item.lower()])
            elif isinstance(item, str):
                ids.append(item)
            else:
                raise FeatureSpecError(f"{pid}: unsupported query value {item!r}")
        if ids:
            queries.append({"btType": "BTMIndividualQuery-138", "deterministicIds": ids})
        return {"btType": "BTMParameterQueryList-148", "parameterId": pid, "queries": queries}

    def parameter(self, pid: str, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict) and "btType" in value:  # raw passthrough for experts
            return {**value, "parameterId": pid}
        p = self.spec["parametersById"].get(pid)
        if p is None:
            known = ", ".join(self.spec["parametersById"])
            raise FeatureSpecError(
                f"Unknown parameter '{pid}' for {self.spec['featureType']}. Known: {known}"
            )
        t = p["typeName"]
        if t.endswith("Quantity"):
            return self._quantity(pid, p, value)
        if t.endswith("Boolean"):
            return {"btType": "BTMParameterBoolean-144", "parameterId": pid, "value": bool(value)}
        if t.endswith("Enum"):
            return self._enum(pid, p, value)
        if t.endswith("String"):
            return {"btType": "BTMParameterString-149", "parameterId": pid, "value": str(value)}
        if t.endswith("Query"):
            return self._query(pid, value)
        raise FeatureSpecError(
            f"{pid}: parameter type {t} is not supported by the generic builder "
            "(pass a raw BTMParameter dict with 'btType')."
        )

    def needed_planes(self, parameters: Dict[str, Any]) -> List[str]:
        names = []
        for v in parameters.values():
            for item in v if isinstance(v, list) else [v]:
                if isinstance(item, str) and item.lower() in _PLANES:
                    names.append(item)
        return names

    def build(self, name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "btType": "BTFeatureDefinitionCall-1406",
            "feature": {
                "btType": "BTMFeature-134",
                "featureType": self.spec["featureType"],
                "name": name or self.spec.get("featureTypeName") or self.spec["featureType"],
                "namespace": "",
                "suppressed": False,
                "parameters": [self.parameter(pid, v) for pid, v in parameters.items()],
            },
        }
