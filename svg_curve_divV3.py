#!/usr/bin/env python3
# svg_curve_divV3.py
# Split one SVG <path> curve into editable SVG path fragments using design-length proportions.
# Inputs: an SVG file, a design total length, fragment lengths and/or number of fragments.
# Outputs: a new SVG file containing divided fragment paths, either aligned to the original curve or separated.
# Optional: color fragments with evenly distributed rainbow stroke colors.
# Example: python svg_curve_divV3.py input.svg --total 40 --lengths 11 18 --color-fragments --output input_div.svg

import argparse
import colorsys
import copy
import os
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple


FLOAT_RE = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")


def require_svgpathtools():
    """Import svgpathtools with a clear error message if it is missing."""
    try:
        from svgpathtools import parse_path  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "This script requires svgpathtools. Install it with:\n"
            "    python -m pip install svgpathtools\n"
        ) from exc
    return parse_path


def add_suffix_before_ext(path: str, suffix: str) -> str:
    base, ext = os.path.splitext(path)
    if not ext:
        ext = ".svg"
    return base + suffix + ext


def register_namespaces(svg_file: str) -> None:
    """Register namespaces so ElementTree does not rename SVG tags to ns0, ns1, etc."""
    try:
        for _event, ns in ET.iterparse(svg_file, events=("start-ns",)):
            prefix, uri = ns
            ET.register_namespace(prefix or "", uri)
    except ET.ParseError:
        # The main parse step will raise a more useful error.
        pass


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def same_namespace_tag(reference_tag: str, local: str) -> str:
    if reference_tag.startswith("{") and "}" in reference_tag:
        ns = reference_tag.split("}", 1)[0][1:]
        return "{%s}%s" % (ns, local)
    return local


def find_path_elements(root: ET.Element) -> List[ET.Element]:
    return [elem for elem in root.iter() if local_name(elem.tag) == "path" and elem.get("d")]


def parent_map(root: ET.Element):
    return {child: parent for parent in root.iter() for child in list(parent)}


def parse_length_values(values: Sequence[str]) -> List[float]:
    """Parse values such as ['11', '18'] or ['11,18'] into floats."""
    parsed: List[float] = []
    for value in values:
        for token in re.split(r"[,;\s]+", value.strip()):
            if not token:
                continue
            parsed.append(float(token))
    return parsed


def complete_fragment_lengths(
    total: Optional[float],
    given_lengths: Sequence[float],
    num_fragments: Optional[int],
) -> Tuple[float, List[float]]:
    """Return (design_total, complete_fragment_lengths).

    If only the first N-1 fragment lengths are supplied and a total is given, the final
    length is calculated automatically. If num_fragments is larger than the number of
    supplied values by more than one, the remaining length is divided equally among the
    missing fragments.
    """
    given = list(given_lengths)
    tol = 1e-9

    if any(x <= 0 for x in given):
        raise ValueError("Fragment lengths must be positive numbers.")

    if num_fragments is not None and num_fragments < 1:
        raise ValueError("--fragments must be at least 1.")

    if total is None:
        if not given:
            raise ValueError("Provide --total, or provide fragment lengths whose sum defines the total.")
        if num_fragments is not None and len(given) < num_fragments:
            raise ValueError("A total length is required when missing fragment lengths must be auto-calculated.")
        total = float(sum(given))

    total = float(total)
    if total <= 0:
        raise ValueError("Total length must be a positive number.")

    supplied_sum = float(sum(given))

    if num_fragments is None:
        if not given:
            raise ValueError("Provide --lengths, --fragments, or both.")
        if supplied_sum < total - tol:
            given.append(total - supplied_sum)
        elif supplied_sum > total + tol:
            raise ValueError(
                "The supplied fragment lengths sum to %.6g, which is larger than the total %.6g."
                % (supplied_sum, total)
            )
        # If supplied_sum is essentially equal to total, use the supplied fragments as-is.
    else:
        if len(given) > num_fragments:
            raise ValueError("More fragment lengths were supplied than --fragments allows.")
        missing = num_fragments - len(given)
        if missing == 0:
            if abs(supplied_sum - total) > max(tol, total * 1e-8):
                raise ValueError(
                    "When all fragment lengths are supplied, their sum must match --total. "
                    "Got sum %.6g and total %.6g." % (supplied_sum, total)
                )
        else:
            remaining = total - supplied_sum
            if remaining <= tol:
                raise ValueError("No positive length remains for the missing fragment(s).")
            fill_value = remaining / missing
            given.extend([fill_value] * missing)

    if not given:
        raise ValueError("No fragment lengths were defined.")
    if abs(sum(given) - total) > max(tol, total * 1e-8):
        raise ValueError("Internal length error: fragment lengths do not sum to total.")
    return total, given


