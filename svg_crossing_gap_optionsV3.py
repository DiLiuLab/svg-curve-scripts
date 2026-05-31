#!/usr/bin/env python3
"""
Convert an SVG containing one or more stroked curves into a knot/link-style SVG
with editable crossing choices.

What it does:
  1. Reads path elements from an input SVG.
  2. Flattens the paths to detect self-crossings and crossings between curves.
  3. Cuts real gaps around every detected crossing on BOTH involved strands.
  4. Adds two editable overpass candidates for each crossing:
       - choice A: strand/curve A goes over
       - choice B: strand/curve B goes over
  5. Keeps the original continuous graph as a separate reference object.
  6. Optionally adds short direction-guide curves between adjacent crossings,
     so arrowheads can later be applied in Adobe Illustrator.
  7. Can run in GUI mode. With no command-line arguments, or with --gui, a
     parameter-entry window is opened.

Inputs:
  An SVG file containing paths. Best results are obtained from simple stroked
  paths with no fill. The parser supports M/L/H/V/C/S/Q/T/Z path commands.
  It does not currently support elliptical arc A/a commands exactly; arcs are
  conservatively approximated as straight lines, so convert arcs to cubic paths
  in Illustrator first for best results.

Output:
  A new SVG containing:
    - original_graph_reference: original continuous curves, hidden by default
    - base_broken_strands: the original curves with real gaps at crossings
    - crossing_options: two possible overpass segments for each crossing
    - direction_guides: optional short curves for adding arrows in Illustrator
    - crossing_labels: optional crossing numbers

Examples:
  python svg_crossing_gap_optionsV3.py input.svg
  python svg_crossing_gap_optionsV3.py input.svg --output input_crossing_gap.svg
  python svg_crossing_gap_optionsV3.py input.svg --gap-radius-px 12 --add-direction-guides
  python svg_crossing_gap_optionsV3.py --gui

Notes:
  This is a practical Illustrator-preparation tool, not an exact symbolic
  Bezier intersection engine. Crossings are detected by polyline approximation,
  then the original cubic Bezier pieces are split to create editable SVG paths.
"""

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


COMMAND_RE = re.compile(
    r"[MmZzLlHhVvCcSsQqTtAa]|"
    r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
)

SVG_NS = "http://www.w3.org/2000/svg"


@dataclass
class CubicSegment:
    p0: tuple
    p1: tuple
    p2: tuple
    p3: tuple


@dataclass
class Curve:
    curve_id: int
    source_id: str
    segments: list
    closed: bool = False
    samples: list = field(default_factory=list)
    segment_sample_s: list = field(default_factory=list)
    segment_lengths: list = field(default_factory=list)
    total_length: float = 0.0


@dataclass
class RawCrossing:
    x: float
    y: float
    curve_a: int
    s_a: float
    curve_b: int
    s_b: float


@dataclass
class Crossing:
    crossing_id: int
    x: float
    y: float
    curve_a: int
    s_a: float
    curve_b: int
    s_b: float


def strip_namespace(tag):
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_number_list(text):
    return [float(x) for x in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", text)]


def matrix_identity():
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def matrix_multiply(m1, m2):
    """
    Return m1 @ m2 for SVG affine matrices.

    Matrix form:
      [a c e]
      [b d f]
      [0 0 1]
    """
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2

    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply_matrix(point, matrix):
    x, y = point
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def parse_transform(transform_text):
    """Parse common SVG transforms into one affine matrix."""
    if not transform_text:
        return matrix_identity()

    transform_re = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
    total = matrix_identity()

    for name, arg_text in transform_re.findall(transform_text):
        args = parse_number_list(arg_text)

        if name == "matrix":
            if len(args) != 6:
                continue
            m = tuple(args)

        elif name == "translate":
            tx = args[0] if len(args) >= 1 else 0.0
            ty = args[1] if len(args) >= 2 else 0.0
            m = (1.0, 0.0, 0.0, 1.0, tx, ty)

        elif name == "scale":
            sx = args[0] if len(args) >= 1 else 1.0
            sy = args[1] if len(args) >= 2 else sx
            m = (sx, 0.0, 0.0, sy, 0.0, 0.0)

        elif name == "rotate":
            angle = math.radians(args[0] if args else 0.0)
            cos_a = math.cos(angle)
            sin_a = math.sin(angle)
            r = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)

            if len(args) >= 3:
                cx, cy = args[1], args[2]
                t1 = (1.0, 0.0, 0.0, 1.0, cx, cy)
                t2 = (1.0, 0.0, 0.0, 1.0, -cx, -cy)
                m = matrix_multiply(t1, matrix_multiply(r, t2))
            else:
                m = r

        elif name == "skewX":
            angle = math.radians(args[0] if args else 0.0)
            m = (1.0, 0.0, math.tan(angle), 1.0, 0.0, 0.0)

        elif name == "skewY":
            angle = math.radians(args[0] if args else 0.0)
            m = (1.0, math.tan(angle), 0.0, 1.0, 0.0, 0.0)

        else:
            continue

        # Applies transforms in the listed order for the point convention used here.
        total = matrix_multiply(m, total)

    return total


def tokenize_path(d):
    return COMMAND_RE.findall(d)


def is_command(token):
    return len(token) == 1 and token.isalpha()


def to_float(token):
    return float(token)


def point_add(a, b):
    return a[0] + b[0], a[1] + b[1]


def point_sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def point_mul(a, s):
    return a[0] * s, a[1] * s


def reflect(point, around):
    return 2.0 * around[0] - point[0], 2.0 * around[1] - point[1]


def line_to_cubic(p0, p1):
    c1 = (p0[0] + (p1[0] - p0[0]) / 3.0, p0[1] + (p1[1] - p0[1]) / 3.0)
    c2 = (p0[0] + 2.0 * (p1[0] - p0[0]) / 3.0, p0[1] + 2.0 * (p1[1] - p0[1]) / 3.0)
    return CubicSegment(p0, c1, c2, p1)


def quadratic_to_cubic(p0, q, p1):
    c1 = (p0[0] + 2.0 * (q[0] - p0[0]) / 3.0, p0[1] + 2.0 * (q[1] - p0[1]) / 3.0)
    c2 = (p1[0] + 2.0 * (q[0] - p1[0]) / 3.0, p1[1] + 2.0 * (q[1] - p1[1]) / 3.0)
    return CubicSegment(p0, c1, c2, p1)


