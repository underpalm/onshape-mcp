"""Onshape standard content (ISO / DIN / ANSI fasteners, washers, pins, ...).

Onshape stores the size tables of every standard part in a FeatureScript
"FSRules" Feature Studio inside the (public) standard content document. The UI
evaluates those rules to turn a choice such as "M3 x 10" into the numeric
configuration of the part (head diameter, pitch, ...). The REST API does not
expose that step, so this module re-implements it:

* it parses the data tables (pure data) and the ``ProcessXxx`` rule functions,
* evaluates the small arithmetic expressions with a whitelisted AST evaluator
  (the downloaded FeatureScript is never executed as code),
* and produces the configuration string for ``assemblies/.../instances``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

CACHE_DIR = os.path.expanduser(os.getenv("ONSHAPE_MCP_CACHE", "~/.cache/onshape-mcp"))
_LIST_TTL = 30 * 24 * 3600


# --------------------------------------------------------------------------- quantities
@dataclass(frozen=True)
class Q:
    """A number with a unit: 'mm' (lengths), 'deg' (angles) or None (plain)."""

    value: float
    unit: Optional[str] = None

    def _u(self, other: "Q", op: str) -> Optional[str]:
        if op in "+-":
            return self.unit or other.unit
        if op == "*":
            return self.unit or other.unit
        # division: mm/mm -> None
        return None if self.unit == other.unit else self.unit

    def _bin(self, other, op):
        o = other if isinstance(other, Q) else Q(float(other))
        a, b = self.value, o.value
        v = {"+": a + b, "-": a - b, "*": a * b, "/": a / b}[op]
        return Q(v, self._u(o, op))

    def __add__(self, o):
        return self._bin(o, "+")

    def __sub__(self, o):
        return self._bin(o, "-")

    def __mul__(self, o):
        return self._bin(o, "*")

    def __truediv__(self, o):
        return self._bin(o, "/")

    def __radd__(self, o):
        return Q(float(o)) + self

    def __rsub__(self, o):
        return Q(float(o)) - self

    def __rmul__(self, o):
        return Q(float(o)) * self

    def __rtruediv__(self, o):
        return Q(float(o)) / self

    def __neg__(self):
        return Q(-self.value, self.unit)


_UNIT_NAMES = {
    "millimeter": Q(1.0, "mm"),
    "centimeter": Q(10.0, "mm"),
    "meter": Q(1000.0, "mm"),
    "inch": Q(25.4, "mm"),
    "foot": Q(304.8, "mm"),
    "degree": Q(1.0, "deg"),
    "radian": Q(57.29577951308232, "deg"),
}


def _num(text: Any) -> float:
    if isinstance(text, Q):
        return text.value
    s = str(text).strip()
    if "/" in s and re.fullmatch(r"[\d.\s/-]+", s):  # fractions like "1/4" or "1-1/2"
        whole, _, frac = s.rpartition("-") if "-" in s.strip("-") else ("", "", s)
        n, d = frac.split("/")
        return (float(whole) if whole else 0.0) + float(n) / float(d)
    return float(s)


# --------------------------------------------------------------------------- parsing
@dataclass
class ParamRule:
    parameter_id: str
    function: str
    map_var: str
    table: Optional[str] = None
    field: Optional[str] = None
    display_name: Optional[str] = None
    statements: List[Tuple[str, str]] = dc_field(default_factory=list)  # (var, expr)
    config: List[Tuple[str, str]] = dc_field(default_factory=list)  # (configParam, expr)
    # if/else chains; each chain: [(condition expr or None for else, config entries), ...]
    branches: List[List[Tuple[Optional[str], List[Tuple[str, str]]]]] = dc_field(
        default_factory=list
    )
    static_values: Optional[List[str]] = None  # options from scStaticParameterValues([...])


@dataclass
class Rules:
    tables: Dict[str, Dict[str, Any]]
    params: List[ParamRule]


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", src)


def _balanced(src: str, start: int, open_ch: str = "{", close_ch: str = "}") -> int:
    """Return index just after the bracket that closes the one at ``start``."""
    depth = 0
    in_str = False
    i = start
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("Unbalanced brackets in FSRules")


def _fs_literal_to_json(text: str) -> Any:
    text = re.sub(r"\bas\s+\w+", "", text)
    text = re.sub(r",\s*([\]}])", r"\1", text)
    return json.loads(text)


def parse_rules(src: str) -> Rules:
    src = _strip_comments(src)

    m = re.search(r"const\s+dataTables\s*=\s*\{", src)
    if not m:
        raise ValueError("FSRules without dataTables")
    end = _balanced(src, m.end() - 1)
    tables = _fs_literal_to_json(src[m.end() - 1 : end])

    m = re.search(r"const\s+defaultValues\s*=\s*\[", src)
    if not m:
        raise ValueError("FSRules without defaultValues")
    dv_end = _balanced(src, m.end() - 1, "[", "]")
    param_ids = [
        pid
        for pid in re.findall(r'\{\s*"(\w+)"\s*:', src[m.end() - 1 : dv_end])
        if pid != "doNotFilterParameterValues"
    ]

    main = re.search(r"function\s+main\s*\(\s*\)\s*\{", src)
    if not main:
        raise ValueError("FSRules without main()")
    main_body = src[main.end() - 1 : _balanced(src, main.end() - 1)]
    calls = dict(
        (int(i), fn)
        for i, fn in re.findall(r"filterArray\.parameters\[(\d+)\]\s*=\s*(\w+)\s*\(", main_body)
    )

    params: List[ParamRule] = []
    for index, pid in enumerate(param_ids):
        fn = calls.get(index)
        if fn is None:
            raise ValueError(f"No rule function for parameter {pid}")
        fm = re.search(rf"function\s+{fn}\s*\(([^)]*)\)[^{{]*\{{", src)
        if not fm:
            raise ValueError(f"Rule function {fn} not found")
        args = [a.strip().split()[0] for a in fm.group(1).split(",") if a.strip()]
        map_var = args[1] if len(args) > 1 else args[0]
        body = src[fm.end() - 1 : _balanced(src, fm.end() - 1)]
        rule = ParamRule(parameter_id=pid, function=fn, map_var=map_var)

        t = re.search(rf'{map_var}\.DataTable\s*=\s*"([^"]+)"', body)
        rule.table = t.group(1) if t else None
        f = re.search(
            rf'{map_var}\.ParameterValues\s*=\s*scGetFieldValuesFromTable\(\s*dataTables\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"',
            body,
        )
        if f:
            rule.table, rule.field = f.group(1), f.group(2)
        s = re.search(
            rf"{map_var}\.ParameterValues\s*=\s*scStaticParameterValues\(\s*\[([^\]]*)\]", body
        )
        if s:
            rule.static_values = re.findall(r'"([^"]*)"', s.group(1))
        d = re.search(rf'{map_var}\.[Dd]isplayName\s*=\s*"([^"]*)"', body)
        rule.display_name = d.group(1) if d else pid

        rule.statements = re.findall(r"\bvar\s+(\w+)\s*=\s*([^;]+);", body)
        top_level, branches = _split_branches(body)
        rule.config = _parse_config(top_level, map_var)
        for new_chain, cond, block in branches:
            entries = _parse_config(block, map_var)
            if new_chain or not rule.branches:
                rule.branches.append([])
            rule.branches[-1].append((cond, entries))
        params.append(rule)
    return Rules(tables=tables, params=params)


def _parse_config(text: str, map_var: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    c = re.search(rf"{map_var}\.ConfigurationParameters\s*=\s*\[", text)
    if not c:
        return out
    cfg_text = text[c.end() - 1 : _balanced(text, c.end() - 1, "[", "]")]
    for entry in re.finditer(r"\{\s*\"(\w+)\"\s*:\s*", cfg_text):
        start = entry.end()
        depth, i = 0, start
        while i < len(cfg_text):
            ch = cfg_text[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        out.append((entry.group(1), cfg_text[start:i].strip()))
    return out


def _split_branches(body: str) -> Tuple[str, List[Tuple[bool, Optional[str], str]]]:
    """Split a function body into its top-level text and the blocks of if/else chains.

    Returns ``(top_level_text, [(condition or None for else, block_text), ...])``.
    Each chain is flattened; ``None`` conditions mean "else" of the preceding ifs.
    """
    inner = body[1:-1]
    top: List[str] = []
    branches: List[Tuple[bool, Optional[str], str]] = []
    pattern = re.compile(r"\b(?:else\s+)?if\s*\(|\belse\s*\{")
    pos = 0
    while True:
        m = pattern.search(inner, pos)
        if not m:
            top.append(inner[pos:])
            break
        top.append(inner[pos : m.start()])
        new_chain = not m.group(0).startswith("else")
        if m.group(0).startswith("else") and m.group(0).rstrip().endswith("{"):
            cond = None
            brace = m.end() - 1
        else:
            paren_end = _balanced(inner, m.end() - 1, "(", ")")
            cond = inner[m.end() : paren_end - 1].strip()
            brace = inner.index("{", paren_end)
        block_end = _balanced(inner, brace)
        branches.append((new_chain, cond, inner[brace:block_end]))
        pos = block_end
    return "".join(top), branches


# --------------------------------------------------------------------------- evaluation
class _Evaluator:
    """Whitelisted evaluator for the tiny FeatureScript expression subset used in rules."""

    def __init__(self, resolver: "_Resolver", variables: Dict[str, Any]):
        self.r = resolver
        self.vars = variables

    def eval(self, expr: str) -> Any:
        py = expr.replace("&&", " and ").replace("||", " or ")
        py = re.sub(r"!(?!=)", " not ", py)
        py = re.sub(r"\btrue\b", "True", re.sub(r"\bfalse\b", "False", py))
        if "?" in py:
            raise ValueError(f"Unsupported expression: {expr}")
        tree = ast.parse(py, mode="eval")
        return self._node(tree.body)

    def _node(self, n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str, bool)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in self.vars:
                return self.vars[n.id]
            if n.id in _UNIT_NAMES:
                return _UNIT_NAMES[n.id]
            if n.id in ("dataTables", "currentFilterMap"):
                return n.id
            if n.id in self.r.map_vars:
                return ("MAP", self.r.map_vars[n.id])
            raise ValueError(f"Unknown name '{n.id}'")
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -self._as_q(self._node(n.operand))
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = self._node(n.left), self._node(n.right)
            if isinstance(n.op, ast.Add) and isinstance(a, str) and isinstance(b, str):
                return a + b
            a, b = self._as_q(a), self._as_q(b)
            return {
                ast.Add: a.__add__,
                ast.Sub: a.__sub__,
                ast.Mult: a.__mul__,
                ast.Div: a.__truediv__,
            }[type(n.op)](b)
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            op = n.ops[0]
            a, b = self._node(n.left), self._node(n.comparators[0])
            if isinstance(op, (ast.Eq, ast.NotEq)):
                if isinstance(a, Q) or isinstance(b, Q):
                    same = abs(self._as_q(a).value - self._as_q(b).value) < 1e-12
                else:
                    same = str(a) == str(b)
                return same if isinstance(op, ast.Eq) else not same
            x, y = self._as_q(a).value, self._as_q(b).value
            if isinstance(op, ast.Lt):
                return x < y
            if isinstance(op, ast.LtE):
                return x <= y
            if isinstance(op, ast.Gt):
                return x > y
            if isinstance(op, ast.GtE):
                return x >= y
        if isinstance(n, ast.BoolOp):
            values = [bool(self._node(v)) for v in n.values]
            return all(values) if isinstance(n.op, ast.And) else any(values)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return not bool(self._node(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            args = [self._node(a) for a in n.args]
            return self.r.call(n.func.id, args)
        raise ValueError(f"Unsupported expression element: {ast.dump(n)[:80]}")

    @staticmethod
    def _as_q(v) -> Q:
        if isinstance(v, Q):
            return v
        if isinstance(v, bool):
            raise ValueError("boolean in arithmetic")
        return Q(_num(v))


class _Resolver:
    """Holds the selection state and implements the sc* helper functions."""

    def __init__(self, rules: Rules, selection: Dict[str, str]):
        self.rules = rules
        self.selection = selection  # parameterId -> value string
        self.map_vars: Dict[str, str] = {}  # map variable name -> parameterId

    def _rule(self, pid: str) -> ParamRule:
        for p in self.rules.params:
            if p.parameter_id == pid:
                return p
        raise ValueError(f"Unknown parameter {pid}")

    def row(self, table: str) -> Dict[str, str]:
        t = self.rules.tables[table]
        fields = t["fields"]
        constraints = {}
        for p in self.rules.params:
            if p.field in fields and p.parameter_id in self.selection:
                constraints[p.field] = self.selection[p.parameter_id]
        for r in t["rows"]:
            rec = dict(zip(fields, r))
            if all(str(rec.get(k)) == str(v) for k, v in constraints.items()):
                return rec
        raise ValueError(f"No row in {table} matching {constraints}")

    def call(self, fn: str, args: List[Any]) -> Any:
        if fn == "scStringToValueWithUnits":
            unit = args[1] if len(args) > 1 else Q(1.0)
            return Q(_num(args[0])) * unit
        if fn == "stringToNumber":
            return _num(args[0])
        if fn == "scGetDefaultDataValue":  # (dataTables, FIELD, map)
            pid = args[2][1]
            return self.row(self._rule(pid).table)[args[1]]
        if fn == "scGetDefaultValue":  # (map)
            return self.selection[args[0][1]]
        if fn == "scGetParameterDefaultDataValue":  # (dataTables, pid, FIELD, filter)
            return self.row(self._rule(args[1]).table)[args[2]]
        if fn == "scGetParameterDefaultFilterValue":  # (pid, filter)
            return self.selection[args[0]]
        if fn == "scGetDataValue":  # (dataTables, table, FIELD, filter?) best effort
            return self.row(args[1])[args[2]]
        if fn in ("toString",):
            return str(args[0])
        if fn in ("sqrt",):
            v = _Evaluator._as_q(args[0])
            return Q(v.value**0.5, v.unit)
        raise ValueError(f"Unsupported rule function {fn}()")


@dataclass
class ResolvedPart:
    selection: Dict[str, str]  # parameterId -> chosen value
    display: Dict[str, str]  # display name -> chosen value
    configuration: Dict[str, Any]  # configuration parameter -> Q / str / bool
    options: Dict[str, List[str]]  # display name -> available values

    def configuration_string(self) -> str:
        parts = []
        for k, v in self.configuration.items():
            if isinstance(v, Q):
                unit = {"mm": " mm", "deg": " deg", None: ""}[v.unit]
                parts.append(f"{k}={round(v.value, 9):g}{unit}")
            elif isinstance(v, bool):
                parts.append(f"{k}={'true' if v else 'false'}")
            else:
                parts.append(f"{k}={v}")
        return ";".join(parts)


def _values(rules: Rules, rule: ParamRule, selection: Dict[str, str]) -> List[str]:
    if rule.static_values is not None:
        return list(rule.static_values)
    if not rule.table or not rule.field:
        return []
    t = rules.tables[rule.table]
    fields = t["fields"]
    out: List[str] = []
    for r in t["rows"]:
        rec = dict(zip(fields, r))
        ok = all(
            str(rec.get(p.field)) == str(selection[p.parameter_id])
            for p in rules.params
            if p.parameter_id in selection and p.field in fields and p is not rule
        )
        v = str(rec.get(rule.field))
        if ok and v not in out:
            out.append(v)
    return out


def _match_choice(rule: ParamRule, choices: Dict[str, Any]) -> Optional[str]:
    for key, value in choices.items():
        k = key.lower().replace(" ", "").replace("_", "")
        names = {
            rule.parameter_id.lower(),
            rule.parameter_id.lower().removesuffix("id"),
            (rule.display_name or "").lower().replace(" ", ""),
        }
        if k in names:
            return str(value)
    return None


def resolve(rules: Rules, choices: Dict[str, Any]) -> ResolvedPart:
    """Resolve user choices (e.g. {"Size": "M3", "Length": 10}) to a configuration."""
    selection: Dict[str, str] = {}
    display: Dict[str, str] = {}
    options: Dict[str, List[str]] = {}
    configuration: Dict[str, Any] = {}
    resolver = _Resolver(rules, selection)

    for rule in rules.params:
        resolver.map_vars[rule.map_var] = rule.parameter_id
        available = _values(rules, rule, selection)
        options[rule.display_name or rule.parameter_id] = available
        wanted = _match_choice(rule, choices)
        if wanted is not None:
            match = next((v for v in available if v == wanted), None)
            if match is None:  # numeric tolerant match: "10" == "10.0"
                try:
                    match = next(v for v in available if abs(_num(v) - _num(wanted)) < 1e-9)
                except (StopIteration, ValueError):
                    match = None
            if match is None:
                raise ValueError(
                    f"'{wanted}' is not available for {rule.display_name}. "
                    f"Options: {', '.join(available[:60])}"
                )
            selection[rule.parameter_id] = match
        elif available:
            selection[rule.parameter_id] = available[0]
        display[rule.display_name or rule.parameter_id] = selection.get(rule.parameter_id, "")

        variables: Dict[str, Any] = {}
        ev = _Evaluator(resolver, variables)
        for var, expr in rule.statements:
            try:
                variables[var] = ev.eval(expr)
            except (ValueError, KeyError, SyntaxError) as e:
                variables[var] = e  # only fatal if actually used below
        entries = list(rule.config)
        for chain in rule.branches:
            for cond, branch_entries in chain:
                if cond is None or ev.eval(cond) is True:
                    entries.extend(branch_entries)
                    break
        for name, expr in entries:
            value = variables.get(expr.strip()) if expr.strip() in variables else ev.eval(expr)
            if isinstance(value, Exception):
                raise ValueError(f"Cannot compute {name}: {value}")
            configuration[name] = value
    return ResolvedPart(selection, display, configuration, options)


# --------------------------------------------------------------------------- Onshape access
@dataclass
class StandardComponent:
    id: str  # standard content document id
    component: str
    category: str
    klass: str
    standard: str


@dataclass
class ComponentSource:
    document_id: str
    version_id: str
    element_id: str
    part_id: str
    rules_text: str


class StandardContentManager:
    def __init__(self, client):
        self.client = client
        self._components: Optional[List[StandardComponent]] = None
        self._sources: Dict[str, ComponentSource] = {}

    # cache helpers --------------------------------------------------------
    @staticmethod
    def _cache_path(name: str) -> str:
        path = os.path.join(CACHE_DIR, "standard_content")
        os.makedirs(path, exist_ok=True)
        return os.path.join(path, name)

    async def list_components(self) -> List[StandardComponent]:
        if self._components is not None:
            return self._components
        path = self._cache_path("list.json")
        data = None
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < _LIST_TTL:
            with open(path) as fh:
                data = json.load(fh)
        if data is None:
            data = await self.client.get("/api/standardcontent/list")
            with open(path, "w") as fh:
                json.dump(data, fh)
        self._components = [
            StandardComponent(
                d["id"],
                d["component"],
                d.get("category", ""),
                d.get("class", ""),
                d.get("standard", ""),
            )
            for d in data
        ]
        return self._components

    async def search(
        self, query: str = "", standard: Optional[str] = None, limit: int = 40
    ) -> List[StandardComponent]:
        words = [w.lower() for w in query.split() if w]
        result = []
        for c in await self.list_components():
            if standard and c.standard.lower() != standard.lower():
                continue
            hay = f"{c.component} {c.category} {c.klass} {c.standard}".lower()
            if all(w in hay for w in words):
                result.append(c)
        # metric first (ISO, DIN) - the default in Germany
        rank = {"ISO": 0, "DIN": 1}
        result.sort(key=lambda c: (rank.get(c.standard, 2), c.component))
        return result[:limit]

    async def find(self, name_or_id: str) -> StandardComponent:
        comps = await self.list_components()
        for c in comps:
            if c.id == name_or_id or c.component.lower() == name_or_id.lower():
                return c
        hits = await self.search(name_or_id, limit=5)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise ValueError(f"No standard part matches '{name_or_id}'. Use search_standard_parts.")
        raise ValueError(
            f"'{name_or_id}' is ambiguous: "
            + "; ".join(h.component for h in hits)
            + ". Be more specific."
        )

    async def source(self, component: StandardComponent) -> ComponentSource:
        if component.id in self._sources:
            return self._sources[component.id]
        path = self._cache_path(f"{component.id}.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < _LIST_TTL:
            with open(path) as fh:
                src = ComponentSource(**json.load(fh))
        else:
            did = component.id
            versions = await self.client.get(f"/api/documents/d/{did}/versions")
            if not versions:
                raise ValueError(f"Standard part '{component.component}' has no released version")
            vid = versions[-1]["id"]
            elements = await self.client.get(f"/api/documents/d/{did}/v/{vid}/elements")
            studio = next((e for e in elements if e["elementType"] == "PARTSTUDIO"), None)
            rules = next((e for e in elements if e["elementType"] == "FEATURESTUDIO"), None)
            if studio is None or rules is None:
                raise ValueError(f"'{component.component}' is not a configurable standard part")
            parts = await self.client.get(f"/api/parts/d/{did}/v/{vid}/e/{studio['id']}")
            fs = await self.client.get(f"/api/featurestudios/d/{did}/v/{vid}/e/{rules['id']}")
            src = ComponentSource(
                did, vid, studio["id"], parts[0]["partId"], fs.get("contents", "")
            )
            with open(path, "w") as fh:
                json.dump(src.__dict__, fh)
        self._sources[component.id] = src
        return src

    async def resolve(
        self, name_or_id: str, choices: Dict[str, Any]
    ) -> Tuple[StandardComponent, ComponentSource, ResolvedPart]:
        component = await self.find(name_or_id)
        src = await self.source(component)
        try:
            rules = parse_rules(src.rules_text)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Cannot parse rules of {component.component}: {e}")
            raise ValueError(
                f"'{component.component}' uses rules this server cannot evaluate yet ({e}). "
                "Insert it via the Onshape UI (Insert > Standard content)."
            ) from e
        return component, src, resolve(rules, choices)