def rounded_number_text(number_text: str, precision: int) -> str:
    value = float(number_text)
    text = ("%%.%df" % precision) % value
    text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def round_numbers_in_svg_path_data(d: str, precision: int) -> str:
    if precision < 0:
        return d
    return FLOAT_RE.sub(lambda m: rounded_number_text(m.group(0), precision), d)


def fmt_num(value: float, precision: int = 6) -> str:
    text = ("%%.%df" % precision) % value
    text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def transformed_attr(existing: Optional[str], extra: str) -> str:
    existing = (existing or "").strip()
    if existing:
        return existing + " " + extra
    return extra


def rainbow_hex_colors(count: int) -> List[str]:
    """Return evenly distributed, vivid rainbow colors as #RRGGBB strings."""
    if count < 1:
        return []
    colors = []
    for i in range(count):
        hue = float(i) / float(count)
        r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 0.95)
        colors.append("#%02X%02X%02X" % (int(round(r * 255)), int(round(g * 255)), int(round(b * 255))))
    return colors


def parse_style_attribute(style: str) -> Dict[str, str]:
    """Parse an SVG style attribute into a property dictionary."""
    result: Dict[str, str] = {}
    for part in style.split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


def serialize_style_attribute(style_dict: Dict[str, str]) -> str:
    return ";".join("%s:%s" % (key, value) for key, value in style_dict.items() if key)


def set_fragment_stroke_color(attrs: Dict[str, str], color: str) -> None:
    """Set the visible stroke color of a copied SVG path while preserving other style settings."""
    style_text = attrs.get("style", "")
    if style_text.strip():
        style_dict = parse_style_attribute(style_text)
        style_dict["stroke"] = color
        attrs["style"] = serialize_style_attribute(style_dict)
    else:
        attrs["stroke"] = color


def crop_path_by_design_lengths(path_obj, design_total: float, fragment_lengths: Sequence[float]):
    """Crop an svgpathtools Path into fragments based on design-length proportions."""
    actual_length = path_obj.length()
    if actual_length <= 0:
        raise ValueError("The selected SVG path has zero length.")

    cut_parameters = [0.0]
    running_design_length = 0.0
    for frag_len in fragment_lengths[:-1]:
        running_design_length += frag_len
        actual_cut_length = (running_design_length / design_total) * actual_length
        actual_cut_length = min(max(actual_cut_length, 0.0), actual_length)
        cut_parameters.append(path_obj.ilength(actual_cut_length))
    cut_parameters.append(1.0)

    fragments = []
    for start_t, end_t in zip(cut_parameters[:-1], cut_parameters[1:]):
        if end_t < start_t:
            raise ValueError("Computed cut parameters are not increasing. Check the fragment lengths.")
        fragments.append(path_obj.cropped(start_t, end_t))
    return actual_length, fragments


def get_path_records(svg_file: str) -> List[Dict[str, object]]:
    """Return path index, id, segment count, and actual SVG length for all valid paths."""
    parse_path = require_svgpathtools()
    register_namespaces(svg_file)
    tree = ET.parse(svg_file)
    root = tree.getroot()
    paths = find_path_elements(root)
    records: List[Dict[str, object]] = []
    for i, elem in enumerate(paths):
        elem_id = elem.get("id", "")
        record: Dict[str, object] = {
            "index": i,
            "id": elem_id,
            "segments": None,
            "length": None,
            "error": None,
        }
        try:
            path_obj = parse_path(elem.get("d", ""))
            record["segments"] = len(path_obj)
            record["length"] = path_obj.length()
        except Exception as exc:
            record["error"] = str(exc)
        records.append(record)
    return records