def parse_svg_path_to_curves(d, matrix, source_id, start_curve_id):
    """
    Parse an SVG path into one or more Curve objects.

    Elliptical arcs A/a are intentionally not supported. If found, a warning is
    printed and the command is skipped in a conservative way.
    """
    tokens = tokenize_path(d)
    index = 0
    command = None

    current = (0.0, 0.0)
    start_point = (0.0, 0.0)
    previous_cubic_control = None
    previous_quadratic_control = None
    previous_command = None

    current_segments = []
    current_closed = False
    curves = []
    next_curve_id = start_curve_id

    def finalize_current_curve():
        nonlocal current_segments, current_closed, curves, next_curve_id
        if current_segments:
            transformed_segments = []
            for seg in current_segments:
                transformed_segments.append(
                    CubicSegment(
                        apply_matrix(seg.p0, matrix),
                        apply_matrix(seg.p1, matrix),
                        apply_matrix(seg.p2, matrix),
                        apply_matrix(seg.p3, matrix),
                    )
                )
            curves.append(
                Curve(
                    curve_id=next_curve_id,
                    source_id=source_id,
                    segments=transformed_segments,
                    closed=current_closed,
                )
            )
            next_curve_id += 1
        current_segments = []
        current_closed = False

    def read_point(relative=False):
        nonlocal index, current
        x = to_float(tokens[index])
        y = to_float(tokens[index + 1])
        index += 2
        p = (x, y)
        if relative:
            p = point_add(current, p)
        return p

    while index < len(tokens):
        if is_command(tokens[index]):
            command = tokens[index]
            index += 1
        elif command is None:
            raise ValueError("Path data starts with numbers before any SVG command.")

        cmd = command
        relative = cmd.islower()
        upper = cmd.upper()

        if upper == "M":
            p = read_point(relative=relative)
            finalize_current_curve()
            current = p
            start_point = p
            previous_cubic_control = None
            previous_quadratic_control = None
            previous_command = "M"

            # Additional coordinate pairs after M are implicit L commands.
            command = "l" if relative else "L"
            while index < len(tokens) and not is_command(tokens[index]):
                p1 = read_point(relative=relative)
                current_segments.append(line_to_cubic(current, p1))
                current = p1
                previous_command = "L"

        elif upper == "L":
            while index < len(tokens) and not is_command(tokens[index]):
                p1 = read_point(relative=relative)
                current_segments.append(line_to_cubic(current, p1))
                current = p1
                previous_cubic_control = None
                previous_quadratic_control = None
                previous_command = "L"

        elif upper == "H":
            while index < len(tokens) and not is_command(tokens[index]):
                x = to_float(tokens[index])
                index += 1
                if relative:
                    x = current[0] + x
                p1 = (x, current[1])
                current_segments.append(line_to_cubic(current, p1))
                current = p1
                previous_cubic_control = None
                previous_quadratic_control = None
                previous_command = "H"

        elif upper == "V":
            while index < len(tokens) and not is_command(tokens[index]):
                y = to_float(tokens[index])
                index += 1
                if relative:
                    y = current[1] + y
                p1 = (current[0], y)
                current_segments.append(line_to_cubic(current, p1))
                current = p1
                previous_cubic_control = None
                previous_quadratic_control = None
                previous_command = "V"

        elif upper == "C":
            while index < len(tokens) and not is_command(tokens[index]):
                c1 = read_point(relative=relative)
                c2 = read_point(relative=relative)
                p1 = read_point(relative=relative)
                current_segments.append(CubicSegment(current, c1, c2, p1))
                current = p1
                previous_cubic_control = c2
                previous_quadratic_control = None
                previous_command = "C"

        elif upper == "S":
            while index < len(tokens) and not is_command(tokens[index]):
                if previous_command in ("C", "S") and previous_cubic_control is not None:
                    c1 = reflect(previous_cubic_control, current)
                else:
                    c1 = current
                c2 = read_point(relative=relative)
                p1 = read_point(relative=relative)
                current_segments.append(CubicSegment(current, c1, c2, p1))
                current = p1
                previous_cubic_control = c2
                previous_quadratic_control = None
                previous_command = "S"

        elif upper == "Q":
            while index < len(tokens) and not is_command(tokens[index]):
                q = read_point(relative=relative)
                p1 = read_point(relative=relative)
                current_segments.append(quadratic_to_cubic(current, q, p1))
                current = p1
                previous_cubic_control = None
                previous_quadratic_control = q
                previous_command = "Q"

        elif upper == "T":
            while index < len(tokens) and not is_command(tokens[index]):
                if previous_command in ("Q", "T") and previous_quadratic_control is not None:
                    q = reflect(previous_quadratic_control, current)
                else:
                    q = current
                p1 = read_point(relative=relative)
                current_segments.append(quadratic_to_cubic(current, q, p1))
                current = p1
                previous_cubic_control = None
                previous_quadratic_control = q
                previous_command = "T"

        elif upper == "Z":
            if distance(current, start_point) > 1e-9:
                current_segments.append(line_to_cubic(current, start_point))
            current = start_point
            current_closed = True
            previous_cubic_control = None
            previous_quadratic_control = None
            previous_command = "Z"

        elif upper == "A":
            # Conservative skip of arc parameters: rx ry x-axis-rotation large-arc-flag sweep-flag x y
            print(
                "Warning: A/a elliptical arc command found in {}. "
                "This script currently skips arcs. Convert arcs to cubic paths first in Illustrator.".format(source_id),
                file=sys.stderr,
            )
            while index < len(tokens) and not is_command(tokens[index]):
                if index + 6 >= len(tokens):
                    break
                # Move to the arc end point so the parser can continue.
                rx = to_float(tokens[index])
                ry = to_float(tokens[index + 1])
                xrot = to_float(tokens[index + 2])
                large = to_float(tokens[index + 3])
                sweep = to_float(tokens[index + 4])
                x = to_float(tokens[index + 5])
                y = to_float(tokens[index + 6])
                index += 7
                p1 = (x, y)
                if relative:
                    p1 = point_add(current, p1)
                # Approximate skipped arc as a straight line so downstream logic still runs.
                current_segments.append(line_to_cubic(current, p1))
                current = p1
                previous_command = "A"

        else:
            raise ValueError("Unsupported SVG path command: {}".format(cmd))

    finalize_current_curve()
    return curves, next_curve_id


def cubic_point(seg, u):
    mt = 1.0 - u
    x = (
        mt ** 3 * seg.p0[0]
        + 3.0 * mt ** 2 * u * seg.p1[0]
        + 3.0 * mt * u ** 2 * seg.p2[0]
        + u ** 3 * seg.p3[0]
    )
    y = (
        mt ** 3 * seg.p0[1]
        + 3.0 * mt ** 2 * u * seg.p1[1]
        + 3.0 * mt * u ** 2 * seg.p2[1]
        + u ** 3 * seg.p3[1]
    )
    return x, y


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def approximate_cubic_length(seg):
    return (
        distance(seg.p0, seg.p1)
        + distance(seg.p1, seg.p2)
        + distance(seg.p2, seg.p3)
    )


def flatten_curve(curve, sample_step_px):
    """Create polyline samples and per-segment arc-length maps for a curve."""
    samples = []
    segment_sample_s = []
    segment_lengths = []
    total_s = 0.0

    for seg_index, seg in enumerate(curve.segments):
        approx_len = approximate_cubic_length(seg)
        n = max(6, int(math.ceil(approx_len / max(sample_step_px, 0.5))))

        seg_samples = []
        previous = cubic_point(seg, 0.0)

        if not samples:
            samples.append(
                {
                    "x": previous[0],
                    "y": previous[1],
                    "s": total_s,
                    "seg_index": seg_index,
                    "u": 0.0,
                }
            )

        seg_samples.append((0.0, total_s))

        for j in range(1, n + 1):
            u = float(j) / float(n)
            point = cubic_point(seg, u)
            total_s += distance(previous, point)
            previous = point
            samples.append(
                {
                    "x": point[0],
                    "y": point[1],
                    "s": total_s,
                    "seg_index": seg_index,
                    "u": u,
                }
            )
            seg_samples.append((u, total_s))

        segment_lengths.append(seg_samples[-1][1] - seg_samples[0][1])
        segment_sample_s.append(seg_samples)

    curve.samples = samples
    curve.segment_sample_s = segment_sample_s
    curve.segment_lengths = segment_lengths
    curve.total_length = total_s


def cross2(a, b):
    return a[0] * b[1] - a[1] * b[0]


