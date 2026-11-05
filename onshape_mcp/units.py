"""Central unit handling.

All length values that tools accept or report use ONE configurable length unit.
The default is millimetres (German/ISO convention). Angles are always degrees,
masses kilograms. Onshape's REST API itself works in SI (metres, radians).

Configure via the environment variable ``ONSHAPE_LENGTH_UNIT``
(``mm`` | ``cm`` | ``m`` | ``in``), e.g. in the project's ``.env`` file.
"""

from __future__ import annotations

import os
from typing import Iterable, Tuple

# metres per unit
_METERS_PER_UNIT = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "in": 0.0254,
}

# Human readable names used in tool descriptions
_UNIT_NAMES = {
    "mm": "millimeters (mm)",
    "cm": "centimeters (cm)",
    "m": "meters (m)",
    "in": "inches (in)",
}

DEFAULT_LENGTH_UNIT = "mm"

_length_unit = DEFAULT_LENGTH_UNIT


def _normalize(unit: str) -> str:
    u = unit.strip().lower()
    aliases = {
        "millimeter": "mm",
        "millimeters": "mm",
        "millimetre": "mm",
        "centimeter": "cm",
        "centimeters": "cm",
        "meter": "m",
        "meters": "m",
        "metre": "m",
        "inch": "in",
        "inches": "in",
    }
    u = aliases.get(u, u)
    if u not in _METERS_PER_UNIT:
        raise ValueError(
            f"Unsupported length unit '{unit}'. Use one of: {', '.join(_METERS_PER_UNIT)}"
        )
    return u


def set_length_unit(unit: str) -> None:
    """Set the active length unit (mm, cm, m, in)."""
    global _length_unit
    _length_unit = _normalize(unit)


def get_length_unit() -> str:
    """Return the active length unit symbol, e.g. ``"mm"``."""
    return _length_unit


def length_unit_name() -> str:
    """Return a descriptive name of the active unit for tool descriptions."""
    return _UNIT_NAMES[_length_unit]


def load_from_env() -> None:
    """Initialise the unit from ``ONSHAPE_LENGTH_UNIT`` (falls back to mm)."""
    set_length_unit(os.getenv("ONSHAPE_LENGTH_UNIT", DEFAULT_LENGTH_UNIT))


def to_m(value: float) -> float:
    """Convert a value in the active length unit to metres."""
    return value * _METERS_PER_UNIT[_length_unit]


def from_m(value: float) -> float:
    """Convert metres to the active length unit."""
    return value / _METERS_PER_UNIT[_length_unit]


def to_m_xyz(xyz: Iterable[float]) -> Tuple[float, ...]:
    return tuple(to_m(v) for v in xyz)


def from_m_xyz(xyz: Iterable[float]) -> Tuple[float, ...]:
    return tuple(from_m(v) for v in xyz)


def length_expr(value: float) -> str:
    """Return an Onshape expression string such as ``"12.5 mm"``."""
    return f"{value:g} {_length_unit}"


def fmt(value: float, decimals: int | None = None) -> str:
    """Format a length (already in the active unit) with its unit symbol."""
    if decimals is None:
        decimals = {"mm": 2, "cm": 3, "m": 4, "in": 3}[_length_unit]
    return f"{value:.{decimals}f} {_length_unit}"


def fmt_m(value_m: float, decimals: int | None = None) -> str:
    """Format a value given in metres in the active unit with its symbol."""
    return fmt(from_m(value_m), decimals)


def volume_unit() -> str:
    return f"{_length_unit}³"


load_from_env()


def documents_public_by_default() -> bool:
    """Whether new documents are created public.

    Education and paid plans can create private documents (default: private).
    Free plans can only create public ones - set ``ONSHAPE_DOCUMENTS_PUBLIC=true``.
    """
    return os.getenv("ONSHAPE_DOCUMENTS_PUBLIC", "false").strip().lower() in {"1", "true", "yes"}
