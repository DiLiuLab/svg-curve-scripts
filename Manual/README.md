# SVG Curve Scripts Manual

This manual shows practical workflows for the scripts in this repository using the files in the `Example` folder.

Run commands from the repository root:

```bash
cd /path/to/svg-curve-scripts
```

## Example Files

| File | What it demonstrates |
| --- | --- |
| `Example/HL_circle.svg` | Two named circle groups, `ring2` and `ring1`, suitable for converting into two closed curves and then generating crossing-gap options. |
| `Example/TK1B_circle.svg` | One circle-anchor group, `Layer_2`, suitable for converting a longer ordered anchor sequence into one smooth closed curve. |

Both examples were exported from Adobe Illustrator and contain circles that act as anchor points. `svg_point2curveV3.py` reads the circle centers in SVG document order.

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Tkinter is needed only for GUI mode. If GUI mode is unavailable in your Python installation, use the command-line examples below.

## Workflow 1: Convert Circle Anchors to Smooth Curves

Use `svg_point2curveV3.py` when an SVG contains circles marking the intended curve anchors.

Convert `HL_circle.svg` into two smooth closed curves:

```bash
python svg_point2curveV3.py Example/HL_circle.svg \
  --output Example/HL_curve.svg \
  --keep-svg-frame \
  --export-points-txt Example/HL_points.txt
```

Expected result:

- `Example/HL_curve.svg` contains two curve paths named `ring2` and `ring1`.
- `Example/HL_points.txt` records the parsed anchor coordinates.

The parsed `HL_circle.svg` point groups are:

```text
[ring2]
250.95, 248.6
109.53, 390.02
250.95, 531.44
392.37, 390.02

[ring1]
392.37, 248.6
250.95, 390.02
392.37, 531.44
533.79, 390.02
```

Convert `TK1B_circle.svg` into one smooth closed curve:

```bash
python svg_point2curveV3.py Example/TK1B_circle.svg \
  --output Example/TK1B_curve.svg \
  --keep-svg-frame \
  --export-points-txt Example/TK1B_points.txt
```

Expected result:

- `Example/TK1B_curve.svg` contains one curve path named `Layer_2`.
- `Example/TK1B_points.txt` records the anchor coordinates.

Useful options:

```bash
python svg_point2curveV3.py Example/HL_circle.svg --open --output Example/HL_open_curve.svg
python svg_point2curveV3.py Example/HL_circle.svg --show-points --show-handles --output Example/HL_debug_curve.svg
python svg_point2curveV3.py Example/HL_circle.svg --method catmull-rom --output Example/HL_catmull_rom.svg
```

Use `--open` when the curve should not return to its first point. The default is a closed curve.

## Workflow 2: Create Editable Crossing Gaps

Use `svg_crossing_gap_optionsV3.py` after converting circle anchors into curve paths.

First create the curve SVG:

```bash
python svg_point2curveV3.py Example/HL_circle.svg \
  --output Example/HL_curve.svg \
  --keep-svg-frame
```

Then create crossing-gap options:

```bash
python svg_crossing_gap_optionsV3.py Example/HL_curve.svg \
  --output Example/HL_crossing_gap.svg \
  --gap-radius-px 12 \
  --default-choice both
```

Expected result for `HL_curve.svg`:

- Input curves: 2
- Detected crossings: 2
- Output file: `Example/HL_crossing_gap.svg`

The output SVG contains editable groups:

- `original_graph_reference`: original continuous curves, hidden by default unless changed.
- `base_broken_strands`: curves with real gaps cut at crossings.
- `crossing_options`: candidate overpass pieces for each crossing.
- `crossing_labels`: optional crossing numbers.
- `direction_guides`: optional short curves for later arrowheads.

Add direction guides for Illustrator arrowheads:

```bash
python svg_crossing_gap_optionsV3.py Example/HL_curve.svg \
  --output Example/HL_crossing_gap_guides.svg \
  --gap-radius-px 12 \
  --add-direction-guides
```

Tips:

- Decrease `--sample-step-px` if a true crossing is missed.
- Increase `--gap-radius-px` if the visual gap is too small.
- Use `--default-choice a`, `--default-choice b`, or `--default-choice none` if you do not want both choices visible at first.

## Workflow 3: Divide a Curve into Fragments

Use `svg_curve_divV2.py` when you need editable curve fragments based on design lengths.

Create a curve SVG first:

```bash
python svg_point2curveV3.py Example/HL_circle.svg \
  --output Example/HL_curve.svg \
  --keep-svg-frame
```

List the available paths:

```bash
python svg_curve_divV2.py Example/HL_curve.svg --list-paths
```

Expected path list for `HL_curve.svg`:

```text
[0] id=ring2 | segments=4 | SVG length=876.163643
[1] id=ring1 | segments=4 | SVG length=876.163643
```

Divide the first path into design-length fragments:

```bash
python svg_curve_divV2.py Example/HL_curve.svg \
  --path-index 0 \
  --total 40 \
  --lengths 11 18 \
  --output Example/HL_ring2_div.svg
```

In this example, the final fragment length is calculated automatically as `40 - 11 - 18 = 11`.

Use separated output for easier inspection:

```bash
python svg_curve_divV2.py Example/HL_curve.svg \
  --path-index 0 \
  --total 40 \
  --lengths 11 18 \
  --separate \
  --output Example/HL_ring2_div_separate.svg
```

## GUI Mode

Each script can open a GUI:

```bash
python svg_point2curveV3.py --gui
python svg_curve_divV2.py --gui
python svg_crossing_gap_optionsV3.py --gui
```

You can also run a script with no arguments to open its GUI.

## Recommended File Practice

Keep original example inputs in `Example/` and write generated outputs with clear suffixes:

- `_curve.svg` for point-to-curve output.
- `_points.txt` for exported point coordinates.
- `_crossing_gap.svg` for editable crossing-gap output.
- `_div.svg` for divided curve output.

For long-term versioning, prefer Git commits and release tags instead of adding new version numbers to filenames.