def segment_intersection(p, p2, q, q2):
    """
    Return (x, y, t, u) if two line segments intersect, otherwise None.

    p + t*(p2-p) intersects q + u*(q2-q)
    """
    r = (p2[0] - p[0], p2[1] - p[1])
    s = (q2[0] - q[0], q2[1] - q[1])
    denom = cross2(r, s)

    if abs(denom) < 1e-12:
        return None

    qp = (q[0] - p[0], q[1] - p[1])
    t = cross2(qp, s) / denom
    u = cross2(qp, r) / denom

    eps = 1e-9
    if -eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps:
        x = p[0] + t * r[0]
        y = p[1] + t * r[1]
        return x, y, max(0.0, min(1.0, t)), max(0.0, min(1.0, u))

    return None


def build_flat_line_segments(curves):
    flat_segments = []
    for curve in curves:
        for i in range(len(curve.samples) - 1):
            a = curve.samples[i]
            b = curve.samples[i + 1]
            if distance((a["x"], a["y"]), (b["x"], b["y"])) < 1e-9:
                continue
            flat_segments.append(
                {
                    "curve_id": curve.curve_id,
                    "flat_index": i,
                    "x0": a["x"],
                    "y0": a["y"],
                    "x1": b["x"],
                    "y1": b["y"],
                    "s0": a["s"],
                    "s1": b["s"],
                }
            )
    return flat_segments


def same_curve_neighbors(seg_a, seg_b, curves_by_id):
    if seg_a["curve_id"] != seg_b["curve_id"]:
        return False

    curve = curves_by_id[seg_a["curve_id"]]
    i = seg_a["flat_index"]
    j = seg_b["flat_index"]

    if abs(i - j) <= 1:
        return True

    if curve.closed:
        n = len(curve.samples) - 1
        if {i, j} == {0, n - 1}:
            return True

    return False


def find_raw_crossings(curves, min_self_separation_px):
    curves_by_id = {curve.curve_id: curve for curve in curves}
    flat_segments = build_flat_line_segments(curves)
    raw = []

    for i in range(len(flat_segments)):
        seg_a = flat_segments[i]
        p0 = (seg_a["x0"], seg_a["y0"])
        p1 = (seg_a["x1"], seg_a["y1"])

        for j in range(i + 1, len(flat_segments)):
            seg_b = flat_segments[j]

            if same_curve_neighbors(seg_a, seg_b, curves_by_id):
                continue

            q0 = (seg_b["x0"], seg_b["y0"])
            q1 = (seg_b["x1"], seg_b["y1"])

            result = segment_intersection(p0, p1, q0, q1)
            if result is None:
                continue

            x, y, ta, tb = result
            s_a = seg_a["s0"] + ta * (seg_a["s1"] - seg_a["s0"])
            s_b = seg_b["s0"] + tb * (seg_b["s1"] - seg_b["s0"])

            curve_a_id = seg_a["curve_id"]
            curve_b_id = seg_b["curve_id"]

            if curve_a_id == curve_b_id:
                curve = curves_by_id[curve_a_id]
                ds = abs(s_a - s_b)
                if curve.closed:
                    ds = min(ds, curve.total_length - ds)
                if ds < min_self_separation_px:
                    continue

            # Deterministic ordering.
            if (curve_b_id, s_b) < (curve_a_id, s_a):
                curve_a_id, curve_b_id = curve_b_id, curve_a_id
                s_a, s_b = s_b, s_a

            raw.append(RawCrossing(x, y, curve_a_id, s_a, curve_b_id, s_b))

    return raw


def arc_length_distance(curve, s1, s2):
    """Distance between two arc-length positions on an open or closed curve."""
    d = abs(s1 - s2)
    if curve.closed and curve.total_length > 0.0:
        d = min(d, curve.total_length - d)
    return d


def circular_arc_length_mean(curve, values):
    """Average arc-length positions, respecting the seam of a closed curve."""
    if not values:
        return 0.0

    if not curve.closed or curve.total_length <= 0.0:
        return sum(values) / float(len(values))

    total = curve.total_length
    sin_sum = 0.0
    cos_sum = 0.0

    for value in values:
        angle = 2.0 * math.pi * (value % total) / total
        sin_sum += math.sin(angle)
        cos_sum += math.cos(angle)

    angle = math.atan2(sin_sum, cos_sum)
    if angle < 0.0:
        angle += 2.0 * math.pi

    result = total * angle / (2.0 * math.pi)

    # Keep seam crossings numerically at 0 rather than total_length.
    if result > total - 1e-6 or result < 1e-6:
        result = 0.0

    return result


def oriented_raw_against_reference(raw, reference, curves_by_id):
    """
    Return raw's parameters oriented to match reference's branch order.

    For crossings between two different curves, the order is fixed by curve id.
    For self-crossings, the two branches are unordered, so this function decides
    whether raw.s_a/raw.s_b or raw.s_b/raw.s_a best matches the reference.
    Closed-curve seams are compared with circular arc-length distance, so s=0
    and s=total_length are treated as the same point.
    """
    if raw.curve_a != reference.curve_a or raw.curve_b != reference.curve_b:
        return None

    if raw.curve_a != raw.curve_b:
        return raw.s_a, raw.s_b

    curve = curves_by_id[raw.curve_a]

    same = (
        arc_length_distance(curve, raw.s_a, reference.s_a)
        + arc_length_distance(curve, raw.s_b, reference.s_b)
    )
    swapped = (
        arc_length_distance(curve, raw.s_b, reference.s_a)
        + arc_length_distance(curve, raw.s_a, reference.s_b)
    )

    if swapped < same:
        return raw.s_b, raw.s_a

    return raw.s_a, raw.s_b


def raw_crossing_matches_cluster(raw, reference, curves_by_id, cluster_px, param_cluster_px):
    """Decide whether a raw polyline intersection belongs to an existing cluster."""
    if raw.curve_a != reference.curve_a or raw.curve_b != reference.curve_b:
        return False

    if math.hypot(raw.x - reference.x, raw.y - reference.y) > cluster_px:
        return False

    oriented = oriented_raw_against_reference(raw, reference, curves_by_id)
    if oriented is None:
        return False

    s_a, s_b = oriented

    curve_a = curves_by_id[reference.curve_a]
    curve_b = curves_by_id[reference.curve_b]

    return (
        arc_length_distance(curve_a, s_a, reference.s_a) <= param_cluster_px
        and arc_length_distance(curve_b, s_b, reference.s_b) <= param_cluster_px
    )


