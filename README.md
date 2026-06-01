# SVG Curve Scripts

Small Python utilities for preparing and editing SVG curve geometry. The scripts can be used from the command line or, when available, through their built-in Tkinter GUIs.

## Scripts

| Script | Purpose | Main inputs | Main outputs |
| --- | --- | --- | --- |
| `svg_point2curveV3.py` | Converts ordered 2D points, or SVG circle/ellipse centers, into smooth cubic Bezier SVG paths. | `.txt`, `.csv`, `.svg` | Smooth curve `.svg`, optional exported points `.txt` |
| `svg_curve_divV2.py` | Splits one SVG path into editable fragments based on design-length proportions. | `.svg` path | Divided fragment `.svg` |
| `svg_crossing_gap_optionsV3.py` | Detects curve crossings and creates editable gap/overpass options for knot or link diagrams. | `.svg` paths | Crossing-gap option `.svg` |

## Requirements

- Python 3.9 or newer is recommended.
- Tkinter is needed only for GUI mode. It is included with many Python installations, but not all.
- `svgpathtools` is required by `svg_curve_divV2.py`.

Install the Python dependency:

```bash
python3 -m pip install -r requirements.txt
```

This installs `svgpathtools`, which is needed by `svg_curve_divV2.py`. The other scripts use only Python's standard library. If you prefer keeping project dependencies isolated, you may use a virtual environment, but it is not required for normal use.

## Quick Start

Show each script's available options:

```bash
python svg_point2curveV3.py --help
python svg_curve_divV2.py --help
python svg_crossing_gap_optionsV3.py --help
```

Run a GUI:

```bash
python svg_point2curveV3.py --gui
python svg_curve_divV2.py --gui
python svg_crossing_gap_optionsV3.py --gui
```

For step-by-step workflows using the included example SVG files, see [Manual/README.md](Manual/README.md).

## Command-Line Examples

Create a closed smooth curve from point data:

```bash
python svg_point2curveV3.py points.txt --output points_curve.svg
```

Create an open curve:

```bash
python svg_point2curveV3.py points.txt --curve-mode open --output open_curve.svg
```

Convert SVG circles or ellipses into a smooth path while preserving the input SVG frame:

```bash
python svg_point2curveV3.py input.svg --output curves.svg --keep-svg-frame --export-points-txt
```

List paths in an SVG before choosing one to divide:

```bash
python svg_curve_divV2.py input.svg --list-paths
```

Divide a selected path using a design total of 40 and fragment lengths 11, 18, and the automatically calculated remainder:

```bash
python svg_curve_divV2.py input.svg --total 40 --lengths 11 18 --output input_div.svg
```

Create editable crossing-gap options:

```bash
python svg_crossing_gap_optionsV3.py input.svg --output input_crossing_gap.svg --gap-radius-px 12
```

Add direction-guide curves for later arrowheads in Illustrator:

```bash
python svg_crossing_gap_optionsV3.py input.svg --add-direction-guides
```

## Input Notes

`svg_point2curveV3.py` accepts simple point lists:

```text
0, 0
100, 0
100, 100
0, 100
```

It also accepts named groups:

```text
[outer]
0, 0
100, 0
100, 100
0, 100

[inner]
25, 25
75, 25
75, 75
25, 75
```

For SVG input, circles inside a group such as `<g id="curve_A">...</g>` become one curve group. Circles may also use `data-curve="curve_A"` or `data-group="curve_A"`.

`svg_crossing_gap_optionsV3.py` works best on simple stroked SVG paths with no fill. It supports `M`, `L`, `H`, `V`, `C`, `S`, `Q`, `T`, and `Z` path commands. Elliptical arc commands are approximated, so convert arcs to cubic paths first when accuracy matters.

## Versioning and File Names

The current files use suffixes such as `V2` and `V3`. That is understandable for personal experiments, but it is usually not the best practice in a GitHub repository because Git already records every version.

Recommended future practice:

- Use stable script names, for example `svg_point2curve.py`, `svg_curve_div.py`, and `svg_crossing_gap_options.py`.
- Track changes with Git commits, tags, and GitHub releases, for example `v1.0.0`.
- Record user-facing changes in `CHANGELOG.md`.
- If you rename scripts later, keep small compatibility wrapper scripts for one release cycle so existing commands do not break suddenly.

I did not rename the scripts in this cleanup because existing filenames may already be part of your workflow.

## Publishing to GitHub

After reviewing the files, create the first commit:

```bash
git add .
git commit -m "Initial GitHub-ready project"
```

Then create an empty GitHub repository and follow GitHub's instructions to add it as `origin`, or use the GitHub CLI:

```bash
gh repo create svg-curve-scripts --public --source=. --remote=origin --push
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
