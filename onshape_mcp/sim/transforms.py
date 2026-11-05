"""Tiny rigid-transform helpers (pure Python, no numpy needed)."""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

Mat3 = List[List[float]]
Vec3 = Tuple[float, float, float]


def rpy_to_matrix(r: float, p: float, y: float) -> Mat3:
    """URDF roll-pitch-yaw (fixed axes X, Y, Z) -> rotation matrix."""
    cr, sr, cp, sp, cy, sy = (
        math.cos(r),
        math.sin(r),
        math.cos(p),
        math.sin(p),
        math.cos(y),
        math.sin(y),
    )
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def matmul(a: Mat3, b: Mat3) -> Mat3:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def transpose(a: Mat3) -> Mat3:
    return [[a[j][i] for j in range(3)] for i in range(3)]


def apply(a: Mat3, v: Sequence[float]) -> Vec3:
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))  # type: ignore[return-value]


class Pose:
    """Rigid transform: x_world = R @ x_local + p."""

    def __init__(self, R: Mat3 = None, p: Sequence[float] = (0.0, 0.0, 0.0)):
        self.R = R or [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.p = tuple(float(c) for c in p)

    @classmethod
    def from_urdf(cls, xyz: Sequence[float], rpy: Sequence[float]) -> "Pose":
        return cls(rpy_to_matrix(*rpy), xyz)

    def __mul__(self, other: "Pose") -> "Pose":
        return Pose(
            matmul(self.R, other.R), tuple(a + b for a, b in zip(apply(self.R, other.p), self.p))
        )

    def inverse(self) -> "Pose":
        rt = transpose(self.R)
        return Pose(rt, tuple(-c for c in apply(rt, self.p)))

    def point(self, v: Sequence[float]) -> Vec3:
        return tuple(a + b for a, b in zip(apply(self.R, v), self.p))  # type: ignore[return-value]

    def vector(self, v: Sequence[float]) -> Vec3:
        return apply(self.R, v)

    def quat(self) -> Tuple[float, float, float, float]:
        return matrix_to_quat(self.R)


def matrix_to_quat(R: Mat3) -> Tuple[float, float, float, float]:
    """Rotation matrix -> unit quaternion (w, x, y, z), MuJoCo convention."""
    t = R[0][0] + R[1][1] + R[2][2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        w, x, y, z = (
            0.25 * s,
            (R[2][1] - R[1][2]) / s,
            (R[0][2] - R[2][0]) / s,
            (R[1][0] - R[0][1]) / s,
        )
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = math.sqrt(1.0 + R[0][0] - R[1][1] - R[2][2]) * 2
        w, x, y, z = (
            (R[2][1] - R[1][2]) / s,
            0.25 * s,
            (R[0][1] + R[1][0]) / s,
            (R[0][2] + R[2][0]) / s,
        )
    elif R[1][1] > R[2][2]:
        s = math.sqrt(1.0 + R[1][1] - R[0][0] - R[2][2]) * 2
        w, x, y, z = (
            (R[0][2] - R[2][0]) / s,
            (R[0][1] + R[1][0]) / s,
            0.25 * s,
            (R[1][2] + R[2][1]) / s,
        )
    else:
        s = math.sqrt(1.0 + R[2][2] - R[0][0] - R[1][1]) * 2
        w, x, y, z = (
            (R[1][0] - R[0][1]) / s,
            (R[0][2] + R[2][0]) / s,
            (R[1][2] + R[2][1]) / s,
            0.25 * s,
        )
    n = math.sqrt(w * w + x * x + y * y + z * z)
    q = (w / n, x / n, y / n, z / n)
    return q if q[0] >= 0 else tuple(-c for c in q)  # type: ignore[return-value]


def fmt(values: Sequence[float], digits: int = 9) -> str:
    out = []
    for v in values:
        v = 0.0 if abs(v) < 10 ** (-digits) else v
        out.append(f"{v:.{digits}g}")
    return " ".join(out)


def normalize(v: Sequence[float]) -> Vec3:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)  # type: ignore[return-value]