def cluster_crossings(raw_crossings, curves_by_id, cluster_px, param_cluster_px=None):
    """
    Cluster duplicate polyline detections that correspond to the same crossing.

    V2 fix: the original version clustered only by xy position and curve ids.
    That failed when a closed path had its seam exactly at a crossing: the same
    physical branch appeared as both s=0 and s=total_length, and averaging those
    numbers moved the overpass segment to the wrong place.

    This version also clusters by the two branch arc-length positions, using
    circular distance for closed curves. Thus s=0 and s=total_length are treated
    as identical, while truly different branch pairs at the same xy position are
    not averaged together.
    """
    if param_cluster_px is None:
        param_cluster_px = max(2.0 * cluster_px, 8.0)

    clusters = []

    for raw in raw_crossings:
        placed = False

        for cluster in clusters:
            reference = cluster[0]
            if raw_crossing_matches_cluster(
                raw,
                reference,
                curves_by_id,
                cluster_px=cluster_px,
                param_cluster_px=param_cluster_px,
            ):
                cluster.append(raw)
                placed = True
                break

        if not placed:
            clusters.append([raw])

    crossings = []

    for idx, cluster in enumerate(clusters, start=1):
        reference = cluster[0]
        curve_a = reference.curve_a
        curve_b = reference.curve_b

        x = sum(c.x for c in cluster) / float(len(cluster))
        y = sum(c.y for c in cluster) / float(len(cluster))

        oriented_s_a = []
        oriented_s_b = []

        for raw in cluster:
            s_a, s_b = oriented_raw_against_reference(raw, reference, curves_by_id)
            oriented_s_a.append(s_a)
            oriented_s_b.append(s_b)

        s_a = circular_arc_length_mean(curves_by_id[curve_a], oriented_s_a)
        s_b = circular_arc_length_mean(curves_by_id[curve_b], oriented_s_b)

        crossings.append(Crossing(idx, x, y, curve_a, s_a, curve_b, s_b))

    crossings.sort(key=lambda c: (c.y, c.x))
    for idx, crossing in enumerate(crossings, start=1):
        crossing.crossing_id = idx

    return crossings


def merge_intervals(intervals):
    if not intervals:
        return []

    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]

    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def normalize_hidden_intervals(intervals, total_length, closed):
    normalized = []

    for start, end in intervals:
        if end <= start:
            continue

        if closed:
            # Split wrapping intervals into pieces inside [0, total_length].
            while start < 0.0:
                start += total_length
                end += total_length
            while start >= total_length:
                start -= total_length
                end -= total_length

            if end <= total_length:
                normalized.append((start, end))
            else:
                normalized.append((start, total_length))
                normalized.append((0.0, end - total_length))
        else:
            start = max(0.0, start)
            end = min(total_length, end)
            if end > start:
                normalized.append((start, end))

    return merge_intervals(normalized)


def complement_intervals(hidden, total_length, closed):
    if not hidden:
        return [(0.0, total_length)]

    hidden = merge_intervals(hidden)
    visible = []

    if closed:
        if len(hidden) == 1 and hidden[0][0] <= 0.0 and hidden[0][1] >= total_length:
            return []

        # Intervals between hidden intervals. The last one may wrap by adding total_length.
        for i in range(len(hidden)):
            current_end = hidden[i][1]
            next_start = hidden[(i + 1) % len(hidden)][0]
            if i == len(hidden) - 1:
                next_start += total_length
            if next_start > current_end:
                visible.append((current_end, next_start))
    else:
        if hidden[0][0] > 0.0:
            visible.append((0.0, hidden[0][0]))

        for i in range(len(hidden) - 1):
            if hidden[i + 1][0] > hidden[i][1]:
                visible.append((hidden[i][1], hidden[i + 1][0]))

        if hidden[-1][1] < total_length:
            visible.append((hidden[-1][1], total_length))

    return visible


def lerp(a, b, u):
    return a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u


def split_cubic(seg, u):
    p0, p1, p2, p3 = seg.p0, seg.p1, seg.p2, seg.p3

    p01 = lerp(p0, p1, u)
    p12 = lerp(p1, p2, u)
    p23 = lerp(p2, p3, u)

    p012 = lerp(p01, p12, u)
    p123 = lerp(p12, p23, u)

    p0123 = lerp(p012, p123, u)

    left = CubicSegment(p0, p01, p012, p0123)
    right = CubicSegment(p0123, p123, p23, p3)
    return left, right


def sub_cubic(seg, u0, u1):
    eps = 1e-9
    u0 = max(0.0, min(1.0, u0))
    u1 = max(0.0, min(1.0, u1))

    if u1 <= u0:
        raise ValueError("u1 must be greater than u0.")

    if abs(u0) < eps and abs(u1 - 1.0) < eps:
        return seg

    if u1 < 1.0 - eps:
        left, _ = split_cubic(seg, u1)
    else:
        left = seg

    if u0 <= eps:
        return left

    _, middle = split_cubic(left, u0 / u1)
    return middle


def find_segment_index_for_s(curve, s):
    if s <= 0.0:
        return 0
    if s >= curve.total_length:
        return len(curve.segments) - 1

    running = 0.0
    for i, seg_len in enumerate(curve.segment_lengths):
        if running <= s <= running + seg_len:
            return i
        running += seg_len

    return len(curve.segments) - 1


def s_to_u_in_segment(curve, seg_index, s):
    samples = curve.segment_sample_s[seg_index]

    if s <= samples[0][1]:
        return 0.0
    if s >= samples[-1][1]:
        return 1.0

    for i in range(len(samples) - 1):
        u0, s0 = samples[i]
        u1, s1 = samples[i + 1]
        if s0 <= s <= s1:
            if abs(s1 - s0) < 1e-12:
                return u0
            f = (s - s0) / (s1 - s0)
            return u0 + f * (u1 - u0)

    return 1.0


def fmt_point(point):
    return "{:.2f},{:.2f}".format(point[0], point[1])


def append_curve_interval(path_parts, curve, s0, s1, move=True):
    """Append cubic path commands for one non-wrapping interval."""
    if s1 <= s0:
        return

    s0 = max(0.0, min(curve.total_length, s0))
    s1 = max(0.0, min(curve.total_length, s1))

    if s1 <= s0:
        return

    i0 = find_segment_index_for_s(curve, s0)
    i1 = find_segment_index_for_s(curve, s1)

    need_move = move

    running_start = 0.0
    seg_start_s = []
    for length in curve.segment_lengths:
        seg_start_s.append(running_start)
        running_start += length

    for seg_index in range(i0, i1 + 1):
        seg_global_start = seg_start_s[seg_index]
        seg_global_end = seg_global_start + curve.segment_lengths[seg_index]

        start = max(s0, seg_global_start)
        end = min(s1, seg_global_end)

        if end <= start + 1e-8:
            continue

        u0 = s_to_u_in_segment(curve, seg_index, start)
        u1 = s_to_u_in_segment(curve, seg_index, end)

        if u1 <= u0 + 1e-9:
            continue

        seg_piece = sub_cubic(curve.segments[seg_index], u0, u1)

        if need_move:
            path_parts.append("M {}".format(fmt_point(seg_piece.p0)))
            need_move = False

        path_parts.append(
            "C {} {} {}".format(
                fmt_point(seg_piece.p1),
                fmt_point(seg_piece.p2),
                fmt_point(seg_piece.p3),
            )
        )


def path_for_curve_interval(curve, s0, s1):
    """
    Build SVG path data for an interval.

    For closed curves, s1 may exceed total_length to indicate wraparound.
    """
    path_parts = []
    total = curve.total_length

    if s1 <= total:
        append_curve_interval(path_parts, curve, s0, s1, move=True)
    else:
        append_curve_interval(path_parts, curve, s0, total, move=True)
        # Continue the same subpath across the closed-curve seam. This is useful
        # for overpass segments centered exactly at the start/end point.
        append_curve_interval(path_parts, curve, 0.0, s1 - total, move=False)

    return " ".join(path_parts)


def interval_around_s(curve, s, radius_px):
    if curve.closed:
        return (s - radius_px, s + radius_px)

    return (max(0.0, s - radius_px), min(curve.total_length, s + radius_px))


