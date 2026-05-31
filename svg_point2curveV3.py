#!/usr/bin/env python3
"""
point2curveV3.py

Create smooth SVG cubic Bezier curves from ordered 2D points.

Inputs:
  - Text/CSV files containing x,y coordinates.
  - SVG files containing circle or ellipse elements; each circle center becomes a point.

Outputs:
  - An SVG file containing one smooth cubic Bezier path per curve group.
  - Optional text export of parsed points, useful when starting from SVG circles.

Example commands:
  python point2curveV3.py points.txt --output points_curve.svg
  python point2curveV3.py points.txt --curve-mode open --output open_curve.svg
  python point2curveV3.py input.svg --output curves.svg --keep-svg-frame --export-points-txt
  python point2curveV3.py input.svg --group-mode split --group-spec "curve_A:4; curve_B:*"
  python point2curveV3.py --gui

Text input examples:

  # Single curve; blank/comment lines are allowed.
  0, 0
  100, 0
  100, 100
  0, 100

  # Multiple curves by section name.
  [curve_A]
  0, 0
  100, 0
  100, 100
  0, 100

  [curve_B]
  150, 0
  250, 0
  250, 100
  150, 100

  # Multiple curves as CSV.
  curve,x,y
  curve_A,0,0
  curve_A,100,0
  curve_A,100,100
  curve_A,0,100
  curve_B,150,0
  curve_B,250,0
  curve_B,250,100
  curve_B,150,100

SVG input grouping:
  - Circles inside <g id="curve_A">...</g> become one curve.
  - Or add data-curve="curve_A" / data-group="curve_A" to each circle.
  - Points are used in SVG document order within each curve group.
  - GUI/CLI regrouping can also split all parsed points by counts or index ranges.

Curve construction notes:
  - V3 defaults to --method elastic. This builds a natural cubic spline for open
    curves and a periodic cubic spline for closed curves. It is a practical
    "elastic rod" interpolation: with --smoothness 1.0, neighboring Bezier
    segments share tangents at each anchor, and closed curves match the tangent
    and curvature at the seam.
  - A closed curve uses one SVG anchor per input point and adds the final cubic
    segment back to the first point. By default V3 also writes an SVG closepath
    Z command so the stroke is truly joined at the start/end instead of showing
    overlapping end caps.
  - Use --curve-mode open when the path should not return to the first point.
  - Non-adjacent repeated points are preserved, so crossings can be represented.
    Consecutive duplicate points are removed because they make zero-length
    spline intervals.
  - For exact interpolation, keep --simplify-tolerance 0. A positive tolerance
    may reduce anchors but then the output may not pass through every original
    point exactly.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape


EPSILON = 1.0e-9
Point = Tuple[float, float]
Matrix = Tuple[float, float, float, float, float, float]


@dataclass
class CurveResult:
    name: str
    points: List[Point]
    path_d: str
    segments: List[Tuple[Point, Point, Point, Point]]


@dataclass
class SvgFrame:
    """Root SVG sizing attributes to preserve input/output alignment."""

    width: Optional[str] = None
    height: Optional[str] = None
    view_box: Optional[str] = None
    x: Optional[str] = None
    y: Optional[str] = None

    def has_any_geometry(self) -> bool:
        return any(value for value in (self.width, self.height, self.view_box, self.x, self.y))


class PointParseError(ValueError):
    """Raised when an input point file cannot be parsed."""


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def derive_output_name(input_path: str, suffix: str = "_curve") -> str:
    base, _ext = os.path.splitext(input_path)
    return base + suffix + ".svg"


def derive_points_output_name(input_path: str, suffix: str = "_points") -> str:
    base, _ext = os.path.splitext(input_path)
    return base + suffix + ".txt"


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def attr_local_name(attr_name: str) -> str:
    if "}" in attr_name:
        return attr_name.rsplit("}", 1)[1]
    return attr_name


def sanitize_id(text: str, fallback: str = "curve") -> str:
    text = str(text).strip() or fallback
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)
    if not re.match(r"^[A-Za-z_]", text):
        text = "curve_" + text
    return text


def unique_names(names: Iterable[str]) -> List[str]:
    counts: Dict[str, int] = {}
    result: List[str] = []
    for name in names:
        clean = sanitize_id(name)
        count = counts.get(clean, 0)
        counts[clean] = count + 1
        if count:
            result.append(f"{clean}_{count + 1}")
        else:
            result.append(clean)
    return result


def format_num(value: float, precision: int = 6) -> str:
    if abs(value) < 0.5 * (10 ** -precision):
        value = 0.0
    s = f"{value:.{precision}f}"
    s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def parse_number(value: Optional[str], what: str = "number") -> float:
    if value is None:
        raise PointParseError(f"Missing {what}.")
    text = str(value).strip()
    match = re.match(
        r"^[\s,]*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", text
    )
    if not match:
        raise PointParseError(f"Could not parse {what!s} from {value!r}.")
    return float(match.group(1))


def strip_comment(line: str) -> str:
    # For simple point files, '#' starts a comment. CSV files with quoted '#'
    # are uncommon for point coordinates; keep this parser lightweight.
    return line.split("#", 1)[0].strip()


def split_tokens(line: str) -> List[str]:
    text = line.strip()
    if not text:
        return []
    if "," in text:
        return [token.strip() for token in next(csv.reader([text]))]
    return [token.strip() for token in re.split(r"\s+", text) if token.strip()]


def is_float_token(token: str) -> bool:
    try:
        parse_number(token)
        return True
    except PointParseError:
        return False


def same_point(a: Point, b: Point, eps: float = EPSILON) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


# ---------------------------------------------------------------------------
# Vector and geometry helpers
# ---------------------------------------------------------------------------


def add(a: Point, b: Point) -> Point:
    return (a[0] + b[0], a[1] + b[1])


def sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def mul(a: Point, scalar: float) -> Point:
    return (a[0] * scalar, a[1] * scalar)


def div(a: Point, scalar: float) -> Point:
    if abs(scalar) < EPSILON:
        return (0.0, 0.0)
    return (a[0] / scalar, a[1] / scalar)


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def distance_point_to_segment(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    denom = vx * vx + vy * vy
    if denom <= EPSILON:
        return distance(p, a)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    projection = (ax + t * vx, ay + t * vy)
    return distance(p, projection)


# ---------------------------------------------------------------------------
# Text/CSV input parser
# ---------------------------------------------------------------------------


def parse_text_points(path: str) -> "OrderedDict[str, List[Point]]":
    with open(path, "r", encoding="utf-8-sig") as handle:
        raw_lines = handle.readlines()

    first_nonempty = ""
    for raw in raw_lines:
        stripped = strip_comment(raw)
        if stripped:
            first_nonempty = stripped
            break
    if not first_nonempty:
        raise PointParseError("The text input file does not contain any points.")

    first_tokens = split_tokens(first_nonempty)
    header_tokens = [token.lower() for token in first_tokens]
    has_header = ("x" in header_tokens or "cx" in header_tokens) and (
        "y" in header_tokens or "cy" in header_tokens
    )

    if has_header:
        return parse_table_text_points(raw_lines)
    return parse_freeform_text_points(raw_lines)


def parse_table_text_points(raw_lines: Sequence[str]) -> "OrderedDict[str, List[Point]]":
    groups: "OrderedDict[str, List[Point]]" = OrderedDict()
    header: Optional[List[str]] = None
    header_line_number = 0

    for line_number, raw in enumerate(raw_lines, start=1):
        stripped = strip_comment(raw)
        if not stripped:
            continue
        header = split_tokens(stripped)
        header_line_number = line_number
        break

    if not header:
        raise PointParseError("Missing table header.")

    lower = [token.lower() for token in header]
    x_index = lower.index("x") if "x" in lower else lower.index("cx")
    y_index = lower.index("y") if "y" in lower else lower.index("cy")

    group_index = None
    for candidate in ("curve", "group", "path", "shape", "id", "name"):
        if candidate in lower:
            group_index = lower.index(candidate)
            break

    for line_number, raw in enumerate(raw_lines[header_line_number:], start=header_line_number + 1):
        stripped = strip_comment(raw)
        if not stripped:
            continue
        tokens = split_tokens(stripped)
        needed = max(x_index, y_index, group_index if group_index is not None else 0)
        if len(tokens) <= needed:
            raise PointParseError(
                f"Line {line_number}: expected at least {needed + 1} columns, got {len(tokens)}."
            )
        group_name = "curve_1"
        if group_index is not None and tokens[group_index].strip():
            group_name = tokens[group_index].strip()
        try:
            point = (parse_number(tokens[x_index], "x"), parse_number(tokens[y_index], "y"))
        except PointParseError as exc:
            raise PointParseError(f"Line {line_number}: {exc}") from exc
        groups.setdefault(group_name, []).append(point)

    if not groups:
        raise PointParseError("No point rows were found after the header.")
    return groups


def parse_group_directive(line: str) -> Optional[str]:
    text = line.strip()
    if len(text) >= 3 and text.startswith("[") and text.endswith("]"):
        return text[1:-1].strip()
    lower = text.lower()
    for prefix in ("curve ", "group ", "path ", ">"):
        if lower.startswith(prefix):
            return text[len(prefix) :].strip()
    return None


def parse_freeform_text_points(raw_lines: Sequence[str]) -> "OrderedDict[str, List[Point]]":
    groups: "OrderedDict[str, List[Point]]" = OrderedDict()
    current_group: Optional[str] = None
    auto_index = 0

    def next_auto_group() -> str:
        nonlocal auto_index
        auto_index += 1
        return f"curve_{auto_index}"

    for line_number, raw in enumerate(raw_lines, start=1):
        stripped = strip_comment(raw)
        if not stripped:
            # A blank line separates unlabeled curves.
            current_group = None
            continue

        directive = parse_group_directive(stripped)
        if directive is not None:
            current_group = directive or next_auto_group()
            groups.setdefault(current_group, [])
            continue

        colon_group: Optional[str] = None
        colon_rest = stripped
        if ":" in stripped:
            left, right = stripped.split(":", 1)
            if left.strip() and right.strip():
                colon_group = left.strip()
                colon_rest = right.strip()

        tokens = split_tokens(colon_rest)
        if len(tokens) < 2:
            raise PointParseError(f"Line {line_number}: expected x,y coordinates.")

        try:
            if is_float_token(tokens[0]) and is_float_token(tokens[1]):
                group_name = colon_group or current_group or next_auto_group()
                point = (parse_number(tokens[0], "x"), parse_number(tokens[1], "y"))
            elif len(tokens) >= 3 and is_float_token(tokens[1]) and is_float_token(tokens[2]):
                group_name = colon_group or tokens[0].strip() or current_group or next_auto_group()
                point = (parse_number(tokens[1], "x"), parse_number(tokens[2], "y"))
            else:
                raise PointParseError("expected either 'x,y' or 'curve,x,y'.")
        except PointParseError as exc:
            raise PointParseError(f"Line {line_number}: {exc}") from exc

        current_group = group_name
        groups.setdefault(group_name, []).append(point)

    empty_groups = [name for name, pts in groups.items() if not pts]
    for name in empty_groups:
        del groups[name]
    if not groups:
        raise PointParseError("No points were found in the text input file.")
    return groups


# ---------------------------------------------------------------------------
# SVG input parser
# ---------------------------------------------------------------------------


def matrix_identity() -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def matrix_multiply(m1: Matrix, m2: Matrix) -> Matrix:
    # SVG affine matrix convention:
    # [a c e]
    # [b d f]
    # [0 0 1]
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


def matrix_apply(m: Matrix, point: Point) -> Point:
    a, b, c, d, e, f = m
    x, y = point
    return (a * x + c * y + e, b * x + d * y + f)


def transform_numbers(arg_text: str) -> List[float]:
    return [
        float(value)
        for value in re.findall(
            r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", arg_text
        )
    ]


def translate_matrix(tx: float, ty: float = 0.0) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def scale_matrix(sx: float, sy: Optional[float] = None) -> Matrix:
    if sy is None:
        sy = sx
    return (sx, 0.0, 0.0, sy, 0.0, 0.0)


def rotate_matrix(angle_degrees: float, cx: Optional[float] = None, cy: Optional[float] = None) -> Matrix:
    theta = math.radians(angle_degrees)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    rotation = (cos_t, sin_t, -sin_t, cos_t, 0.0, 0.0)
    if cx is None or cy is None:
        return rotation
    return matrix_multiply(
        matrix_multiply(translate_matrix(cx, cy), rotation), translate_matrix(-cx, -cy)
    )


def skew_x_matrix(angle_degrees: float) -> Matrix:
    return (1.0, 0.0, math.tan(math.radians(angle_degrees)), 1.0, 0.0, 0.0)


def skew_y_matrix(angle_degrees: float) -> Matrix:
    return (1.0, math.tan(math.radians(angle_degrees)), 0.0, 1.0, 0.0, 0.0)


def parse_transform(transform: Optional[str]) -> Matrix:
    if not transform:
        return matrix_identity()
    combined = matrix_identity()
    for name, arg_text in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", transform):
        args = transform_numbers(arg_text)
        lower = name.lower()
        if lower == "matrix" and len(args) >= 6:
            current = (args[0], args[1], args[2], args[3], args[4], args[5])
        elif lower == "translate" and args:
            current = translate_matrix(args[0], args[1] if len(args) > 1 else 0.0)
        elif lower == "scale" and args:
            current = scale_matrix(args[0], args[1] if len(args) > 1 else None)
        elif lower == "rotate" and args:
            current = rotate_matrix(args[0], args[1], args[2]) if len(args) >= 3 else rotate_matrix(args[0])
        elif lower == "skewx" and args:
            current = skew_x_matrix(args[0])
        elif lower == "skewy" and args:
            current = skew_y_matrix(args[0])
        else:
            # Unknown transform: ignore instead of failing the whole file.
            current = matrix_identity()
        combined = matrix_multiply(combined, current)
    return combined


def get_attr_by_local_name(element: ET.Element, names: Sequence[str]) -> Optional[str]:
    wanted = {name.lower() for name in names}
    for key, value in element.attrib.items():
        if attr_local_name(key).lower() in wanted:
            return value
    return None


def get_curve_group_attr(element: ET.Element) -> Optional[str]:
    value = get_attr_by_local_name(
        element,
        (
            "data-curve",
            "data-group",
            "curve",
            "group",
            "path",
            "data-path",
            "data-name",
        ),
    )
    if value and value.strip():
        return value.strip()
    return None


def get_svg_group_name(element: ET.Element) -> Optional[str]:
    value = get_curve_group_attr(element)
    if value:
        return value
    label = get_attr_by_local_name(element, ("label",))
    if label and label.strip():
        return label.strip()
    group_id = element.get("id")
    if group_id and group_id.strip():
        return group_id.strip()
    return None


def parse_svg_points(path: str) -> "OrderedDict[str, List[Point]]":
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise PointParseError(f"Could not parse SVG XML: {exc}") from exc

    root = tree.getroot()
    groups: "OrderedDict[str, List[Point]]" = OrderedDict()

    def visit(element: ET.Element, inherited_group: Optional[str], inherited_matrix: Matrix) -> None:
        tag = local_name(element.tag).lower()
        element_transform = parse_transform(element.get("transform"))
        current_matrix = matrix_multiply(inherited_matrix, element_transform)
        current_group = inherited_group

        if tag == "g":
            current_group = get_svg_group_name(element) or inherited_group

        if tag in {"circle", "ellipse"}:
            point_group = get_curve_group_attr(element) or current_group or "curve_1"
            try:
                cx = parse_number(element.get("cx", "0"), "cx")
                cy = parse_number(element.get("cy", "0"), "cy")
            except PointParseError as exc:
                raise PointParseError(f"Circle/ellipse missing a valid center: {exc}") from exc
            point = matrix_apply(current_matrix, (cx, cy))
            groups.setdefault(point_group, []).append(point)

        for child in list(element):
            visit(child, current_group, current_matrix)

    visit(root, None, matrix_identity())

    if not groups:
        raise PointParseError(
            "No <circle> or <ellipse> elements were found in the SVG input."
        )
    return groups


def read_svg_frame(path: str) -> SvgFrame:
    """Read root SVG width/height/viewBox so output can align with input SVG."""
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise PointParseError(f"Could not parse SVG XML: {exc}") from exc

    root = tree.getroot()
    if local_name(root.tag).lower() != "svg":
        return SvgFrame()
    return SvgFrame(
        width=root.get("width"),
        height=root.get("height"),
        view_box=root.get("viewBox"),
        x=root.get("x"),
        y=root.get("y"),
    )


# ---------------------------------------------------------------------------
# Curve/group handling
# ---------------------------------------------------------------------------


def copy_groups(groups: "OrderedDict[str, List[Point]]") -> "OrderedDict[str, List[Point]]":
    return OrderedDict((name, list(points)) for name, points in groups.items())


def flatten_group_points(groups: "OrderedDict[str, List[Point]]") -> List[Point]:
    points: List[Point] = []
    for group_points in groups.values():
        points.extend(group_points)
    return points


def split_group_spec_entries(spec: str) -> List[str]:
    text = spec.strip()
    if not text:
        return []
    entries = [entry.strip() for entry in re.split(r"[;\n]+", text) if entry.strip()]
    if len(entries) == 1 and not re.search(r"[:=]", entries[0]):
        entries = [entry.strip() for entry in entries[0].split(",") if entry.strip()]
    return entries


def parse_index_expression(expression: str, total_points: int) -> List[int]:
    """Parse 1-based index lists such as '1-4,8,10-12'."""
    result: List[int] = []
    for token in [part.strip() for part in expression.split(",") if part.strip()]:
        range_match = re.match(r"^(\d+)\s*-\s*(\d+)$", token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            step = 1 if end >= start else -1
            values = range(start, end + step, step)
        elif re.match(r"^\d+$", token):
            values = [int(token)]
        else:
            raise PointParseError(f"Could not parse grouping index token {token!r}.")
        for value in values:
            if value < 1 or value > total_points:
                raise PointParseError(
                    f"Grouping index {value} is outside the valid range 1-{total_points}."
                )
            result.append(value - 1)
    return result


def apply_grouping_mode(
    groups: "OrderedDict[str, List[Point]]",
    group_mode: str = "preserve",
    group_spec: str = "",
) -> "OrderedDict[str, List[Point]]":
    """Optionally regroup parsed points before curve construction.

    group_mode values:
      - preserve: keep groups from text sections, CSV columns, SVG groups, or data-curve attributes.
      - all: merge every parsed point into one curve.
      - split: use group_spec with counts or 1-based index ranges.

    split examples:
      - "4,5,*" makes curve_1 from the first 4 points, curve_2 from the next 5,
        and curve_3 from all remaining points.
      - "outer:1-8; inner:9-16" uses explicit 1-based point indices.
      - "outer:8; inner:*" consumes points sequentially by count.
    """
    mode = (group_mode or "preserve").strip().lower().replace("-", "_")
    if mode in {"preserve", "input", "input_groups", "existing"}:
        return copy_groups(groups)

    flat_points = flatten_group_points(groups)
    if not flat_points:
        raise PointParseError("No points are available for grouping.")

    if mode in {"all", "single", "one"}:
        return OrderedDict([("curve_1", flat_points)])

    if mode not in {"split", "manual", "spec"}:
        raise PointParseError(f"Unsupported group mode: {group_mode!r}.")

    entries = split_group_spec_entries(group_spec)
    if not entries:
        raise PointParseError(
            "Group mode 'split' requires a group spec, for example '4,5,*' or "
            "'outer:1-8; inner:9-16'."
        )

    result: "OrderedDict[str, List[Point]]" = OrderedDict()
    cursor = 0
    used_indices = set()
    total = len(flat_points)

    for entry_index, entry in enumerate(entries, start=1):
        if ":" in entry:
            raw_name, expression = entry.split(":", 1)
        elif "=" in entry:
            raw_name, expression = entry.split("=", 1)
        else:
            raw_name, expression = f"curve_{entry_index}", entry
        name = raw_name.strip() or f"curve_{entry_index}"
        expression = expression.strip()
        if not expression:
            raise PointParseError(f"Missing grouping expression for {name!r}.")

        lower_expr = expression.lower()
        if lower_expr in {"*", "rest", "remaining"}:
            indices = [index for index in range(total) if index not in used_indices]
        elif re.match(r"^\d+$", expression):
            count = int(expression)
            if count < 0:
                raise PointParseError(f"Grouping count for {name!r} must be non-negative.")
            if cursor + count > total:
                raise PointParseError(
                    f"Grouping count for {name!r} asks for {count} point(s), but only "
                    f"{max(total - cursor, 0)} remain."
                )
            indices = list(range(cursor, cursor + count))
            cursor += count
        else:
            indices = parse_index_expression(expression, total)
            if indices:
                cursor = max(cursor, max(indices) + 1)

        used_indices.update(indices)
        result.setdefault(name, []).extend(flat_points[index] for index in indices)

    if not result:
        raise PointParseError("The grouping spec did not produce any curves.")
    return result


def write_points_text(
    groups: "OrderedDict[str, List[Point]]",
    output_path: str,
    precision: int = 6,
) -> None:
    """Write parsed/grouped points in a text format this script can read again."""
    lines: List[str] = [
        "# Points exported by point2curveV3.py",
        "# Each [section] is one curve; coordinates are x, y in SVG user units.",
        "",
    ]
    for group_name, points in groups.items():
        lines.append(f"[{group_name}]")
        for x, y in points:
            lines.append(f"{format_num(x, precision)}, {format_num(y, precision)}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


# ---------------------------------------------------------------------------
# Curve construction
# ---------------------------------------------------------------------------


def normalize_curve_mode(curve_mode: str) -> str:
    mode = (curve_mode or "closed").strip().lower().replace("-", "_")
    if mode in {"closed", "close", "loop", "cyclic", "periodic"}:
        return "closed"
    if mode in {"open", "opened", "unclosed"}:
        return "open"
    raise PointParseError(f"Unsupported curve mode: {curve_mode!r}. Use 'closed' or 'open'.")


def normalize_curve_method(method: str) -> str:
    value = (method or "elastic").strip().lower().replace("_", "-")
    aliases = {
        "elastic": "elastic",
        "spline": "elastic",
        "cubic": "elastic",
        "cubic-spline": "elastic",
        "natural": "elastic",
        "periodic": "elastic",
        "rod": "elastic",
        "elastic-rod": "elastic",
        "catmull-rom": "catmull-rom",
        "catmullrom": "catmull-rom",
        "catmull": "catmull-rom",
        "cr": "catmull-rom",
    }
    if value in aliases:
        return aliases[value]
    raise PointParseError(f"Unsupported curve method: {method!r}. Use 'elastic' or 'catmull-rom'.")


def clean_points_for_mode(points: Sequence[Point], closed: bool) -> Tuple[List[Point], int]:
    """Remove only duplicates that make zero-length spline intervals.

    For closed curves, a final point equal to the first point is treated as an
    explicit closure marker and removed before the final segment is generated.
    Non-adjacent repeated points are preserved for crossings/self-intersections.
    """
    removed = 0
    cleaned: List[Point] = []
    for point in points:
        if cleaned and same_point(cleaned[-1], point):
            removed += 1
            continue
        cleaned.append(point)

    if closed and len(cleaned) > 1 and same_point(cleaned[0], cleaned[-1]):
        cleaned.pop()
        removed += 1

    return cleaned, removed


def simplify_open_points(points: Sequence[Point], tolerance: float) -> List[Point]:
    """Ramer-Douglas-Peucker simplification for open polylines."""
    pts = list(points)
    if tolerance <= 0 or len(pts) <= 2:
        return pts

    def rdp(start_index: int, end_index: int) -> List[Point]:
        start_point = pts[start_index]
        end_point = pts[end_index]
        max_distance = -1.0
        max_index = start_index
        for index in range(start_index + 1, end_index):
            d = distance_point_to_segment(pts[index], start_point, end_point)
            if d > max_distance:
                max_distance = d
                max_index = index
        if max_distance > tolerance:
            left = rdp(start_index, max_index)
            right = rdp(max_index, end_index)
            return left[:-1] + right
        return [start_point, end_point]

    return rdp(0, len(pts) - 1)


def simplify_closed_points(points: Sequence[Point], tolerance: float) -> List[Point]:
    """Simple local closed-polyline simplifier.

    This keeps at least three points. It is intentionally conservative and is
    used only when the user requests an approximate curve.
    """
    pts = list(points)
    if tolerance <= 0 or len(pts) <= 3:
        return pts

    changed = True
    while changed and len(pts) > 3:
        changed = False
        best_index = None
        best_distance = None
        n = len(pts)
        for i in range(n):
            prev_point = pts[(i - 1) % n]
            point = pts[i]
            next_point = pts[(i + 1) % n]
            d = distance_point_to_segment(point, prev_point, next_point)
            if d <= tolerance and (best_distance is None or d < best_distance):
                best_distance = d
                best_index = i
        if best_index is not None:
            del pts[best_index]
            changed = True
    return pts


def parameter_step(a: Point, b: Point, alpha: float) -> float:
    d = distance(a, b)
    if d <= EPSILON:
        return EPSILON
    if alpha <= 0:
        return 1.0
    return max(d ** alpha, EPSILON)


def interval_steps(points: Sequence[Point], closed: bool, alpha: float) -> List[float]:
    pts = list(points)
    n = len(pts)
    if closed:
        return [parameter_step(pts[i], pts[(i + 1) % n], alpha) for i in range(n)]
    return [parameter_step(pts[i], pts[i + 1], alpha) for i in range(n - 1)]


def solve_linear_system_dense_multi(
    matrix: Sequence[Sequence[float]],
    rhs: Sequence[Sequence[float]],
) -> List[List[float]]:
    """Solve A*x=b for multiple right-hand sides using Gaussian elimination.

    This fallback keeps the script dependency-free. NumPy is used automatically
    by solve_linear_system_multi when it is available.
    """
    n = len(matrix)
    if n == 0:
        return []
    rhs_width = len(rhs[0]) if rhs else 0
    augmented = [list(matrix[row]) + list(rhs[row]) for row in range(n)]

    for col in range(n):
        pivot_row = max(range(col, n), key=lambda row: abs(augmented[row][col]))
        pivot_value = augmented[pivot_row][col]
        if abs(pivot_value) <= EPSILON:
            raise PointParseError(
                "The spline system is singular. Check for duplicate or badly ordered points."
            )
        if pivot_row != col:
            augmented[col], augmented[pivot_row] = augmented[pivot_row], augmented[col]

        pivot_value = augmented[col][col]
        for row in range(col + 1, n):
            factor = augmented[row][col] / pivot_value
            if abs(factor) <= EPSILON:
                continue
            augmented[row][col] = 0.0
            for k in range(col + 1, n + rhs_width):
                augmented[row][k] -= factor * augmented[col][k]

    solution = [[0.0 for _ in range(rhs_width)] for _ in range(n)]
    for row in range(n - 1, -1, -1):
        diagonal = augmented[row][row]
        if abs(diagonal) <= EPSILON:
            raise PointParseError("The spline system is singular during back substitution.")
        for rhs_col in range(rhs_width):
            value = augmented[row][n + rhs_col]
            for col in range(row + 1, n):
                value -= augmented[row][col] * solution[col][rhs_col]
            solution[row][rhs_col] = value / diagonal
    return solution


def solve_linear_system_multi(
    matrix: Sequence[Sequence[float]],
    rhs: Sequence[Sequence[float]],
) -> List[List[float]]:
    """Solve a dense linear system for one or more right-hand sides.

    NumPy is optional. If it is unavailable, the script falls back to a pure
    Python solver. The systems here are small to moderate because there is one
    equation per anchor point.
    """
    try:  # pragma: no cover - optional dependency path.
        import numpy as np  # type: ignore

        a = np.array(matrix, dtype=float)
        b = np.array(rhs, dtype=float)
        x = np.linalg.solve(a, b)
        return [[float(value) for value in row] for row in x.tolist()]
    except ImportError:
        return solve_linear_system_dense_multi(matrix, rhs)
    except Exception:
        # Fall back to the dependency-free solver for environments with a
        # partially installed or incompatible NumPy. Real singular systems will
        # still raise a clear error from the fallback.
        return solve_linear_system_dense_multi(matrix, rhs)


def natural_open_spline_derivatives(points: Sequence[Point], alpha: float) -> List[Point]:
    """Derivative vectors for a natural cubic spline through open points."""
    pts = list(points)
    n = len(pts)
    if n < 2:
        raise PointParseError("At least two points are required for an open curve.")

    h = interval_steps(pts, closed=False, alpha=alpha)
    if n == 2:
        derivative = div(sub(pts[1], pts[0]), h[0])
        return [derivative, derivative]

    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    rhs: List[List[float]] = [[0.0, 0.0] for _ in range(n)]

    matrix[0][0] = 2.0
    matrix[0][1] = 1.0
    first_delta = div(sub(pts[1], pts[0]), h[0])
    rhs[0] = [3.0 * first_delta[0], 3.0 * first_delta[1]]

    for i in range(1, n - 1):
        h_prev = h[i - 1]
        h_next = h[i]
        matrix[i][i - 1] = h_next
        matrix[i][i] = 2.0 * (h_prev + h_next)
        matrix[i][i + 1] = h_prev
        prev_delta = div(sub(pts[i], pts[i - 1]), h_prev)
        next_delta = div(sub(pts[i + 1], pts[i]), h_next)
        rhs_vec = add(mul(prev_delta, h_next), mul(next_delta, h_prev))
        rhs[i] = [3.0 * rhs_vec[0], 3.0 * rhs_vec[1]]

    matrix[n - 1][n - 2] = 1.0
    matrix[n - 1][n - 1] = 2.0
    last_delta = div(sub(pts[n - 1], pts[n - 2]), h[-1])
    rhs[n - 1] = [3.0 * last_delta[0], 3.0 * last_delta[1]]

    solution = solve_linear_system_multi(matrix, rhs)
    return [(row[0], row[1]) for row in solution]


def periodic_closed_spline_derivatives(points: Sequence[Point], alpha: float) -> List[Point]:
    """Derivative vectors for a periodic cubic spline through closed points."""
    pts = list(points)
    n = len(pts)
    if n < 3:
        raise PointParseError("At least three points are required for a closed curve.")

    h = interval_steps(pts, closed=True, alpha=alpha)
    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    rhs: List[List[float]] = [[0.0, 0.0] for _ in range(n)]

    for i in range(n):
        prev_i = (i - 1) % n
        next_i = (i + 1) % n
        h_prev = h[prev_i]
        h_next = h[i]

        matrix[i][prev_i] += h_next
        matrix[i][i] += 2.0 * (h_prev + h_next)
        matrix[i][next_i] += h_prev

        prev_delta = div(sub(pts[i], pts[prev_i]), h_prev)
        next_delta = div(sub(pts[next_i], pts[i]), h_next)
        rhs_vec = add(mul(prev_delta, h_next), mul(next_delta, h_prev))
        rhs[i] = [3.0 * rhs_vec[0], 3.0 * rhs_vec[1]]

    solution = solve_linear_system_multi(matrix, rhs)
    return [(row[0], row[1]) for row in solution]


def elastic_spline_to_beziers(
    points: Sequence[Point],
    closed: bool,
    smoothness: float = 1.0,
    alpha: float = 0.5,
) -> List[Tuple[Point, Point, Point, Point]]:
    """Convert natural/periodic cubic spline interpolation to Bezier segments."""
    pts = list(points)
    n = len(pts)
    if closed and n < 3:
        raise ValueError("At least three points are required for a closed curve.")
    if not closed and n < 2:
        raise ValueError("At least two points are required for an open curve.")

    smoothness = max(0.0, float(smoothness))
    alpha = max(0.0, float(alpha))
    h = interval_steps(pts, closed=closed, alpha=alpha)
    derivatives = (
        periodic_closed_spline_derivatives(pts, alpha=alpha)
        if closed
        else natural_open_spline_derivatives(pts, alpha=alpha)
    )

    segments: List[Tuple[Point, Point, Point, Point]] = []
    segment_count = n if closed else n - 1
    for i in range(segment_count):
        j = (i + 1) % n
        p1 = pts[i]
        p2 = pts[j]
        step = h[i]
        c1 = add(p1, mul(derivatives[i], smoothness * step / 3.0))
        c2 = sub(p2, mul(derivatives[j], smoothness * step / 3.0))
        segments.append((p1, c1, c2, p2))
    return segments


def catmull_rom_tangent(p0: Point, p1: Point, p2: Point, t0: float, t1: float, t2: float) -> Point:
    dt10 = max(t1 - t0, EPSILON)
    dt21 = max(t2 - t1, EPSILON)
    dt20 = max(t2 - t0, EPSILON)
    term_a = mul(div(sub(p2, p1), dt21), t1 - t0)
    term_b = mul(div(sub(p1, p0), dt10), t2 - t1)
    return div(add(term_a, term_b), dt20)


def catmull_rom_derivatives(points: Sequence[Point], closed: bool, alpha: float) -> List[Point]:
    pts = list(points)
    n = len(pts)
    alpha = max(0.0, float(alpha))
    derivatives: List[Point] = []

    for i in range(n):
        if closed:
            p0 = pts[(i - 1) % n]
            p1 = pts[i]
            p2 = pts[(i + 1) % n]
            t0 = 0.0
            t1 = t0 + parameter_step(p0, p1, alpha)
            t2 = t1 + parameter_step(p1, p2, alpha)
            derivatives.append(catmull_rom_tangent(p0, p1, p2, t0, t1, t2))
        else:
            if i == 0:
                step = parameter_step(pts[0], pts[1], alpha)
                derivatives.append(div(sub(pts[1], pts[0]), step))
            elif i == n - 1:
                step = parameter_step(pts[n - 2], pts[n - 1], alpha)
                derivatives.append(div(sub(pts[n - 1], pts[n - 2]), step))
            else:
                p0 = pts[i - 1]
                p1 = pts[i]
                p2 = pts[i + 1]
                t0 = 0.0
                t1 = t0 + parameter_step(p0, p1, alpha)
                t2 = t1 + parameter_step(p1, p2, alpha)
                derivatives.append(catmull_rom_tangent(p0, p1, p2, t0, t1, t2))
    return derivatives


def catmull_rom_to_beziers(
    points: Sequence[Point],
    closed: bool,
    smoothness: float = 1.0,
    alpha: float = 0.5,
) -> List[Tuple[Point, Point, Point, Point]]:
    pts = list(points)
    n = len(pts)
    if closed and n < 3:
        raise ValueError("At least three points are required for a closed curve.")
    if not closed and n < 2:
        raise ValueError("At least two points are required for an open curve.")

    smoothness = max(0.0, float(smoothness))
    alpha = max(0.0, float(alpha))
    h = interval_steps(pts, closed=closed, alpha=alpha)
    derivatives = catmull_rom_derivatives(pts, closed=closed, alpha=alpha)

    segments: List[Tuple[Point, Point, Point, Point]] = []
    segment_count = n if closed else n - 1
    for i in range(segment_count):
        j = (i + 1) % n
        p1 = pts[i]
        p2 = pts[j]
        step = h[i]
        c1 = add(p1, mul(derivatives[i], smoothness * step / 3.0))
        c2 = sub(p2, mul(derivatives[j], smoothness * step / 3.0))
        segments.append((p1, c1, c2, p2))
    return segments


def build_svg_path_d(
    segments: Sequence[Tuple[Point, Point, Point, Point]],
    precision: int,
    close_with_z: bool = False,
) -> str:
    if not segments:
        return ""
    start = segments[0][0]
    pieces = [f"M {format_num(start[0], precision)} {format_num(start[1], precision)}"]
    for _p1, c1, c2, p2 in segments:
        pieces.append(
            "C "
            + " ".join(
                [
                    format_num(c1[0], precision),
                    format_num(c1[1], precision),
                    format_num(c2[0], precision),
                    format_num(c2[1], precision),
                    format_num(p2[0], precision),
                    format_num(p2[1], precision),
                ]
            )
        )
    if close_with_z:
        pieces.append("Z")
    return " ".join(pieces)


def build_curves(
    groups: "OrderedDict[str, List[Point]]",
    smoothness: float,
    alpha: float,
    simplify_tolerance: float,
    precision: int,
    close_with_z: bool = True,
    curve_mode: str = "closed",
    method: str = "elastic",
) -> Tuple[List[CurveResult], List[str]]:
    results: List[CurveResult] = []
    warnings: List[str] = []
    mode = normalize_curve_mode(curve_mode)
    spline_method = normalize_curve_method(method)
    closed = mode == "closed"

    if spline_method == "elastic" and abs(float(smoothness) - 1.0) > 1.0e-9:
        warnings.append(
            "Using --smoothness values other than 1.0 changes handle lengths after the elastic spline solve. "
            "Tangents remain continuous, but exact minimum-bending/natural-spline curvature is best represented by --smoothness 1.0."
        )

    for group_name, raw_points in groups.items():
        points, removed = clean_points_for_mode(raw_points, closed=closed)
        if removed:
            warnings.append(
                f"{group_name}: removed {removed} adjacent/closing duplicate point(s)."
            )
        if simplify_tolerance > 0:
            before = len(points)
            points = (
                simplify_closed_points(points, simplify_tolerance)
                if closed
                else simplify_open_points(points, simplify_tolerance)
            )
            after = len(points)
            if after < before:
                warnings.append(
                    f"{group_name}: simplified from {before} to {after} anchor point(s); "
                    "curve is approximate."
                )

        min_points = 3 if closed else 2
        if len(points) < min_points:
            curve_kind = "closed" if closed else "open"
            warnings.append(
                f"{group_name}: skipped because a {curve_kind} curve requires at least {min_points} point(s)."
            )
            continue

        if spline_method == "elastic":
            segments = elastic_spline_to_beziers(
                points,
                closed=closed,
                smoothness=smoothness,
                alpha=alpha,
            )
        else:
            segments = catmull_rom_to_beziers(
                points,
                closed=closed,
                smoothness=smoothness,
                alpha=alpha,
            )

        path_d = build_svg_path_d(
            segments,
            precision=precision,
            close_with_z=close_with_z and closed,
        )
        results.append(CurveResult(group_name, points, path_d, segments))

    if not results:
        needed = "3 points" if closed else "2 points"
        raise PointParseError(f"No valid {mode} curve groups with at least {needed} were found.")
    return results, warnings


# ---------------------------------------------------------------------------
# SVG output writer
# ---------------------------------------------------------------------------


def collect_bbox_points(curves: Sequence[CurveResult]) -> List[Point]:
    bbox_points: List[Point] = []
    for curve in curves:
        bbox_points.extend(curve.points)
        for p1, c1, c2, p2 in curve.segments:
            bbox_points.extend([p1, c1, c2, p2])
    return bbox_points


def calculate_view_box(curves: Sequence[CurveResult], padding: float) -> Tuple[float, float, float, float]:
    pts = collect_bbox_points(curves)
    min_x = min(p[0] for p in pts)
    max_x = max(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_y = max(p[1] for p in pts)
    if abs(max_x - min_x) < EPSILON:
        min_x -= 1.0
        max_x += 1.0
    if abs(max_y - min_y) < EPSILON:
        min_y -= 1.0
        max_y += 1.0
    padding = max(0.0, padding)
    return (
        min_x - padding,
        min_y - padding,
        (max_x - min_x) + 2.0 * padding,
        (max_y - min_y) + 2.0 * padding,
    )


def escape_attr(value: object) -> str:
    return escape(str(value), {'"': "&quot;"})


def build_svg_root_tag(
    curves: Sequence[CurveResult],
    padding: float,
    precision: int,
    frame: Optional[SvgFrame] = None,
) -> str:
    attrs = ['xmlns="http://www.w3.org/2000/svg"']

    if frame is not None and frame.has_any_geometry():
        # Preserve the input SVG frame exactly enough for overlay/alignment work.
        if frame.x:
            attrs.append(f'x="{escape_attr(frame.x)}"')
        if frame.y:
            attrs.append(f'y="{escape_attr(frame.y)}"')
        if frame.width:
            attrs.append(f'width="{escape_attr(frame.width)}"')
        if frame.height:
            attrs.append(f'height="{escape_attr(frame.height)}"')
        if frame.view_box:
            attrs.append(f'viewBox="{escape_attr(frame.view_box)}"')
    else:
        view_x, view_y, view_w, view_h = calculate_view_box(curves, padding)
        width = max(view_w, 1.0)
        height = max(view_h, 1.0)
        attrs.append(f'width="{format_num(width, precision)}"')
        attrs.append(f'height="{format_num(height, precision)}"')
        attrs.append(
            f'viewBox="{format_num(view_x, precision)} {format_num(view_y, precision)} '
            f'{format_num(view_w, precision)} {format_num(view_h, precision)}"'
        )

    return "<svg " + " ".join(attrs) + ">"


def write_svg_output(
    curves: Sequence[CurveResult],
    output_path: str,
    stroke: str = "#000000",
    stroke_width: float = 2.0,
    fill: str = "none",
    padding: float = 10.0,
    show_points: bool = False,
    show_handles: bool = False,
    point_radius: float = 2.0,
    precision: int = 6,
    frame: Optional[SvgFrame] = None,
) -> None:
    safe_stroke = escape_attr(stroke)
    safe_fill = escape_attr(fill)

    lines: List[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(build_svg_root_tag(curves, padding, precision, frame=frame))
    lines.append("  <title>Curves generated by point2curveV3.py</title>")
    lines.append("  <g id=\"curves\">")

    safe_ids = unique_names(curve.name for curve in curves)
    for curve, safe_id in zip(curves, safe_ids):
        lines.append(
            f'    <path id="{escape(safe_id)}" d="{escape(curve.path_d)}" '
            f'fill="{safe_fill}" stroke="{safe_stroke}" '
            f'stroke-width="{format_num(stroke_width, precision)}" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    if show_handles:
        lines.append('    <g id="bezier_handles" fill="none" opacity="0.45">')
        for curve, safe_id in zip(curves, safe_ids):
            lines.append(f'      <g id="{escape(safe_id)}_handles">')
            for p1, c1, c2, p2 in curve.segments:
                lines.append(
                    '        <line '
                    f'x1="{format_num(p1[0], precision)}" y1="{format_num(p1[1], precision)}" '
                    f'x2="{format_num(c1[0], precision)}" y2="{format_num(c1[1], precision)}" '
                    f'stroke="{safe_stroke}" stroke-width="{format_num(max(stroke_width * 0.35, 0.5), precision)}"/>'
                )
                lines.append(
                    '        <line '
                    f'x1="{format_num(p2[0], precision)}" y1="{format_num(p2[1], precision)}" '
                    f'x2="{format_num(c2[0], precision)}" y2="{format_num(c2[1], precision)}" '
                    f'stroke="{safe_stroke}" stroke-width="{format_num(max(stroke_width * 0.35, 0.5), precision)}"/>'
                )
            lines.append("      </g>")
        lines.append("    </g>")

    if show_points:
        lines.append('    <g id="anchor_points">')
        for curve, safe_id in zip(curves, safe_ids):
            lines.append(f'      <g id="{escape(safe_id)}_anchors">')
            for x, y in curve.points:
                lines.append(
                    '        <circle '
                    f'cx="{format_num(x, precision)}" cy="{format_num(y, precision)}" '
                    f'r="{format_num(point_radius, precision)}" fill="{safe_stroke}" stroke="none"/>'
                )
            lines.append("      </g>")
        lines.append("    </g>")

    lines.append("  </g>")
    lines.append("</svg>")

    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def resolve_input_format(input_path: str, input_format: str = "auto") -> str:
    fmt = input_format.lower()
    if fmt == "auto":
        ext = os.path.splitext(input_path)[1].lower()
        fmt = "svg" if ext == ".svg" else "text"
    if fmt in {"svg", "text", "txt", "csv"}:
        return "text" if fmt in {"txt", "csv"} else fmt
    raise PointParseError(f"Unsupported input format: {input_format!r}")


def load_point_groups(input_path: str, input_format: str = "auto") -> "OrderedDict[str, List[Point]]":
    fmt = resolve_input_format(input_path, input_format)
    if fmt == "svg":
        return parse_svg_points(input_path)
    if fmt == "text":
        return parse_text_points(input_path)
    raise PointParseError(f"Unsupported input format: {input_format!r}")


def process_file(
    input_path: str,
    output_path: Optional[str] = None,
    input_format: str = "auto",
    smoothness: float = 1.0,
    alpha: float = 0.5,
    stroke: str = "#000000",
    stroke_width: float = 2.0,
    fill: str = "none",
    padding: float = 10.0,
    show_points: bool = False,
    show_handles: bool = False,
    point_radius: float = 2.0,
    simplify_tolerance: float = 0.0,
    precision: int = 6,
    keep_svg_frame: bool = False,
    export_points_path: Optional[str] = None,
    group_mode: str = "preserve",
    group_spec: str = "",
    close_with_z: bool = True,
    curve_mode: str = "closed",
    method: str = "elastic",
) -> Tuple[str, List[str], List[CurveResult]]:
    if precision < 0:
        raise PointParseError("Precision must be 0 or greater.")
    if stroke_width < 0:
        raise PointParseError("Stroke width must be 0 or greater.")
    if point_radius < 0:
        raise PointParseError("Point radius must be 0 or greater.")
    if simplify_tolerance < 0:
        raise PointParseError("Simplify tolerance must be 0 or greater.")
    normalize_curve_mode(curve_mode)
    normalize_curve_method(method)
    if not input_path:
        raise PointParseError("An input file is required.")
    if not os.path.exists(input_path):
        raise PointParseError(f"Input file not found: {input_path}")
    if output_path is None or not output_path.strip():
        output_path = derive_output_name(input_path)

    fmt = resolve_input_format(input_path, input_format)
    groups = load_point_groups(input_path, input_format=fmt)
    groups = apply_grouping_mode(groups, group_mode=group_mode, group_spec=group_spec)

    if export_points_path is not None:
        point_path = export_points_path.strip() or derive_points_output_name(input_path)
        write_points_text(groups, point_path, precision=precision)

    curves, warnings = build_curves(
        groups,
        smoothness=smoothness,
        alpha=alpha,
        simplify_tolerance=simplify_tolerance,
        precision=precision,
        close_with_z=close_with_z,
        curve_mode=curve_mode,
        method=method,
    )

    frame: Optional[SvgFrame] = None
    if keep_svg_frame:
        if fmt == "svg":
            frame = read_svg_frame(input_path)
            if not frame.has_any_geometry():
                warnings.append("Input SVG has no width/height/viewBox frame to preserve.")
        else:
            warnings.append("--keep-svg-frame was ignored because the input is not SVG.")

    write_svg_output(
        curves,
        output_path=output_path,
        stroke=stroke,
        stroke_width=stroke_width,
        fill=fill,
        padding=padding,
        show_points=show_points,
        show_handles=show_handles,
        point_radius=point_radius,
        precision=precision,
        frame=frame,
    )
    return output_path, warnings, curves


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


def run_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:  # pragma: no cover - depends on local Python build.
        print(f"GUI mode requires tkinter, but it could not be imported: {exc}", file=sys.stderr)
        return 2

    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - depends on display availability.
        print(f"Could not open the GUI window: {exc}", file=sys.stderr)
        return 2

    root.title("point2curveV3.py")
    root.geometry("960x760")
    root.minsize(880, 660)

    input_var = tk.StringVar()
    output_var = tk.StringVar()
    format_var = tk.StringVar(value="auto")
    keep_svg_frame_var = tk.BooleanVar(value=False)
    export_points_var = tk.BooleanVar(value=False)
    export_points_path_var = tk.StringVar()
    group_mode_var = tk.StringVar(value="preserve")
    group_spec_var = tk.StringVar()
    curve_mode_var = tk.StringVar(value="closed")
    method_var = tk.StringVar(value="elastic")
    smoothness_var = tk.StringVar(value="1.0")
    alpha_var = tk.StringVar(value="0.5")
    close_with_z_var = tk.BooleanVar(value=True)
    stroke_var = tk.StringVar(value="#000000")
    stroke_width_var = tk.StringVar(value="2.0")
    fill_var = tk.StringVar(value="none")
    padding_var = tk.StringVar(value="10.0")
    point_radius_var = tk.StringVar(value="2.0")
    simplify_var = tk.StringVar(value="0.0")
    precision_var = tk.StringVar(value="6")
    show_points_var = tk.BooleanVar(value=False)
    show_handles_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="Choose an input file, then generate the SVG.")

    def show_help(title: str, message: str) -> None:
        messagebox.showinfo(title, message)

    def help_button(parent: "tk.Widget", title: str, message: str) -> "tk.Button":
        return tk.Button(
            parent,
            text="?",
            width=2,
            bg="#cfefff",
            activebackground="#b9e3ff",
            fg="#003a5d",
            relief="groove",
            command=lambda: show_help(title, message),
        )

    def add_help(parent: "tk.Widget", row: int, title: str, message: str) -> None:
        help_button(parent, title, message).grid(row=row, column=3, padx=(8, 0), pady=4, sticky="w")

    def configure_tab(tab: "ttk.Frame") -> None:
        tab.columnconfigure(1, weight=1)
        tab.columnconfigure(2, weight=0)
        tab.columnconfigure(3, weight=0)

    def browse_input() -> None:
        filename = filedialog.askopenfilename(
            title="Choose point text/CSV or SVG file",
            filetypes=(
                ("Point or SVG files", "*.txt *.csv *.tsv *.svg"),
                ("SVG files", "*.svg"),
                ("Text/CSV files", "*.txt *.csv *.tsv"),
                ("All files", "*.*"),
            ),
        )
        if filename:
            input_var.set(filename)
            if not output_var.get().strip():
                output_var.set(derive_output_name(filename))
            if not export_points_path_var.get().strip():
                export_points_path_var.set(derive_points_output_name(filename))
            keep_svg_frame_var.set(os.path.splitext(filename)[1].lower() == ".svg")
            status_var.set("Input selected. You can preview groups or generate the SVG.")

    def browse_output() -> None:
        if output_var.get().strip():
            initial = output_var.get().strip()
        elif input_var.get().strip():
            initial = derive_output_name(input_var.get().strip())
        else:
            initial = "curves.svg"

        options = {
            "title": "Save output SVG as",
            "defaultextension": ".svg",
            "initialfile": os.path.basename(initial),
            "filetypes": (("SVG files", "*.svg"), ("All files", "*.*")),
        }
        initial_dir = os.path.dirname(initial)
        if initial_dir:
            options["initialdir"] = initial_dir
        filename = filedialog.asksaveasfilename(**options)
        if filename:
            output_var.set(filename)

    def browse_points_output() -> None:
        if export_points_path_var.get().strip():
            initial = export_points_path_var.get().strip()
        elif input_var.get().strip():
            initial = derive_points_output_name(input_var.get().strip())
        else:
            initial = "points_export.txt"

        options = {
            "title": "Save parsed points as",
            "defaultextension": ".txt",
            "initialfile": os.path.basename(initial),
            "filetypes": (("Text files", "*.txt"), ("CSV files", "*.csv"), ("All files", "*.*")),
        }
        initial_dir = os.path.dirname(initial)
        if initial_dir:
            options["initialdir"] = initial_dir
        filename = filedialog.asksaveasfilename(**options)
        if filename:
            export_points_path_var.set(filename)
            export_points_var.set(True)

    def parse_float(value: str, label: str) -> float:
        try:
            return float(value)
        except ValueError as exc:
            raise PointParseError(f"{label} must be a number.") from exc

    def parse_int(value: str, label: str) -> int:
        try:
            return int(value)
        except ValueError as exc:
            raise PointParseError(f"{label} must be an integer.") from exc

    def current_export_points_path() -> Optional[str]:
        if not export_points_var.get():
            return None
        input_path = input_var.get().strip()
        return export_points_path_var.get().strip() or (derive_points_output_name(input_path) if input_path else "points_export.txt")

    def load_current_groups() -> "OrderedDict[str, List[Point]]":
        input_path = input_var.get().strip()
        if not input_path:
            raise PointParseError("An input file is required.")
        if not os.path.exists(input_path):
            raise PointParseError(f"Input file not found: {input_path}")
        groups = load_point_groups(input_path, input_format=format_var.get())
        return apply_grouping_mode(
            groups,
            group_mode=group_mode_var.get(),
            group_spec=group_spec_var.get(),
        )

    def preview_groups() -> None:
        try:
            groups = load_current_groups()
        except Exception as exc:
            messagebox.showerror("point2curveV3.py group preview error", str(exc))
            return
        lines = [f"Detected {len(groups)} curve group(s):"]
        for name, points in groups.items():
            lines.append(f"  {name}: {len(points)} point(s)")
        lines.append("")
        if normalize_curve_mode(curve_mode_var.get()) == "closed":
            lines.append("Closed mode: groups with fewer than 3 points will be skipped during curve generation.")
        else:
            lines.append("Open mode: groups with fewer than 2 points will be skipped during curve generation.")
        messagebox.showinfo("Point groups", "\n".join(lines))

    def generate() -> None:
        export_path = current_export_points_path()
        try:
            output_path, warnings, curves = process_file(
                input_path=input_var.get().strip(),
                output_path=output_var.get().strip() or None,
                input_format=format_var.get(),
                smoothness=parse_float(smoothness_var.get(), "Smoothness"),
                alpha=parse_float(alpha_var.get(), "Alpha"),
                stroke=stroke_var.get().strip() or "#000000",
                stroke_width=parse_float(stroke_width_var.get(), "Stroke width"),
                fill=fill_var.get().strip() or "none",
                padding=parse_float(padding_var.get(), "Padding"),
                show_points=show_points_var.get(),
                show_handles=show_handles_var.get(),
                point_radius=parse_float(point_radius_var.get(), "Point radius"),
                simplify_tolerance=parse_float(simplify_var.get(), "Simplify tolerance"),
                precision=parse_int(precision_var.get(), "Precision"),
                keep_svg_frame=keep_svg_frame_var.get(),
                export_points_path=export_path,
                group_mode=group_mode_var.get(),
                group_spec=group_spec_var.get(),
                close_with_z=close_with_z_var.get(),
                curve_mode=curve_mode_var.get(),
                method=method_var.get(),
            )
        except Exception as exc:
            messagebox.showerror("point2curveV3.py error", str(exc))
            status_var.set("Error: " + str(exc))
            return

        summary = f"Created {len(curves)} curve(s).\nSaved SVG:\n{output_path}"
        if export_path is not None:
            summary += f"\n\nExported points:\n{export_path}"
        if warnings:
            summary += "\n\nWarnings:\n" + "\n".join(warnings)
        messagebox.showinfo("point2curveV3.py", summary)
        status_var.set(f"Created {len(curves)} curve(s).")

    main_frame = ttk.Frame(root, padding=12)
    main_frame.pack(fill="both", expand=True)
    main_frame.rowconfigure(0, weight=1)
    main_frame.columnconfigure(0, weight=1)

    notebook = ttk.Notebook(main_frame)
    notebook.grid(row=0, column=0, sticky="nsew")

    files_tab = ttk.Frame(notebook, padding=14)
    grouping_tab = ttk.Frame(notebook, padding=14)
    curve_tab = ttk.Frame(notebook, padding=14)
    style_tab = ttk.Frame(notebook, padding=14)
    for tab in (files_tab, grouping_tab, curve_tab, style_tab):
        configure_tab(tab)

    notebook.add(files_tab, text="Files")
    notebook.add(grouping_tab, text="Grouping")
    notebook.add(curve_tab, text="Curve")
    notebook.add(style_tab, text="Style / View")

    row = 0
    ttk.Label(files_tab, text="Input file").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Entry(files_tab, textvariable=input_var).grid(row=row, column=1, sticky="ew", pady=4)
    ttk.Button(files_tab, text="Browse...", command=browse_input).grid(row=row, column=2, padx=(8, 0), pady=4)
    add_help(
        files_tab,
        row,
        "Input file",
        "Choose a text/CSV file with point coordinates or an SVG file containing circle/ellipse point markers. "
        "For SVG files, each circle or ellipse center is used as one point.",
    )

    row += 1
    ttk.Label(files_tab, text="Output SVG").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Entry(files_tab, textvariable=output_var).grid(row=row, column=1, sticky="ew", pady=4)
    ttk.Button(files_tab, text="Browse...", command=browse_output).grid(row=row, column=2, padx=(8, 0), pady=4)
    add_help(files_tab, row, "Output SVG", "Path for the generated SVG curve file. If blank, INPUT_curve.svg is used.")

    row += 1
    ttk.Label(files_tab, text="Input format").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Combobox(
        files_tab,
        textvariable=format_var,
        values=("auto", "text", "svg"),
        state="readonly",
        width=12,
    ).grid(row=row, column=1, sticky="w", pady=4)
    ttk.Label(files_tab, text="auto usually works").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(files_tab, row, "Input format", "Use auto to infer SVG from .svg extension; otherwise text/CSV is used.")

    row += 1
    ttk.Checkbutton(files_tab, text="Keep input SVG size and position", variable=keep_svg_frame_var).grid(
        row=row, column=1, sticky="w", pady=4
    )
    ttk.Label(files_tab, text="SVG input only").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(
        files_tab,
        row,
        "Keep input SVG frame",
        "When enabled for SVG input, the output preserves the root SVG x, y, width, height, and viewBox. "
        "This keeps the generated curve aligned with the original SVG when the two files are overlaid. Padding is ignored in this mode.",
    )

    row += 1
    ttk.Checkbutton(files_tab, text="Export parsed/grouped points as text", variable=export_points_var).grid(
        row=row, column=1, sticky="w", pady=4
    )
    ttk.Button(files_tab, text="Save as...", command=browse_points_output).grid(row=row, column=2, padx=(8, 0), pady=4)
    add_help(
        files_tab,
        row,
        "Export points",
        "Writes the parsed points to a reusable text file. This is especially useful for SVG input because it converts circle centers into [curve] sections with x, y coordinates.",
    )

    row += 1
    ttk.Label(files_tab, text="Points text output").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Entry(files_tab, textvariable=export_points_path_var).grid(row=row, column=1, sticky="ew", pady=4)
    ttk.Label(files_tab, text="optional").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(files_tab, row, "Points text output", "Optional path for the exported point list. If blank, INPUT_points.txt is used.")

    row += 1
    ttk.Label(
        files_tab,
        text=(
            "Tip: point order matters. For SVG input, circles are read in document order within each group. "
            "Use the Grouping tab to keep existing groups or split points manually."
        ),
        wraplength=760,
        foreground="#555555",
    ).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(18, 0))

    row = 0
    ttk.Label(grouping_tab, text="Group mode").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Combobox(
        grouping_tab,
        textvariable=group_mode_var,
        values=("preserve", "all", "split"),
        state="readonly",
        width=14,
    ).grid(row=row, column=1, sticky="w", pady=4)
    ttk.Button(grouping_tab, text="Preview groups", command=preview_groups).grid(row=row, column=2, padx=(8, 0), pady=4)
    add_help(
        grouping_tab,
        row,
        "Group mode",
        "preserve: use groups already present in the input. Text files can use [curve] sections or a curve/group column; SVG files can use <g id='curve_name'> or data-curve attributes.\n\n"
        "all: merge all parsed points into a single curve.\n\n"
        "split: divide all parsed points using the Group spec below.",
    )

    row += 1
    ttk.Label(grouping_tab, text="Group spec").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Entry(grouping_tab, textvariable=group_spec_var).grid(row=row, column=1, sticky="ew", pady=4)
    ttk.Label(grouping_tab, text="used only in split mode").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(
        grouping_tab,
        row,
        "Group spec",
        "Examples:\n"
        "  4,5,*\n"
        "    curve_1 gets the first 4 points, curve_2 gets the next 5, curve_3 gets the rest.\n\n"
        "  outer:1-8; inner:9-16\n"
        "    uses explicit 1-based point indices.\n\n"
        "  outer:8; inner:*\n"
        "    consumes 8 points for outer, then all remaining points for inner.",
    )

    row += 1
    ttk.Label(
        grouping_tab,
        text=(
            "The current script already supports multiple curves directly from input files. "
            "Use [curve_name] sections or a CSV curve/group column for text input; use SVG <g id=...> groups or data-curve attributes for SVG input."
        ),
        wraplength=760,
        foreground="#555555",
    ).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(18, 0))

    row = 0
    ttk.Label(curve_tab, text="Curve mode").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Combobox(
        curve_tab,
        textvariable=curve_mode_var,
        values=("closed", "open"),
        state="readonly",
        width=14,
    ).grid(row=row, column=1, sticky="w", pady=4)
    ttk.Label(curve_tab, text="closed returns to the first point").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(
        curve_tab,
        row,
        "Curve mode",
        "closed: the curve passes through all points in order and adds a final cubic segment back to the first point. This is best for loops/outlines.\n\n"
        "open: the curve starts at the first point and ends at the last point without returning to the start.",
    )
    row += 1

    ttk.Label(curve_tab, text="Curve method").grid(row=row, column=0, sticky="w", pady=4)
    ttk.Combobox(
        curve_tab,
        textvariable=method_var,
        values=("elastic", "catmull-rom"),
        state="readonly",
        width=14,
    ).grid(row=row, column=1, sticky="w", pady=4)
    ttk.Label(curve_tab, text="elastic is smoother at anchors").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(
        curve_tab,
        row,
        "Curve method",
        "elastic: default V3 method. It uses a cubic spline similar to an elastic rod. Open curves use natural end conditions; closed curves use periodic end conditions, so the start/end seam has matching tangent and curvature.\n\n"
        "catmull-rom: the older local interpolation method. It can be useful for tighter local control, but it is usually less curvature-smooth at anchor points.",
    )
    row += 1

    numeric_fields = [
        (
            "Smoothness",
            smoothness_var,
            "1 = true elastic spline",
            "Handle length multiplier. For the elastic method, keep this at 1.0 for the true natural/periodic cubic spline. Smaller values tighten the curve and preserve tangent direction, but no longer represent the exact minimum-bending spline.",
        ),
        (
            "Alpha",
            alpha_var,
            "0 uniform, 0.5 centripetal, 1 chordal",
            "Controls how distances between points are parameterized before the spline is solved. 0.5 is usually safest for unevenly spaced points because it reduces loops and cusps.",
        ),
        (
            "Simplify tolerance",
            simplify_var,
            "0 = exact through all points",
            "Optional approximate simplification in SVG units. Keep this at 0 if the curve must pass through every input point exactly.",
        ),
        (
            "Precision",
            precision_var,
            "decimal places",
            "Number of decimal places written to SVG coordinates and exported point text. 6 is safer for preserving handle alignment. Lower values make smaller SVG files but can introduce tiny rounding kinks in some editors.",
        ),
    ]
    for label, var, hint, help_text in numeric_fields:
        ttk.Label(curve_tab, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(curve_tab, textvariable=var, width=14).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(curve_tab, text=hint).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        add_help(curve_tab, row, label, help_text)
        row += 1

    ttk.Checkbutton(curve_tab, text="Use SVG closepath Z command for closed curves", variable=close_with_z_var).grid(
        row=row, column=1, sticky="w", pady=4
    )
    ttk.Label(curve_tab, text="recommended for closed strokes").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(
        curve_tab,
        row,
        "Close with Z",
        "When Curve mode is closed, this writes a formal SVG closepath Z command after the final cubic segment. V3 enables this by default so the stroke is joined at the start/end instead of drawing two overlapping end caps. It is ignored for open curves.",
    )
    row += 1

    ttk.Label(
        curve_tab,
        text=(
            "For the smoothest exact pass-through curve, use Method = elastic, Smoothness = 1.0, Simplify tolerance = 0. "
            "Non-adjacent repeated points are preserved for crossings."
        ),
        wraplength=800,
        foreground="#555555",
    ).grid(row=row, column=0, columnspan=4, sticky="ew", pady=(18, 0))

    row = 0
    style_fields = [
        ("Stroke color", stroke_var, "Example: #000000", "SVG stroke color for curves, points, and handle guides. Use values such as #000000, red, or rgb(0,0,0)."),
        ("Stroke width", stroke_width_var, "SVG units", "Width of the generated curve stroke."),
        ("Fill", fill_var, "use none for outline only", "SVG fill value for each path. Use none for an outline-only curve."),
        ("Padding", padding_var, "ignored when keeping SVG frame", "Extra viewBox padding around generated curves when not preserving the input SVG frame."),
        ("Point radius", point_radius_var, "only when points are shown", "Radius of optional anchor point markers in the output SVG."),
    ]
    for label, var, hint, help_text in style_fields:
        ttk.Label(style_tab, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(style_tab, textvariable=var, width=16).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(style_tab, text=hint).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
        add_help(style_tab, row, label, help_text)
        row += 1

    ttk.Checkbutton(style_tab, text="Show anchor points", variable=show_points_var).grid(
        row=row, column=1, sticky="w", pady=4
    )
    ttk.Label(style_tab, text="debug/overlay aid").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(style_tab, row, "Show anchor points", "Draws small circles at the curve anchor points so you can verify that the generated curve passes through the expected input points.")
    row += 1

    ttk.Checkbutton(style_tab, text="Show Bezier handles", variable=show_handles_var).grid(
        row=row, column=1, sticky="w", pady=4
    )
    ttk.Label(style_tab, text="debug/inspection aid").grid(row=row, column=2, sticky="w", padx=(8, 0), pady=4)
    add_help(style_tab, row, "Show Bezier handles", "Draws guide lines from each anchor point to its cubic Bezier handles. Useful for checking smoothness and editing the SVG later.")

    button_frame = ttk.Frame(main_frame)
    button_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
    button_frame.columnconfigure(0, weight=1)
    ttk.Label(button_frame, textvariable=status_var, foreground="#555555").grid(row=0, column=0, sticky="w")
    ttk.Button(button_frame, text="Generate SVG", command=generate).grid(row=0, column=1, padx=(8, 0))
    ttk.Button(button_frame, text="Close", command=root.destroy).grid(row=0, column=2, padx=(8, 0))

    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="point2curveV3.py",
        description=(
            "Create open or closed smooth SVG cubic Bezier curves from ordered 2D points "
            "or from circle centers in an SVG file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Text grouping examples:\n"
            "  [curve_A]\n  0,0\n  100,0\n  100,100\n\n"
            "  curve,x,y\n  curve_A,0,0\n  curve_A,100,0\n\n"
            "SVG grouping examples:\n"
            "  <g id=\"curve_A\"><circle cx=\"0\" cy=\"0\" r=\"2\"/>...</g>\n"
            "  <circle data-curve=\"curve_A\" cx=\"0\" cy=\"0\" r=\"2\"/>\n\n"
            "Manual regrouping examples with --group-mode split:\n"
            "  --group-spec 4,5,*\n"
            "  --group-spec 'outer:1-8; inner:9-16'"
        ),
    )
    parser.add_argument("input", nargs="?", help="Input .txt/.csv/.svg file.")
    parser.add_argument("--output", "-o", help="Output SVG file. Default: INPUT_curve.svg")
    parser.add_argument(
        "--input-format",
        choices=("auto", "text", "svg"),
        default="auto",
        help="Input parser to use. Default: auto.",
    )
    parser.add_argument(
        "--curve-mode",
        choices=("closed", "open"),
        default="closed",
        help="Whether each curve returns to its first point. Default: closed.",
    )
    parser.add_argument(
        "--open",
        dest="curve_mode",
        action="store_const",
        const="open",
        help="Shortcut for --curve-mode open.",
    )
    parser.add_argument(
        "--closed",
        dest="curve_mode",
        action="store_const",
        const="closed",
        help="Shortcut for --curve-mode closed.",
    )
    parser.add_argument(
        "--method",
        choices=("elastic", "catmull-rom"),
        default="elastic",
        help="Curve interpolation method. elastic is a natural/periodic cubic spline. Default: elastic.",
    )
    parser.add_argument(
        "--smoothness",
        type=float,
        default=1.0,
        help="Handle length multiplier. Use 1.0 for the true elastic spline. Default: 1.0.",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.5,
        help="Point-distance parameterization: 0 uniform, 0.5 centripetal, 1 chordal. Default: 0.5.",
    )
    parser.add_argument("--stroke", default="#000000", help="SVG stroke color. Default: #000000.")
    parser.add_argument("--stroke-width", type=float, default=2.0, help="SVG stroke width. Default: 2.")
    parser.add_argument("--fill", default="none", help="SVG path fill. Default: none.")
    parser.add_argument("--padding", type=float, default=10.0, help="ViewBox padding. Default: 10.")
    parser.add_argument(
        "--keep-svg-frame",
        action="store_true",
        help="For SVG input, preserve the root x/y/width/height/viewBox in the output SVG.",
    )
    parser.add_argument(
        "--export-points-txt",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Export parsed/grouped points as text. If PATH is omitted, INPUT_points.txt is used.",
    )
    parser.add_argument(
        "--group-mode",
        choices=("preserve", "all", "split"),
        default="preserve",
        help="How to divide points into curves. Default: preserve input grouping.",
    )
    parser.add_argument(
        "--group-spec",
        default="",
        help="Used with --group-mode split. Examples: '4,5,*' or 'outer:1-8; inner:9-16'.",
    )
    close_group = parser.add_mutually_exclusive_group()
    close_group.add_argument(
        "--close-with-z",
        dest="close_with_z",
        action="store_true",
        default=True,
        help="Append SVG closepath Z for closed curves. Default: on in V3.",
    )
    close_group.add_argument(
        "--no-close-with-z",
        dest="close_with_z",
        action="store_false",
        help="Do not append SVG closepath Z. The final cubic still returns to the start in closed mode.",
    )
    parser.add_argument("--show-points", action="store_true", help="Draw anchor points in the output SVG.")
    parser.add_argument("--show-handles", action="store_true", help="Draw Bezier handle guide lines.")
    parser.add_argument("--point-radius", type=float, default=2.0, help="Anchor point radius if shown.")
    parser.add_argument(
        "--simplify-tolerance",
        type=float,
        default=0.0,
        help=(
            "Approximate simplification tolerance in SVG units. "
            "0 keeps exact pass-through points. Default: 0."
        ),
    )
    parser.add_argument("--precision", type=int, default=6, help="Decimal places in output SVG. Default: 6.")
    parser.add_argument("--gui", action="store_true", help="Open GUI mode.")
    parser.add_argument("--verbose", action="store_true", help="Print extra diagnostic information.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or "--gui" in argv:
        return run_gui()

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if not args.input:
        parser.error("an input file is required unless --gui is used")

    try:
        export_points_path = None
        if args.export_points_txt is not None:
            export_points_path = args.export_points_txt or derive_points_output_name(args.input)

        output_path, warnings, curves = process_file(
            input_path=args.input,
            output_path=args.output,
            input_format=args.input_format,
            smoothness=args.smoothness,
            alpha=args.alpha,
            stroke=args.stroke,
            stroke_width=args.stroke_width,
            fill=args.fill,
            padding=args.padding,
            show_points=args.show_points,
            show_handles=args.show_handles,
            point_radius=args.point_radius,
            simplify_tolerance=args.simplify_tolerance,
            precision=args.precision,
            keep_svg_frame=args.keep_svg_frame,
            export_points_path=export_points_path,
            group_mode=args.group_mode,
            group_spec=args.group_spec,
            close_with_z=args.close_with_z,
            curve_mode=args.curve_mode,
            method=args.method,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1

    print(f"Wrote {output_path}")
    if args.export_points_txt is not None:
        print(f"Exported points to {export_points_path}")
    print(f"Created {len(curves)} curve(s): " + ", ".join(curve.name for curve in curves))
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