def summarize_paths(svg_file: str) -> str:
    records = get_path_records(svg_file)
    if not records:
        return "No <path> elements with a d attribute were found."

    lines = []
    for record in records:
        elem_id = str(record.get("id") or "")
        label = "id=%s" % elem_id if elem_id else "no id"
        if record.get("error"):
            lines.append("[%d] %s | could not parse path: %s" % (record["index"], label, record["error"]))
        else:
            lines.append(
                "[%d] %s | segments=%s | SVG length=%.6f"
                % (record["index"], label, record["segments"], record["length"])
            )
    return "\n".join(lines)


def selected_path_info(svg_file: str, path_index: int) -> Dict[str, object]:
    records = get_path_records(svg_file)
    if not records:
        raise ValueError("No <path> elements with a d attribute were found.")
    if path_index < 0 or path_index >= len(records):
        raise IndexError("Path index %d is out of range. Found %d path(s)." % (path_index, len(records)))
    record = records[path_index]
    if record.get("error"):
        raise ValueError("Selected path could not be parsed: %s" % record["error"])
    record["path_count"] = len(records)
    return record


def divide_svg_curve(
    input_svg: str,
    output_svg: Optional[str],
    total: Optional[float],
    lengths: Sequence[float],
    num_fragments: Optional[int] = None,
    path_index: int = 0,
    align_to_original: bool = True,
    keep_original: bool = False,
    separate_direction: str = "y",
    separate_spacing: float = 25.0,
    precision: int = 6,
    color_fragments: bool = False,
) -> Tuple[str, List[float], float]:
    """Split one SVG path into multiple path elements and write a new SVG."""
    parse_path = require_svgpathtools()

    if not os.path.isfile(input_svg):
        raise FileNotFoundError("Input SVG not found: %s" % input_svg)
    if output_svg is None:
        output_svg = add_suffix_before_ext(input_svg, "_div")

    design_total, completed_lengths = complete_fragment_lengths(total, lengths, num_fragments)

    register_namespaces(input_svg)
    tree = ET.parse(input_svg)
    root = tree.getroot()
    paths = find_path_elements(root)
    if not paths:
        raise ValueError("No <path> elements with a d attribute were found in the SVG.")
    if path_index < 0 or path_index >= len(paths):
        raise IndexError("--path-index %d is out of range. Found %d path(s)." % (path_index, len(paths)))

    target = paths[path_index]
    original_d = target.get("d", "")
    path_obj = parse_path(original_d)
    actual_length, path_fragments = crop_path_by_design_lengths(path_obj, design_total, completed_lengths)
    fragment_colors = rainbow_hex_colors(len(path_fragments)) if color_fragments else []

    base_id = target.get("id") or ("path_%d" % path_index)
    group_tag = same_namespace_tag(root.tag, "g")
    group = ET.Element(group_tag, {"id": base_id + "_divided"})

    original_transform = target.get("transform")
    path_tag = target.tag

    for idx, frag in enumerate(path_fragments, start=1):
        new_attrs = copy.deepcopy(target.attrib)
        new_attrs["id"] = "%s_frag_%02d" % (base_id, idx)
        new_attrs["d"] = round_numbers_in_svg_path_data(frag.d(), precision)

        if align_to_original:
            # Keep the original local coordinates and the original transform.
            if original_transform is not None:
                new_attrs["transform"] = original_transform
            elif "transform" in new_attrs:
                del new_attrs["transform"]
        else:
            # Preserve the original coordinates, then offset each fragment for visual inspection.
            if separate_direction.lower() == "x":
                dx, dy = (idx - 1) * separate_spacing, 0.0
            else:
                dx, dy = 0.0, (idx - 1) * separate_spacing
            extra = "translate(%s %s)" % (fmt_num(dx), fmt_num(dy))
            new_attrs["transform"] = transformed_attr(original_transform, extra)

        if color_fragments:
            set_fragment_stroke_color(new_attrs, fragment_colors[idx - 1])

        group.append(ET.Element(path_tag, new_attrs))

    pmap = parent_map(root)
    parent = pmap.get(target)
    if parent is None:
        raise ValueError("Could not find parent element for selected path.")

    children = list(parent)
    insert_at = children.index(target)
    if keep_original:
        parent.insert(insert_at + 1, group)
    else:
        parent.remove(target)
        parent.insert(insert_at, group)

    try:
        ET.indent(tree, space="  ")  # Python 3.9+
    except AttributeError:
        pass

    tree.write(output_svg, encoding="utf-8", xml_declaration=True)
    return output_svg, completed_lengths, actual_length


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("SVG curve divider V3")
    root.geometry("980x700")

    input_var = tk.StringVar()
    output_var = tk.StringVar()
    path_index_var = tk.StringVar(value="0")
    selected_path_info_var = tk.StringVar(value="Selected path: choose an SVG file to show path id and length.")
    total_var = tk.StringVar(value="40")
    lengths_var = tk.StringVar(value="11, 18")
    fragments_var = tk.StringVar(value="")
    fragment_info_var = tk.StringVar(value="Fragments: enter total/lengths/fragments to preview the division.")
    align_var = tk.BooleanVar(value=True)
    keep_original_var = tk.BooleanVar(value=False)
    color_fragments_var = tk.BooleanVar(value=False)
    direction_var = tk.StringVar(value="y")
    spacing_var = tk.StringVar(value="25")
    precision_var = tk.StringVar(value="6")
    status_var = tk.StringVar(value="Choose an SVG file, set the total/design length and fragment lengths, then click Create output SVG.")

    path_update_job = None
    fragment_update_job = None

    help_text = {
        "Input SVG": (
            "Choose the SVG file that contains the curve path you want to divide.\n\n"
            "Only <path> elements with a d attribute are used. If your object is a shape, line, or text in Illustrator, first convert it to a path."
        ),
        "Output SVG": (
            "Choose where to save the divided SVG.\n\n"
            "If this is left blank, the command-line mode uses the input filename with _div before .svg. "
            "In the GUI, the output path is filled automatically after choosing the input file."
        ),
        "Path index": (
            "Select which SVG <path> element should be divided. The first path is index 0.\n\n"
            "After you type an index, the GUI automatically shows the selected path id, segment count, and actual SVG length.\n\n"
            "Tip: click 'List paths' to see all path indices."
        ),
        "List paths": (
            "Show all path indices found in the SVG, together with their id, segment count, and actual SVG length.\n\n"
            "Use this when an SVG contains multiple paths and you need to identify the correct one."
        ),
        "Total/design length": (
            "This is the reference length used for proportional division, not necessarily the measured SVG path length.\n\n"
            "Example: if total is 40 and fragment lengths are 11 and 18, the final fragment is automatically calculated as 11. "
            "The script maps these values proportionally onto the actual SVG path length."
        ),
        "Fragment lengths": (
            "Enter known fragment lengths separated by commas, spaces, or semicolons.\n\n"
            "Example: 11, 18 with total 40 gives fragments 11, 18, 11.\n\n"
            "If Number of fragments is blank, the script auto-adds one final missing fragment when the supplied sum is smaller than total."
        ),
        "Number of fragments": (
            "Optional total number of fragments.\n\n"
            "Example: total 40, lengths 11, 18, number 3 gives 11, 18, 11.\n"
            "Example: total 40, lengths 10, number 4 gives 10, 10, 10, 10 because the remaining 30 is split equally among 3 missing fragments."
        ),
        "Align fragments": (
            "When checked, the divided fragments remain exactly aligned to the original curve.\n\n"
            "This is usually best for Illustrator editing because the fragments replace or overlay the original curve at the same position.\n\n"
            "When unchecked, fragments are offset from each other for visual inspection."
        ),
        "Keep original": (
            "When checked, the original path is kept as a reference and the divided fragments are inserted after it.\n\n"
            "When unchecked, the original selected path is replaced by a group containing the divided fragments."
        ),
        "Color fragments": (
            "When checked, each divided fragment receives a different stroke color.\n\n"
            "The default colors are vivid rainbow colors evenly distributed through the hue spectrum. "
            "This is useful for checking whether the division worked or for preparing Illustrator artwork.\n\n"
            "The script changes the fragment stroke color but preserves other path attributes as much as possible."
        ),
        "Separate direction": (
            "Used only when 'Align fragments to original curve' is unchecked.\n\n"
            "Choose x to place fragments side-by-side horizontally, or y to place them vertically."
        ),
        "Separate spacing": (
            "Used only when fragments are not aligned to the original curve.\n\n"
            "This controls the offset distance between adjacent fragments in SVG units."
        ),
        "Numeric precision": (
            "Number of decimal places used in the output path coordinates.\n\n"
            "Default 6 is usually enough. Use a larger value if you need maximum geometric fidelity. Use -1 to avoid rounding."
        ),
        "Create output SVG": (
            "Create the output SVG using the current settings.\n\n"
            "After creation, the status box reports the output file, completed fragment lengths, and actual SVG path length."
        ),
    }

    def show_help(title: str) -> None:
        messagebox.showinfo(title, help_text.get(title, "No help text is available for this option."))

    def help_button(parent, title: str):
        # tk.Button allows background color more reliably than ttk.Button on many systems.
        return tk.Button(
            parent,
            text="?",
            width=2,
            command=lambda: show_help(title),
            bg="#cfeeff",
            activebackground="#aee2ff",
            relief="raised",
        )

    def choose_input() -> None:
        path = filedialog.askopenfilename(
            title="Choose input SVG",
            filetypes=[("SVG files", "*.svg"), ("All files", "*.*")],
        )
        if path:
            input_var.set(path)
            if not output_var.get().strip():
                output_var.set(add_suffix_before_ext(path, "_div"))
            schedule_path_update()
            schedule_fragment_update()

    def choose_output() -> None:
        initial = output_var.get().strip() or add_suffix_before_ext(input_var.get().strip(), "_div")
        path = filedialog.asksaveasfilename(
            title="Save output SVG as",
            initialfile=os.path.basename(initial) if initial else "output_div.svg",
            initialdir=os.path.dirname(initial) if initial else None,
            defaultextension=".svg",
            filetypes=[("SVG files", "*.svg"), ("All files", "*.*")],
        )
        if path:
            output_var.set(path)

    def show_paths() -> None:
        svg = input_var.get().strip()
        if not svg:
            messagebox.showwarning("Missing input", "Please choose an input SVG first.")
            return
        try:
            info = summarize_paths(svg)
            messagebox.showinfo("Path list", info)
        except Exception as exc:
            messagebox.showerror("Could not list paths", str(exc))

    def current_selected_actual_length() -> Optional[float]:
        svg = input_var.get().strip()
        if not svg or not os.path.isfile(svg):
            return None
        try:
            path_index = int(path_index_var.get().strip())
            info = selected_path_info(svg, path_index)
            value = info.get("length")
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def update_selected_path_info() -> None:
        svg = input_var.get().strip()
        if not svg:
            selected_path_info_var.set("Selected path: choose an SVG file to show path id and length.")
            schedule_fragment_update()
            return
        if not os.path.isfile(svg):
            selected_path_info_var.set("Selected path: input SVG file does not exist.")
            schedule_fragment_update()
            return
        try:
            path_index = int(path_index_var.get().strip())
            info = selected_path_info(svg, path_index)
            path_id = str(info.get("id") or "(no id)")
            selected_path_info_var.set(
                "Selected path: index %d of %d | id: %s | segments: %s | actual SVG length: %.6f"
                % (
                    int(info["index"]),
                    int(info["path_count"]),
                    path_id,
                    info.get("segments"),
                    float(info["length"]),
                )
            )
        except Exception as exc:
            selected_path_info_var.set("Selected path: %s" % exc)
        schedule_fragment_update()

    def build_fragment_preview_text() -> str:
        total_text = total_var.get().strip()
        lengths_text = lengths_var.get().strip()
        fragments_text = fragments_var.get().strip()

        if not (total_text or lengths_text or fragments_text):
            return "Fragments: enter total/lengths/fragments to preview the division."

        total = float(total_text) if total_text else None
        lengths = parse_length_values([lengths_text]) if lengths_text else []
        num_fragments = int(fragments_text) if fragments_text else None
        design_total, completed_lengths = complete_fragment_lengths(total, lengths, num_fragments)

        percentages = [(x / design_total) * 100.0 for x in completed_lengths]
        text_lines = [
            "Fragments preview:",
            "  design total = %s" % fmt_num(design_total),
            "  fragment design lengths = %s" % ", ".join(fmt_num(x) for x in completed_lengths),
            "  proportions = %s" % ", ".join(fmt_num(x, 3) + "%" for x in percentages),
        ]
        if color_fragments_var.get():
            colors = rainbow_hex_colors(len(completed_lengths))
            text_lines.append("  rainbow stroke colors = %s" % ", ".join(colors))

        actual_length = current_selected_actual_length()
        if actual_length is not None:
            actual_fragment_lengths = [(x / design_total) * actual_length for x in completed_lengths]
            cumulative = []
            running = 0.0
            for value in actual_fragment_lengths[:-1]:
                running += value
                cumulative.append(running)
            text_lines.extend(
                [
                    "  selected SVG path length = %.6f" % actual_length,
                    "  approximate SVG fragment lengths = %s" % ", ".join(fmt_num(x) for x in actual_fragment_lengths),
                    "  approximate cut distances from path start = %s"
                    % (", ".join(fmt_num(x) for x in cumulative) if cumulative else "none"),
                ]
            )
        else:
            text_lines.append("  choose a valid SVG/path index to also preview actual SVG fragment lengths.")
        return "\n".join(text_lines)

    def update_fragment_info() -> None:
        try:
            fragment_info_var.set(build_fragment_preview_text())
        except Exception as exc:
            fragment_info_var.set("Fragments preview: %s" % exc)

    def schedule_path_update(*_args) -> None:
        nonlocal path_update_job
        if path_update_job is not None:
            root.after_cancel(path_update_job)
        path_update_job = root.after(250, update_selected_path_info)

    def schedule_fragment_update(*_args) -> None:
        nonlocal fragment_update_job
        if fragment_update_job is not None:
            root.after_cancel(fragment_update_job)
        fragment_update_job = root.after(250, update_fragment_info)

    def create_output() -> None:
        try:
            svg = input_var.get().strip()
            out = output_var.get().strip() or None
            if not svg:
                raise ValueError("Please choose an input SVG file.")

            total_text = total_var.get().strip()
            total = float(total_text) if total_text else None
            lengths = parse_length_values([lengths_var.get()])
            fragments_text = fragments_var.get().strip()
            num_fragments = int(fragments_text) if fragments_text else None
            path_index = int(path_index_var.get().strip())
            spacing = float(spacing_var.get().strip())
            precision = int(precision_var.get().strip())

            output_svg, completed_lengths, actual_length = divide_svg_curve(
                input_svg=svg,
                output_svg=out,
                total=total,
                lengths=lengths,
                num_fragments=num_fragments,
                path_index=path_index,
                align_to_original=align_var.get(),
                keep_original=keep_original_var.get(),
                separate_direction=direction_var.get(),
                separate_spacing=spacing,
                precision=precision,
                color_fragments=color_fragments_var.get(),
            )
            status = (
                "Created: %s\nFragments: %s\nActual SVG path length: %.6f"
                % (output_svg, ", ".join(fmt_num(x) for x in completed_lengths), actual_length)
            )
            status_var.set(status)
            messagebox.showinfo("Done", status)
        except Exception as exc:
            status_var.set("Error: %s" % exc)
            messagebox.showerror("Error", "%s\n\n%s" % (exc, traceback.format_exc()))

    # Automatic live updates for the two requested GUI information fields.
    for var in (input_var, path_index_var):
        var.trace_add("write", schedule_path_update)
    for var in (total_var, lengths_var, fragments_var, path_index_var, input_var, color_fragments_var):
        var.trace_add("write", schedule_fragment_update)

    pad = {"padx": 8, "pady": 5}
    main = ttk.Frame(root, padding=10)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Input SVG").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(main, textvariable=input_var, width=78).grid(row=0, column=1, sticky="we", **pad)
    ttk.Button(main, text="Browse", command=choose_input).grid(row=0, column=2, sticky="we", **pad)
    help_button(main, "Input SVG").grid(row=0, column=3, sticky="w", **pad)

    ttk.Label(main, text="Output SVG").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(main, textvariable=output_var, width=78).grid(row=1, column=1, sticky="we", **pad)
    ttk.Button(main, text="Browse", command=choose_output).grid(row=1, column=2, sticky="we", **pad)
    help_button(main, "Output SVG").grid(row=1, column=3, sticky="w", **pad)

    ttk.Label(main, text="Path index").grid(row=2, column=0, sticky="w", **pad)
    path_frame = ttk.Frame(main)
    path_frame.grid(row=2, column=1, sticky="w", **pad)
    ttk.Entry(path_frame, textvariable=path_index_var, width=10).pack(side="left")
    ttk.Button(path_frame, text="List paths", command=show_paths).pack(side="left", padx=8)
    help_button(path_frame, "List paths").pack(side="left", padx=2)
    help_button(main, "Path index").grid(row=2, column=3, sticky="w", **pad)

    selected_path_label = ttk.Label(main, textvariable=selected_path_info_var, wraplength=820, foreground="#004080")
    selected_path_label.grid(row=3, column=0, columnspan=4, sticky="we", padx=8, pady=(0, 8))

    ttk.Label(main, text="Total/design length").grid(row=4, column=0, sticky="w", **pad)
    ttk.Entry(main, textvariable=total_var, width=20).grid(row=4, column=1, sticky="w", **pad)
    help_button(main, "Total/design length").grid(row=4, column=3, sticky="w", **pad)

    ttk.Label(main, text="Fragment lengths").grid(row=5, column=0, sticky="w", **pad)
    ttk.Entry(main, textvariable=lengths_var, width=45).grid(row=5, column=1, sticky="w", **pad)
    ttk.Label(main, text="Example: 11, 18; the last length is auto-calculated from total.").grid(row=5, column=2, sticky="w", **pad)
    help_button(main, "Fragment lengths").grid(row=5, column=3, sticky="w", **pad)

    ttk.Label(main, text="Number of fragments").grid(row=6, column=0, sticky="w", **pad)
    ttk.Entry(main, textvariable=fragments_var, width=20).grid(row=6, column=1, sticky="w", **pad)
    ttk.Label(main, text="Optional. Use 3 with 11, 18 to force one auto final fragment.").grid(row=6, column=2, sticky="w", **pad)
    help_button(main, "Number of fragments").grid(row=6, column=3, sticky="w", **pad)

    preview_frame = ttk.LabelFrame(main, text="Live fragment preview")
    preview_frame.grid(row=7, column=0, columnspan=4, sticky="we", padx=8, pady=8)
    ttk.Label(preview_frame, textvariable=fragment_info_var, wraplength=880, justify="left").pack(fill="x", padx=8, pady=8)

    align_frame = ttk.Frame(main)
    align_frame.grid(row=8, column=1, sticky="w", **pad)
    ttk.Checkbutton(align_frame, text="Align fragments to original curve", variable=align_var).pack(side="left")
    help_button(align_frame, "Align fragments").pack(side="left", padx=6)

    keep_frame = ttk.Frame(main)
    keep_frame.grid(row=9, column=1, sticky="w", **pad)
    ttk.Checkbutton(keep_frame, text="Keep original path as reference", variable=keep_original_var).pack(side="left")
    help_button(keep_frame, "Keep original").pack(side="left", padx=6)

    color_frame = ttk.Frame(main)
    color_frame.grid(row=10, column=1, sticky="w", **pad)
    ttk.Checkbutton(color_frame, text="Color fragments with rainbow stroke colors", variable=color_fragments_var).pack(side="left")
    help_button(color_frame, "Color fragments").pack(side="left", padx=6)

    separate_frame = ttk.Frame(main)
    separate_frame.grid(row=11, column=1, sticky="w", **pad)
    ttk.Label(separate_frame, text="If not aligned: direction").pack(side="left")
    ttk.Combobox(separate_frame, textvariable=direction_var, values=["x", "y"], width=5, state="readonly").pack(side="left", padx=6)
    help_button(separate_frame, "Separate direction").pack(side="left", padx=2)
    ttk.Label(separate_frame, text="spacing").pack(side="left", padx=(12, 0))
    ttk.Entry(separate_frame, textvariable=spacing_var, width=8).pack(side="left", padx=6)
    help_button(separate_frame, "Separate spacing").pack(side="left", padx=2)

    ttk.Label(main, text="Numeric precision").grid(row=12, column=0, sticky="w", **pad)
    ttk.Entry(main, textvariable=precision_var, width=20).grid(row=12, column=1, sticky="w", **pad)
    help_button(main, "Numeric precision").grid(row=12, column=3, sticky="w", **pad)

    create_frame = ttk.Frame(main)
    create_frame.grid(row=13, column=1, sticky="w", **pad)
    ttk.Button(create_frame, text="Create output SVG", command=create_output).pack(side="left")
    help_button(create_frame, "Create output SVG").pack(side="left", padx=6)

    ttk.Label(main, textvariable=status_var, wraplength=880).grid(row=14, column=0, columnspan=4, sticky="we", **pad)

    main.columnconfigure(1, weight=1)
    main.columnconfigure(2, weight=0)

    # Initialize live preview text.
    update_selected_path_info()
    update_fragment_info()

    root.mainloop()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Split one SVG <path> curve into editable fragments. By default, the fragments "
            "are aligned to the original curve. With no arguments, or with --gui, a Tkinter GUI opens. "
            "Use --color-fragments to give each fragment a different rainbow stroke color."
        )
    )
    parser.add_argument("input_svg", nargs="?", help="Input SVG file.")
    parser.add_argument("-o", "--output", help="Output SVG file. Default: input filename with _div before .svg.")
    parser.add_argument("--total", type=float, help="Design total length used to interpret fragment lengths, e.g. 40.")
    parser.add_argument(
        "--lengths",
        nargs="*",
        default=[],
        help=(
            "Fragment lengths. Use spaces or commas, e.g. --lengths 11 18 or --lengths 11,18. "
            "Missing final length is auto-calculated from --total."
        ),
    )
    parser.add_argument(
        "--fragments",
        type=int,
        help="Optional total number of fragments. Missing fragment length(s) are calculated from --total.",
    )
    parser.add_argument("--path-index", type=int, default=0, help="Index of the SVG path to split. Default: 0.")
    parser.add_argument("--list-paths", action="store_true", help="List path indices and actual SVG lengths, then exit.")
    parser.add_argument("--gui", action="store_true", help="Open the GUI.")
    parser.add_argument(
        "--separate",
        action="store_true",
        help="Do not align fragments to the original curve. Instead, offset them for inspection.",
    )
    parser.add_argument(
        "--separate-direction",
        choices=["x", "y"],
        default="y",
        help="Direction for separated fragments. Default: y.",
    )
    parser.add_argument(
        "--separate-spacing",
        type=float,
        default=25.0,
        help="Spacing between separated fragments. Default: 25 SVG units.",
    )
    parser.add_argument(
        "--keep-original",
        action="store_true",
        help="Keep the original path and insert the divided fragments after it.",
    )
    parser.add_argument(
        "--color-fragments",
        action="store_true",
        help="Color divided fragments with evenly distributed rainbow stroke colors.",
    )
    parser.add_argument(
        "--precision",
        type=int,
        default=6,
        help="Decimal precision for output path coordinates. Use -1 to avoid rounding. Default: 6.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        launch_gui()
        return 0

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.gui:
        launch_gui()
        return 0

    if not args.input_svg:
        parser.error("input_svg is required unless --gui is used.")

    try:
        if args.list_paths:
            print(summarize_paths(args.input_svg))
            return 0

        lengths = parse_length_values(args.lengths)
        output_svg, completed_lengths, actual_length = divide_svg_curve(
            input_svg=args.input_svg,
            output_svg=args.output,
            total=args.total,
            lengths=lengths,
            num_fragments=args.fragments,
            path_index=args.path_index,
            align_to_original=not args.separate,
            keep_original=args.keep_original,
            separate_direction=args.separate_direction,
            separate_spacing=args.separate_spacing,
            precision=args.precision,
            color_fragments=args.color_fragments,
        )
        print("Wrote: %s" % output_svg)
        print("Fragment design lengths: %s" % ", ".join(fmt_num(x) for x in completed_lengths))
        print("Actual selected SVG path length: %.6f" % actual_length)
        if not args.separate:
            print("Placement: fragments aligned to the original curve.")
        else:
            print("Placement: fragments separated for inspection.")
        if args.color_fragments:
            print("Coloring: rainbow stroke colors applied to fragments.")
        return 0
    except Exception as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