def build_hidden_intervals_by_curve(curves, crossings, gap_radius_px):
    hidden_by_curve = {curve.curve_id: [] for curve in curves}

    for crossing in crossings:
        hidden_by_curve[crossing.curve_a].append(
            interval_around_s(
                next(c for c in curves if c.curve_id == crossing.curve_a),
                crossing.s_a,
                gap_radius_px,
            )
        )
        hidden_by_curve[crossing.curve_b].append(
            interval_around_s(
                next(c for c in curves if c.curve_id == crossing.curve_b),
                crossing.s_b,
                gap_radius_px,
            )
        )

    return hidden_by_curve


def build_visible_path_for_curve(curve, hidden_intervals):
    hidden = normalize_hidden_intervals(
        hidden_intervals,
        total_length=curve.total_length,
        closed=curve.closed,
    )
    visible = complement_intervals(
        hidden,
        total_length=curve.total_length,
        closed=curve.closed,
    )

    parts = []
    for s0, s1 in visible:
        d = path_for_curve_interval(curve, s0, s1)
        if d:
            parts.append(d)

    return " ".join(parts)


def build_overpass_path(curve, s, radius_px):
    s0, s1 = interval_around_s(curve, s, radius_px)

    if curve.closed:
        # Normalize into the [0, total_length] domain but allow wrap.
        while s0 < 0.0:
            s0 += curve.total_length
            s1 += curve.total_length
        while s0 >= curve.total_length:
            s0 -= curve.total_length
            s1 -= curve.total_length

    return path_for_curve_interval(curve, s0, s1)



def build_full_path_for_curve(curve):
    """Reconstruct the original continuous curve as cubic Bezier path data."""
    if curve.total_length <= 0.0:
        return ""

    d = path_for_curve_interval(curve, 0.0, curve.total_length)
    if curve.closed and d:
        d += " Z"
    return d


def add_unique_crossing_position(items, curve, s, crossing_id, tolerance_px=1.0):
    """Add an arc-length position unless an equivalent position is already present."""
    if curve.closed and curve.total_length > 0.0:
        s = s % curve.total_length
    else:
        s = max(0.0, min(curve.total_length, s))

    for existing_s, _existing_crossing_id in items:
        if arc_length_distance(curve, existing_s, s) <= tolerance_px:
            return

    items.append((s, crossing_id))


def collect_crossing_positions_by_curve(curves, crossings, tolerance_px=1.0):
    """Collect all branch positions where each curve participates in a crossing."""
    curves_by_id = {curve.curve_id: curve for curve in curves}
    positions = {curve.curve_id: [] for curve in curves}

    for crossing in crossings:
        curve_a = curves_by_id[crossing.curve_a]
        curve_b = curves_by_id[crossing.curve_b]

        add_unique_crossing_position(
            positions[crossing.curve_a],
            curve_a,
            crossing.s_a,
            crossing.crossing_id,
            tolerance_px=tolerance_px,
        )
        add_unique_crossing_position(
            positions[crossing.curve_b],
            curve_b,
            crossing.s_b,
            crossing.crossing_id,
            tolerance_px=tolerance_px,
        )

    for curve_id in positions:
        positions[curve_id].sort(key=lambda item: item[0])

    return positions


def build_direction_guide_paths(curves, crossings, guide_length_px, min_interval_px=20.0):
    """
    Build short curve segments centered between adjacent crossings on each curve.

    These segments are intended as Illustrator arrow placeholders. They lie on
    top of the source curve and follow its local direction.
    """
    guides = []
    if not crossings or guide_length_px <= 0.0:
        return guides

    positions_by_curve = collect_crossing_positions_by_curve(curves, crossings)

    for curve in curves:
        positions = positions_by_curve.get(curve.curve_id, [])
        if len(positions) < 2 or curve.total_length <= 0.0:
            continue

        pair_list = []
        if curve.closed:
            for i in range(len(positions)):
                s0, cid0 = positions[i]
                s1, cid1 = positions[(i + 1) % len(positions)]
                if i == len(positions) - 1:
                    s1 += curve.total_length
                pair_list.append((s0, s1, cid0, cid1))
        else:
            for i in range(len(positions) - 1):
                s0, cid0 = positions[i]
                s1, cid1 = positions[i + 1]
                pair_list.append((s0, s1, cid0, cid1))

        for s0, s1, cid0, cid1 in pair_list:
            interval_len = s1 - s0
            if interval_len < min_interval_px:
                continue

            center = s0 + 0.5 * interval_len
            if curve.closed and center >= curve.total_length:
                center -= curve.total_length

            # Keep the guide short enough that it remains clearly between the two crossings.
            half_len = min(0.5 * guide_length_px, 0.22 * interval_len)
            if half_len <= 0.5:
                continue

            d = build_overpass_path(curve, center, half_len)
            if d:
                guides.append(
                    {
                        "curve_id": curve.curve_id,
                        "crossing_start": cid0,
                        "crossing_end": cid1,
                        "center_s": center,
                        "d": d,
                    }
                )

    return guides


def parse_style_attr(style_text):
    style = {}
    if not style_text:
        return style
    for item in style_text.split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        style[key.strip()] = value.strip()
    return style


def get_svg_dimensions(root):
    view_box = root.get("viewBox")
    width = root.get("width", "800")
    height = root.get("height", "800")

    def clean_dimension(value, default):
        if value is None:
            return default
        m = re.match(r"[-+]?(?:\d*\.\d+|\d+\.?)", value)
        return float(m.group(0)) if m else default

    return view_box, clean_dimension(width, 800.0), clean_dimension(height, 800.0)


def collect_path_elements(root):
    items = []

    def traverse(element, parent_matrix):
        local_transform = parse_transform(element.get("transform"))
        matrix = matrix_multiply(local_transform, parent_matrix)

        if strip_namespace(element.tag) == "path" and element.get("d"):
            items.append((element, matrix))

        for child in list(element):
            traverse(child, matrix)

    traverse(root, matrix_identity())
    return items


def read_svg_curves(input_path):
    tree = ET.parse(str(input_path))
    root = tree.getroot()
    path_items = collect_path_elements(root)

    curves = []
    next_curve_id = 1

    for path_index, (element, matrix) in enumerate(path_items, start=1):
        d = element.get("d")
        element_id = element.get("id", "path_{}".format(path_index))

        parsed, next_curve_id = parse_svg_path_to_curves(
            d=d,
            matrix=matrix,
            source_id=element_id,
            start_curve_id=next_curve_id,
        )
        curves.extend(parsed)

    return root, curves


