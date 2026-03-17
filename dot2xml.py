#!/usr/bin/env python3
"""
Convert Graphviz DOT to yEd-compatible GraphML.

Fixes:
- If node label is '\\N' (Graphviz placeholder meaning node name), use node name instead.
- Add config file support for node/table node/edge styling.
- Better CLI: output defaults to input filename with .graphml suffix.

Notes:
- Table nodes: LLVM-style Mrecord labels are rendered as HTML tables in yEd via CDATA.
- "Normal" nodes: rendered with y:ShapeNode + y:NodeLabel.
"""

import argparse
import json
import subprocess
import sys
import re
from pathlib import Path
from xml.sax.saxutils import escape


YED_GRAPHML_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xmlns:y="http://www.yworks.com/xml/graphml"
         xsi:schemaLocation="http://graphml.graphdrawing.org/xmlns
                             http://www.yworks.com/xml/schema/graphml/1.1/ygraphml.xsd">
  <key id="d0" for="node" yfiles.type="nodegraphics"/>
  <key id="d1" for="edge" yfiles.type="edgegraphics"/>
  <graph id="G" edgedefault="directed">
"""

YED_GRAPHML_FOOTER = """  </graph>
</graphml>
"""


DEFAULT_CONFIG = {
    "graphviz": {
        # If empty, uses "dot" from PATH.
        # Windows example: "F:\\Downloads\\Graphviz\\bin\\dot"
        "dot_executable": ""
    },
    "layout": {
        "scale_pos": 2.5,
        "flip_y": True
    },
    "nodes": {
        "default": {
            "fill_color": "#F9F9F9",
            "border_color": "#000000",
            "border_width": 1.0,
            "shape": "roundrectangle",
            "font_family": "Consolas",
            "font_size": 12,
            "font_color": "#000000",
        },
        # Optional: when LLVM table label detected
        "table": {
            "fill_color": "#FFFFFF",
            "border_color": "#000000",
            "border_width": 1.0,
            "shape": "rectangle",
            "font_family": "Consolas",
            "font_size": 11,
            "font_color": "#000000",
            # HTML table border (inside CDATA): kept in generate_yEd_html_table()
        },
        # Optional: override by node name regex
        "overrides": [
            # {"match": "^block_0$", "style": {"fill_color": "#FFF2CC"}},
        ]
    },
    "edges": {
        "default": {
            "color": "#000000",
            "width": 1.0,
            "style": "line",          # yEd line type: line/dashed/dotted
            "arrow_target": "standard",
            "arrow_source": "none"
        },
        # Map graphviz named colors to hex (extend as you like)
        "color_map": {
            "blue": "#0000FF",
            "red": "#FF0000",
            "black": "#000000",
            "gray": "#808080",
        },
        # Map graphviz styles to yEd
        "style_map": {
            "dashed": "dashed",
            "dotted": "dotted",
            "solid": "line",
            "bold": "line"
        },
        "bold_width": 2.0
    }
}


def deep_merge(a: dict, b: dict) -> dict:
    """Return a merged dict: a updated with b recursively (b wins)."""
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config_path: Path | None) -> dict:
    cfg = DEFAULT_CONFIG
    if not config_path:
        return cfg
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(2)
    try:
        user_cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error: failed to parse config json: {config_path}\n{e}", file=sys.stderr)
        sys.exit(2)
    return deep_merge(cfg, user_cfg)


def run_graphviz_to_json(dot_path: Path, dot_executable: str):
    exe = dot_executable.strip() if dot_executable else ""
    cmd = [exe or "dot", "-Tjson", str(dot_path)]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print("Graphviz failed. Ensure 'dot' is installed and supports -Tjson.", file=sys.stderr)
        print(e.stderr, file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Could not find dot executable: {cmd[0]}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def normalize_id(raw_id) -> str:
    return "n_" + "".join(c if c.isalnum() or c in "_-" else "_" for c in str(raw_id))


def collect_nodes_recursive(objects, gvid_to_node):
    for obj in objects:
        if "_gvid" in obj and "objects" not in obj:
            gvid_to_node[obj["_gvid"]] = obj
        if "objects" in obj:
            collect_nodes_recursive(obj["objects"], gvid_to_node)


def parse_llvm_record(raw_label: str) -> dict:
    """
    Parse LLVM record labels (Graphviz record/Mrecord-like).
    Example: {{<s0>0|<s1>1}|RISCVISD::CALL|t34|{<d0>ch|<d1>glue}}
    """
    if raw_label == "\\N" or not raw_label:
        return {}

    raw_label = raw_label.strip('"')

    # remove outer {}
    if raw_label.startswith('{') and raw_label.endswith('}'):
        depth = 0
        is_wrapping = True
        for i in range(len(raw_label) - 1):
            if raw_label[i] == '{':
                depth += 1
            elif raw_label[i] == '}':
                depth -= 1
            if depth == 0 and i > 0:
                is_wrapping = False
                break
        if is_wrapping:
            raw_label = raw_label[1:-1]

    # split by top-level |
    parts = []
    curr = []
    depth = 0
    i = 0
    while i < len(raw_label):
        c = raw_label[i]
        if c == '\\':
            curr.append(c)
            i += 1
            if i < len(raw_label):
                curr.append(raw_label[i])
        elif c == '{':
            depth += 1
            curr.append(c)
        elif c == '}':
            depth -= 1
            curr.append(c)
        elif c == '|' and depth == 0:
            parts.append(''.join(curr))
            curr = []
        else:
            curr.append(c)
        i += 1
    if curr:
        parts.append(''.join(curr))

    def clean_text(text):
        t = text.replace('\\<', '<').replace('\\>', '>')
        t = re.sub(r'\\([{}|"])', r'\1', t)
        return t.strip()

    parsed = {}
    for i, p in enumerate(parts):
        if p.startswith('{') and p.endswith('}'):
            sub_parts = p[1:-1].split('|')
            clean_subs = [re.sub(r'<[^>]+>', '', sub).strip() for sub in sub_parts]
            clean_subs = [clean_text(sub) for sub in clean_subs]
            if i == 0:
                parsed['inputs'] = clean_subs
            else:
                parsed['outputs'] = clean_subs
        else:
            val = clean_text(p)
            if 'operator' not in parsed:
                parsed['operator'] = val
            else:
                parsed['id'] = val

    return parsed


def generate_yEd_html_table(parsed: dict, font_family="Consolas", font_size=11, font_color="#000000") -> str:
    """Generate yEd HTML table label (wrapped later in CDATA)."""
    if not parsed:
        return ""

    cols = 1
    if 'inputs' in parsed:
        cols = max(cols, len(parsed['inputs']) or 1)
    if 'outputs' in parsed:
        cols = max(cols, len(parsed['outputs']) or 1)

    html = [(
        '<html><table border="1" cellpadding="3" cellspacing="0" '
        f'style="font-family: {escape(font_family)}; font-size: {int(font_size)}pt; color: {escape(font_color)};">'
    )]

    if parsed.get('inputs'):
        html.append('<tr>')
        n = len(parsed['inputs'])
        for i, val in enumerate(parsed['inputs']):
            span = cols // n + (1 if i < cols % n else 0)
            html.append(f'<td colspan="{span}" align="center">{escape(val)}</td>')
        html.append('</tr>')

    if parsed.get('operator'):
        html.append(f'<tr><td colspan="{cols}" align="center"><b>{escape(parsed["operator"])}</b></td></tr>')

    if parsed.get('id'):
        html.append(f'<tr><td colspan="{cols}" align="center">ID: {escape(parsed["id"])}</td></tr>')

    if parsed.get('outputs'):
        html.append('<tr>')
        n = len(parsed['outputs'])
        for i, val in enumerate(parsed['outputs']):
            span = cols // n + (1 if i < cols % n else 0)
            html.append(f'<td colspan="{span}" align="center">{escape(val)}</td>')
        html.append('</tr>')

    html.append('</table></html>')
    return "".join(html)


def resolve_node_style(cfg: dict, node_name: str, is_table: bool) -> dict:
    base = cfg["nodes"]["table"] if is_table else cfg["nodes"]["default"]
    style = dict(base)

    for ov in cfg["nodes"].get("overrides", []) or []:
        pat = ov.get("match")
        if not pat:
            continue
        if re.search(pat, node_name):
            style.update(ov.get("style") or {})
    return style


def make_node_xml(node_id, node_name, raw_label, x, y, w, h, cfg: dict):
    """
    Build yEd node.
    - raw_label might be '\\N' (placeholder): show node_name instead.
    - If LLVM record label detected: render as HTML table.
    """
    label = raw_label if raw_label is not None else node_name
    if label == "\\N" or label.strip('"') == "\\N":
        label = node_name

    parsed_info = parse_llvm_record(label)
    is_table = bool(parsed_info)
    style = resolve_node_style(cfg, node_name, is_table)

    if is_table:
        html_str = generate_yEd_html_table(
            parsed_info,
            font_family=style.get("font_family", "Consolas"),
            font_size=style.get("font_size", 11),
            font_color=style.get("font_color", "#000000"),
        )
        final_label = f"<![CDATA[{html_str}]]>"
    else:
        final_label = escape(label)

    fill = style.get("fill_color", "#F9F9F9")
    border_color = style.get("border_color", "#000000")
    border_width = float(style.get("border_width", 1.0))
    shape = style.get("shape", "roundrectangle")

    # yEd NodeLabel supports fontFamily/fontSize/textColor attributes
    font_family = escape(style.get("font_family", "Consolas"))
    font_size = int(style.get("font_size", 12))
    font_color = escape(style.get("font_color", "#000000"))

    return f"""    <node id="{node_id}">
      <data key="d0">
        <y:ShapeNode>
          <y:Geometry x="{x}" y="{y}" width="{w}" height="{h}"/>
          <y:Fill color="{fill}" transparent="false"/>
          <y:BorderStyle color="{border_color}" type="line" width="{border_width}"/>
          <y:NodeLabel alignment="center" autoSizePolicy="content" hasBackgroundColor="false" hasLineColor="false"
                       fontFamily="{font_family}" fontSize="{font_size}" textColor="{font_color}">{final_label}</y:NodeLabel>
          <y:Shape type="{shape}"/>
        </y:ShapeNode>
      </data>
    </node>"""


def parse_edge_style(e: dict, cfg: dict):
    edges_cfg = cfg["edges"]
    defaults = edges_cfg["default"]
    color_map = edges_cfg.get("color_map") or {}
    style_map = edges_cfg.get("style_map") or {}

    # color
    raw_color = e.get("color") or defaults.get("color", "#000000")
    color = color_map.get(raw_color, raw_color)
    if not color.startswith("#") and re.fullmatch(r"[0-9A-Fa-f]{6}", color or ""):
        color = "#" + color

    # style/width
    style_str = (e.get("style") or "").strip()
    width = float(defaults.get("width", 1.0))
    y_style = defaults.get("style", "line")

    # graphviz style might contain multiple words: "bold,dashed"
    style_tokens = [t.strip() for t in re.split(r"[, ]+", style_str) if t.strip()]
    if "bold" in style_tokens:
        width = float(edges_cfg.get("bold_width", 2.0))

    # choose first known dash style if any
    for tok in style_tokens:
        mapped = style_map.get(tok)
        if mapped:
            y_style = mapped
            break

    arrow_target = defaults.get("arrow_target", "standard")
    arrow_source = defaults.get("arrow_source", "none")
    return color, width, y_style, arrow_source, arrow_target


def main():
    parser = argparse.ArgumentParser(description="Convert DOT to yEd GraphML.")
    parser.add_argument("input", help="input .dot file")
    parser.add_argument("-o", "--output", help="output .graphml file (default: input name with .graphml suffix)")
    parser.add_argument("-c", "--config", help="config json file", default=None)
    args = parser.parse_args()

    dot_path = Path(args.input)
    if not dot_path.exists():
        print(f"Error: input not found: {dot_path}", file=sys.stderr)
        sys.exit(2)

    out_path = Path(args.output) if args.output else dot_path.with_suffix(".graphml")
    cfg = load_config(Path(args.config) if args.config else None)

    data = run_graphviz_to_json(dot_path, cfg["graphviz"].get("dot_executable", ""))

    all_gvid_nodes = {}
    if "objects" in data:
        collect_nodes_recursive(data["objects"], all_gvid_nodes)

    lines = [YED_GRAPHML_HEADER]

    scale = float(cfg["layout"].get("scale_pos", 2.5))
    flip_y = bool(cfg["layout"].get("flip_y", True))

    for gvid, n in all_gvid_nodes.items():
        raw_name = n.get("name", str(gvid))
        node_name = str(raw_name)
        node_id = normalize_id(node_name)

        raw_label = n.get("label", None)

        pos = str(n.get("pos", "0,0")).split(",")
        x = float(pos[0]) if len(pos) > 1 else 0.0
        y = float(pos[1]) if len(pos) > 1 else 0.0

        x = x * scale
        y = y * scale
        if flip_y:
            y = -y

        w = float(n.get("width", 1.0)) * 72.0
        h = float(n.get("height", 0.5)) * 72.0

        lines.append(make_node_xml(node_id, node_name, raw_label, x, y, w, h, cfg))

    edges = data.get("edges", [])
    edge_id_counter = 0

    def get_final_id(gvid):
        if gvid in all_gvid_nodes:
            return normalize_id(all_gvid_nodes[gvid].get("name", str(gvid)))
        return normalize_id(gvid)

    for e in edges:
        src_id = get_final_id(e.get("tail"))
        tgt_id = get_final_id(e.get("head"))

        edge_color, edge_width, edge_style, arrow_source, arrow_target = parse_edge_style(e, cfg)
        edge_id_counter += 1
        lines.append(f"""    <edge id="e{edge_id_counter}" source="{src_id}" target="{tgt_id}">
      <data key="d1">
        <y:PolyLineEdge>
          <y:LineStyle color="{edge_color}" type="{edge_style}" width="{edge_width}"/>
          <y:Arrows source="{arrow_source}" target="{arrow_target}"/>
        </y:PolyLineEdge>
      </data>
    </edge>""")

    lines.append(YED_GRAPHML_FOOTER)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Success! Wrote: {out_path}")


if __name__ == "__main__":
    main()