def make_output_svg(root, curves, crossings, hidden_by_curve, args):
    view_box, width, height = get_svg_dimensions(root)
    attrs = [
        'xmlns="{}"'.format(SVG_NS),
        'width="{}"'.format(args.width if args.width else "{:.0f}".format(width)),
        'height="{}"'.format(args.height if args.height else "{:.0f}".format(height)),
    ]

    if args.viewbox:
        attrs.append('viewBox="{}"'.format(args.viewbox))
    elif view_box:
        attrs.append('viewBox="{}"'.format(view_box))

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<svg {}>".format(" ".join(attrs)),
        "  <title>SVG crossing gap options</title>",
        "  <desc>Generated by svg_crossing_gap_optionsV3.py. Base curves have real gaps. Each crossing has two editable overpass choices.</desc>",
        "",
    ]

    if not args.no_original:
        original_display = "" if args.original_display == "visible" else ' display="none"'
        original_stroke_width = args.original_stroke_width if args.original_stroke_width is not None else args.stroke_width
        lines.append(
            "  <g id=\"original_graph_reference\" fill=\"none\" stroke=\"{}\" stroke-width=\"{}\" stroke-linecap=\"round\" stroke-linejoin=\"round\" opacity=\"{}\"{}>".format(
                args.original_stroke,
                original_stroke_width,
                args.original_opacity,
                original_display,
            )
        )
        for curve in curves:
            d = build_full_path_for_curve(curve)
            if d:
                safe_source_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", curve.source_id)
                lines.append(
                    '    <path id="original_curve_{:03d}_from_{}" d="{}"/>'.format(
                        curve.curve_id,
                        safe_source_id,
                        d,
                    )
                )
        lines.append("  </g>")
        lines.append("")

    lines.append(
        "  <g id=\"base_broken_strands\" fill=\"none\" stroke=\"{}\" stroke-width=\"{}\" stroke-linecap=\"round\" stroke-linejoin=\"round\">".format(
            args.stroke,
            args.stroke_width,
        )
    )

    for curve in curves:
        d = build_visible_path_for_curve(curve, hidden_by_curve.get(curve.curve_id, []))
        if d:
            safe_source_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", curve.source_id)
            lines.append(
                '    <path id="curve_{:03d}_from_{}" d="{}"/>'.format(
                    curve.curve_id,
                    safe_source_id,
                    d,
                )
            )
    lines.append("  </g>")
    lines.append("")

    curves_by_id = {curve.curve_id: curve for curve in curves}
    option_radius = args.gap_radius_px + args.bridge_extra_px

    lines.append('  <g id="crossing_options" fill="none" stroke-linecap="round" stroke-linejoin="round">')

    for crossing in crossings:
        curve_a = curves_by_id[crossing.curve_a]
        curve_b = curves_by_id[crossing.curve_b]

        d_a = build_overpass_path(curve_a, crossing.s_a, option_radius)
        d_b = build_overpass_path(curve_b, crossing.s_b, option_radius)

        display_a = "" if args.default_choice in ("both", "a") else ' display="none"'
        display_b = "" if args.default_choice in ("both", "b") else ' display="none"'

        lines.append('    <g id="crossing_{:03d}_choose_one">'.format(crossing.crossing_id))
        lines.append(
            '      <path id="crossing_{:03d}_choice_A_curve_{:03d}_over" d="{}" stroke="{}" stroke-width="{}"{} />'.format(
                crossing.crossing_id,
                crossing.curve_a,
                d_a,
                args.option_a_color,
                args.stroke_width,
                display_a,
            )
        )
        lines.append(
            '      <path id="crossing_{:03d}_choice_B_curve_{:03d}_over" d="{}" stroke="{}" stroke-width="{}"{} />'.format(
                crossing.crossing_id,
                crossing.curve_b,
                d_b,
                args.option_b_color,
                args.stroke_width,
                display_b,
            )
        )
        lines.append("    </g>")

    lines.append("  </g>")
    lines.append("")

    if args.add_direction_guides:
        guide_stroke_width = args.direction_guide_stroke_width if args.direction_guide_stroke_width is not None else args.stroke_width
        guide_display = "" if args.direction_guide_display == "visible" else ' display="none"'
        direction_guides = build_direction_guide_paths(
            curves,
            crossings,
            guide_length_px=args.direction_guide_length_px,
            min_interval_px=args.direction_guide_min_interval_px,
        )
        lines.append(
            "  <g id=\"direction_guides\" fill=\"none\" stroke=\"{}\" stroke-width=\"{}\" stroke-linecap=\"round\" stroke-linejoin=\"round\"{}>".format(
                args.direction_guide_stroke,
                guide_stroke_width,
                guide_display,
            )
        )
        for guide_index, guide in enumerate(direction_guides, start=1):
            lines.append(
                '    <path id="direction_guide_{:03d}_curve_{:03d}_between_crossing_{:03d}_{:03d}" d="{}"/>'.format(
                    guide_index,
                    guide["curve_id"],
                    guide["crossing_start"],
                    guide["crossing_end"],
                    guide["d"],
                )
            )
        lines.append("  </g>")
        lines.append("")

    if not args.no_labels:
        lines.append('  <g id="crossing_labels" font-family="Arial, sans-serif" font-size="12" fill="#666666">')
        for crossing in crossings:
            lines.append(
                '    <text id="crossing_{:03d}_label" x="{:.2f}" y="{:.2f}">{:03d}</text>'.format(
                    crossing.crossing_id,
                    crossing.x + args.label_offset_px,
                    crossing.y - args.label_offset_px,
                    crossing.crossing_id,
                )
            )
        lines.append("  </g>")
        lines.append("")

    lines.append("  <!-- Crossing table:")
    for crossing in crossings:
        lines.append(
            "       crossing {cid:03d}: curve {ca} at s={sa:.2f}; curve {cb} at s={sb:.2f}; xy=({x:.2f},{y:.2f})".format(
                cid=crossing.crossing_id,
                ca=crossing.curve_a,
                sa=crossing.s_a,
                cb=crossing.curve_b,
                sb=crossing.s_b,
                x=crossing.x,
                y=crossing.y,
            )
        )
    lines.append("  -->")
    lines.append("</svg>")
    lines.append("")

    return "\n".join(lines)

def derive_output_path(input_path):
    p = Path(input_path)
    return str(p.with_name(p.stem + "_crossing_gap" + p.suffix))


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Create an SVG with real crossing gaps and editable over/under choices."
    )
    parser.add_argument(
        "input_svg",
        nargs="?",
        help="Input SVG file. If omitted, GUI mode is opened.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Open GUI mode for selecting the input file and parameters.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output SVG path. Default: input name with _crossing_gap suffix.",
    )
    parser.add_argument(
        "--gap-radius-px",
        type=float,
        default=10.0,
        help="Arc-length radius removed on each side of every crossing. Total gap is about 2x this value. Default: 10.",
    )
    parser.add_argument(
        "--bridge-extra-px",
        type=float,
        default=1.0,
        help="Extra overlap added to overpass choices to avoid tiny seams. Default: 1.",
    )
    parser.add_argument(
        "--sample-step-px",
        type=float,
        default=3.0,
        help="Polyline sampling step for crossing detection. Smaller is more accurate but slower. Default: 3.",
    )
    parser.add_argument(
        "--cluster-px",
        type=float,
        default=4.0,
        help="Distance threshold for merging duplicate crossing detections. Default: 4.",
    )
    parser.add_argument(
        "--param-cluster-px",
        type=float,
        default=None,
        help=(
            "Arc-length threshold for merging duplicate detections along the same branch. "
            "Default: max(2 * cluster-px, 8)."
        ),
    )
    parser.add_argument(
        "--min-self-separation-px",
        type=float,
        default=20.0,
        help="Ignore self-intersections between nearby positions on the same curve. Default: 20.",
    )
    parser.add_argument(
        "--stroke",
        default="#000000",
        help="Base broken-strand stroke color. Default: black.",
    )
    parser.add_argument(
        "--stroke-width",
        type=float,
        default=8.0,
        help="Stroke width for output line art. Default: 8.",
    )
    parser.add_argument(
        "--option-a-color",
        default="#d62728",
        help="Color for choice A overpass candidate. Default: red.",
    )
    parser.add_argument(
        "--option-b-color",
        default="#1f77b4",
        help="Color for choice B overpass candidate. Default: blue.",
    )
    parser.add_argument(
        "--default-choice",
        choices=["both", "a", "b", "none"],
        default="both",
        help="Which overpass choices are visible initially. Default: both.",
    )
    parser.add_argument(
        "--no-labels",
        action="store_true",
        help="Do not add crossing number labels.",
    )
    parser.add_argument(
        "--label-offset-px",
        type=float,
        default=12.0,
        help="Offset for crossing labels. Default: 12.",
    )
    parser.add_argument(
        "--no-original",
        action="store_true",
        help="Do not include the original continuous graph as a reference object.",
    )
    parser.add_argument(
        "--original-display",
        choices=["hidden", "visible"],
        default="hidden",
        help="Whether the original graph reference is initially hidden or visible. Default: hidden.",
    )
    parser.add_argument(
        "--original-stroke",
        default="#888888",
        help="Stroke color for the original graph reference. Default: gray.",
    )
    parser.add_argument(
        "--original-opacity",
        type=float,
        default=0.35,
        help="Opacity for the original graph reference. Default: 0.35.",
    )
    parser.add_argument(
        "--original-stroke-width",
        type=float,
        default=None,
        help="Stroke width for original graph reference. Default: same as --stroke-width.",
    )
    parser.add_argument(
        "--add-direction-guides",
        action="store_true",
        help="Add short curved guide segments between adjacent crossings for later arrowheads.",
    )
    parser.add_argument(
        "--direction-guide-length-px",
        type=float,
        default=32.0,
        help="Approximate length of each direction-guide segment. Default: 32.",
    )
    parser.add_argument(
        "--direction-guide-min-interval-px",
        type=float,
        default=20.0,
        help="Skip direction guides when adjacent crossings are closer than this. Default: 20.",
    )
    parser.add_argument(
        "--direction-guide-stroke",
        default="#2ca02c",
        help="Stroke color for direction guides. Default: green.",
    )
    parser.add_argument(
        "--direction-guide-stroke-width",
        type=float,
        default=None,
        help="Stroke width for direction guides. Default: same as --stroke-width.",
    )
    parser.add_argument(
        "--direction-guide-display",
        choices=["hidden", "visible"],
        default="visible",
        help="Whether direction guides are initially hidden or visible. Default: visible.",
    )
    parser.add_argument(
        "--viewbox",
        default=None,
        help="Override output SVG viewBox.",
    )
    parser.add_argument(
        "--width",
        default=None,
        help="Override output SVG width.",
    )
    parser.add_argument(
        "--height",
        default=None,
        help="Override output SVG height.",
    )
    return parser


def parse_args(argv=None):
    return build_arg_parser().parse_args(argv)


def validate_args(args):
    if not args.input_svg:
        raise ValueError("Input SVG is required unless GUI mode is used.")
    if args.gap_radius_px <= 0.0:
        raise ValueError("--gap-radius-px must be positive.")
    if args.sample_step_px <= 0.0:
        raise ValueError("--sample-step-px must be positive.")
    if args.stroke_width <= 0.0:
        raise ValueError("--stroke-width must be positive.")
    if args.add_direction_guides and args.direction_guide_length_px <= 0.0:
        raise ValueError("--direction-guide-length-px must be positive.")


def process_svg(args):
    validate_args(args)

    input_path = Path(args.input_svg)
    output_path = Path(args.output) if args.output else Path(derive_output_path(input_path))

    if not input_path.exists():
        raise FileNotFoundError("Input SVG does not exist: {}".format(input_path))

    root, curves = read_svg_curves(input_path)

    if not curves:
        raise ValueError("No supported SVG path curves were found in the input file.")

    for curve in curves:
        flatten_curve(curve, sample_step_px=args.sample_step_px)

    raw = find_raw_crossings(
        curves,
        min_self_separation_px=args.min_self_separation_px,
    )
    curves_by_id = {curve.curve_id: curve for curve in curves}
    crossings = cluster_crossings(
        raw,
        curves_by_id=curves_by_id,
        cluster_px=args.cluster_px,
        param_cluster_px=args.param_cluster_px,
    )

    hidden_by_curve = build_hidden_intervals_by_curve(
        curves,
        crossings,
        gap_radius_px=args.gap_radius_px,
    )

    svg = make_output_svg(root, curves, crossings, hidden_by_curve, args)
    output_path.write_text(svg, encoding="utf-8")

    return {
        "input_curves": len(curves),
        "detected_crossings": len(crossings),
        "output_path": str(output_path),
    }


def run_gui(initial_args=None):
    """Run a small tkinter GUI for users who prefer not to use command-line arguments."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception as exc:
        raise RuntimeError(
            "GUI mode requires tkinter, which is not available in this Python installation. "
            "Use command-line mode instead."
        ) from exc

    defaults = initial_args if initial_args is not None else parse_args([])

    root = tk.Tk()
    root.title("SVG Crossing Gap Options V3")

    input_var = tk.StringVar(value=defaults.input_svg or "")
    output_var = tk.StringVar(value=defaults.output or "")
    gap_var = tk.StringVar(value=str(defaults.gap_radius_px))
    bridge_var = tk.StringVar(value=str(defaults.bridge_extra_px))
    sample_var = tk.StringVar(value=str(defaults.sample_step_px))
    cluster_var = tk.StringVar(value=str(defaults.cluster_px))
    param_cluster_var = tk.StringVar(value="" if defaults.param_cluster_px is None else str(defaults.param_cluster_px))
    min_self_var = tk.StringVar(value=str(defaults.min_self_separation_px))
    stroke_var = tk.StringVar(value=defaults.stroke)
    stroke_width_var = tk.StringVar(value=str(defaults.stroke_width))
    option_a_var = tk.StringVar(value=defaults.option_a_color)
    option_b_var = tk.StringVar(value=defaults.option_b_color)
    default_choice_var = tk.StringVar(value=defaults.default_choice)
    labels_var = tk.BooleanVar(value=not defaults.no_labels)
    label_offset_var = tk.StringVar(value=str(defaults.label_offset_px))

    keep_original_var = tk.BooleanVar(value=not defaults.no_original)
    original_display_var = tk.StringVar(value=defaults.original_display)
    original_stroke_var = tk.StringVar(value=defaults.original_stroke)
    original_opacity_var = tk.StringVar(value=str(defaults.original_opacity))
    original_stroke_width_var = tk.StringVar(value="" if defaults.original_stroke_width is None else str(defaults.original_stroke_width))

    direction_guides_var = tk.BooleanVar(value=defaults.add_direction_guides)
    direction_length_var = tk.StringVar(value=str(defaults.direction_guide_length_px))
    direction_min_interval_var = tk.StringVar(value=str(defaults.direction_guide_min_interval_px))
    direction_stroke_var = tk.StringVar(value=defaults.direction_guide_stroke)
    direction_stroke_width_var = tk.StringVar(value="" if defaults.direction_guide_stroke_width is None else str(defaults.direction_guide_stroke_width))
    direction_display_var = tk.StringVar(value=defaults.direction_guide_display)

    viewbox_var = tk.StringVar(value=defaults.viewbox or "")
    width_var = tk.StringVar(value=defaults.width or "")
    height_var = tk.StringVar(value=defaults.height or "")
    status_var = tk.StringVar(value="Choose an input SVG, adjust parameters, then click Run.")

    def default_output_for(path_text):
        if not path_text:
            return ""
        return derive_output_path(Path(path_text))

    def browse_input():
        path = filedialog.askopenfilename(
            title="Select input SVG",
            filetypes=[("SVG files", "*.svg"), ("All files", "*.*")],
        )
        if path:
            input_var.set(path)
            if not output_var.get().strip():
                output_var.set(default_output_for(path))

    def browse_output():
        initial = output_var.get().strip() or default_output_for(input_var.get().strip())
        path = filedialog.asksaveasfilename(
            title="Save output SVG",
            defaultextension=".svg",
            initialfile=Path(initial).name if initial else "output_crossing_gap.svg",
            initialdir=str(Path(initial).parent) if initial else None,
            filetypes=[("SVG files", "*.svg"), ("All files", "*.*")],
        )
        if path:
            output_var.set(path)

    def optional_float(text):
        text = text.strip()
        return None if text == "" else float(text)

    def optional_text(text):
        text = text.strip()
        return None if text == "" else text

    def collect_gui_args():
        input_path = input_var.get().strip()
        output_path = output_var.get().strip() or default_output_for(input_path)

        return argparse.Namespace(
            input_svg=input_path,
            gui=False,
            output=output_path,
            gap_radius_px=float(gap_var.get()),
            bridge_extra_px=float(bridge_var.get()),
            sample_step_px=float(sample_var.get()),
            cluster_px=float(cluster_var.get()),
            param_cluster_px=optional_float(param_cluster_var.get()),
            min_self_separation_px=float(min_self_var.get()),
            stroke=stroke_var.get().strip() or "#000000",
            stroke_width=float(stroke_width_var.get()),
            option_a_color=option_a_var.get().strip() or "#d62728",
            option_b_color=option_b_var.get().strip() or "#1f77b4",
            default_choice=default_choice_var.get(),
            no_labels=not labels_var.get(),
            label_offset_px=float(label_offset_var.get()),
            no_original=not keep_original_var.get(),
            original_display=original_display_var.get(),
            original_stroke=original_stroke_var.get().strip() or "#888888",
            original_opacity=float(original_opacity_var.get()),
            original_stroke_width=optional_float(original_stroke_width_var.get()),
            add_direction_guides=direction_guides_var.get(),
            direction_guide_length_px=float(direction_length_var.get()),
            direction_guide_min_interval_px=float(direction_min_interval_var.get()),
            direction_guide_stroke=direction_stroke_var.get().strip() or "#2ca02c",
            direction_guide_stroke_width=optional_float(direction_stroke_width_var.get()),
            direction_guide_display=direction_display_var.get(),
            viewbox=optional_text(viewbox_var.get()),
            width=optional_text(width_var.get()),
            height=optional_text(height_var.get()),
        )

    def run_processing():
        try:
            args = collect_gui_args()
            result = process_svg(args)
            message = (
                "Done.\n\nInput curves: {input_curves}\nDetected crossings: {detected_crossings}\nSaved SVG:\n{output_path}".format(
                    **result
                )
            )
            status_var.set("Saved: {}".format(result["output_path"]))
            messagebox.showinfo("SVG crossing gap options", message)
        except Exception as exc:
            status_var.set("Error: {}".format(exc))
            messagebox.showerror("Error", str(exc))

    main_frame = tk.Frame(root, padx=12, pady=12)
    main_frame.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    row = 0

    def add_labeled_entry(label, variable, width=24, browse_command=None):
        nonlocal row
        tk.Label(main_frame, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        entry = tk.Entry(main_frame, textvariable=variable, width=width)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        if browse_command is not None:
            tk.Button(main_frame, text="Browse", command=browse_command).grid(row=row, column=2, sticky="ew", padx=(6, 0), pady=2)
        row += 1
        return entry

    main_frame.columnconfigure(1, weight=1)

    add_labeled_entry("Input SVG", input_var, width=54, browse_command=browse_input)
    add_labeled_entry("Output SVG", output_var, width=54, browse_command=browse_output)

    separator = tk.Frame(main_frame, height=1, bd=1, relief="sunken")
    separator.grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
    row += 1

    add_labeled_entry("Gap radius px", gap_var)
    add_labeled_entry("Bridge extra px", bridge_var)
    add_labeled_entry("Sample step px", sample_var)
    add_labeled_entry("Cluster px", cluster_var)
    add_labeled_entry("Param cluster px blank=auto", param_cluster_var)
    add_labeled_entry("Min self separation px", min_self_var)
    add_labeled_entry("Stroke color", stroke_var)
    add_labeled_entry("Stroke width", stroke_width_var)
    add_labeled_entry("Choice A color", option_a_var)
    add_labeled_entry("Choice B color", option_b_var)

    tk.Label(main_frame, text="Default visible choice", anchor="w").grid(row=row, column=0, sticky="w", pady=2)
    tk.OptionMenu(main_frame, default_choice_var, "both", "a", "b", "none").grid(row=row, column=1, sticky="w", pady=2)
    row += 1

    tk.Checkbutton(main_frame, text="Add crossing labels", variable=labels_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
    row += 1
    add_labeled_entry("Label offset px", label_offset_var)

    separator = tk.Frame(main_frame, height=1, bd=1, relief="sunken")
    separator.grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
    row += 1

    tk.Checkbutton(main_frame, text="Keep original graph reference", variable=keep_original_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
    row += 1
    tk.Label(main_frame, text="Original display", anchor="w").grid(row=row, column=0, sticky="w", pady=2)
    tk.OptionMenu(main_frame, original_display_var, "hidden", "visible").grid(row=row, column=1, sticky="w", pady=2)
    row += 1
    add_labeled_entry("Original stroke", original_stroke_var)
    add_labeled_entry("Original opacity", original_opacity_var)
    add_labeled_entry("Original stroke width blank=same", original_stroke_width_var)

    separator = tk.Frame(main_frame, height=1, bd=1, relief="sunken")
    separator.grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
    row += 1

    tk.Checkbutton(main_frame, text="Add direction-guide curves for arrows", variable=direction_guides_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
    row += 1
    add_labeled_entry("Direction guide length px", direction_length_var)
    add_labeled_entry("Direction min interval px", direction_min_interval_var)
    add_labeled_entry("Direction guide stroke", direction_stroke_var)
    add_labeled_entry("Direction stroke width blank=same", direction_stroke_width_var)
    tk.Label(main_frame, text="Direction guide display", anchor="w").grid(row=row, column=0, sticky="w", pady=2)
    tk.OptionMenu(main_frame, direction_display_var, "visible", "hidden").grid(row=row, column=1, sticky="w", pady=2)
    row += 1

    separator = tk.Frame(main_frame, height=1, bd=1, relief="sunken")
    separator.grid(row=row, column=0, columnspan=3, sticky="ew", pady=8)
    row += 1

    add_labeled_entry("Override viewBox blank=keep", viewbox_var, width=54)
    add_labeled_entry("Override width blank=keep", width_var)
    add_labeled_entry("Override height blank=keep", height_var)

    button_frame = tk.Frame(main_frame)
    button_frame.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 4))
    tk.Button(button_frame, text="Run", command=run_processing, width=16).pack(side="left")
    tk.Button(button_frame, text="Quit", command=root.destroy, width=16).pack(side="left", padx=8)
    row += 1

    tk.Label(main_frame, textvariable=status_var, anchor="w", fg="#555555", wraplength=580).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))

    root.mainloop()


def main():
    if len(sys.argv) == 1:
        run_gui()
        return

    args = parse_args()

    if args.gui or not args.input_svg:
        run_gui(args)
        return

    result = process_svg(args)

    print("Input curves:", result["input_curves"])
    print("Detected crossings:", result["detected_crossings"])
    print("Saved SVG:", result["output_path"])

    if result["detected_crossings"] == 0:
        print(
            "No crossings were detected. Try decreasing --sample-step-px, "
            "or check whether your paths are actually intersecting.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
