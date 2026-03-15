#!/usr/bin/env python3
"""
Convert LLVM Graphviz DOT to yEd-compatible GraphML.
Features:
- Structured parsing of LLVM Mrecord labels (Inputs, Operator, ID, Outputs).
- Renders exactly as HTML Tables in yEd (matches Graphviz visuals perfectly).
- Extracts edge colors and styles.
"""

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

def run_graphviz_to_json(dot_path: Path):
    dot_executable = "dot" 
    cmd = [dot_executable, "-Tjson", str(dot_path)]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print("Graphviz failed. Ensure 'dot' is installed and supports -Tjson.")
        print(e.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Could not find dot executable at {dot_executable}")
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
    解析 LLVM 的 Record 字符串。
    将例如：{{<s0>0|<s1>1}|RISCVISD::CALL|t34|{<d0>ch|<d1>glue}}
    提取为结构化字典：{'inputs': ['0', '1'], 'operator': 'RISCVISD::CALL', 'id': 't34', 'outputs': ['ch', 'glue']}
    """
    if raw_label == "\\N" or not raw_label:
        return {}
    
    raw_label = raw_label.strip('"')
    
    # 移除最外层包裹的 {}
    if raw_label.startswith('{') and raw_label.endswith('}'):
        depth = 0
        is_wrapping = True
        for i in range(len(raw_label) - 1):
            if raw_label[i] == '{': depth += 1
            elif raw_label[i] == '}': depth -= 1
            if depth == 0 and i > 0:
                is_wrapping = False
                break
        if is_wrapping:
            raw_label = raw_label[1:-1]
            
    # 按顶层 '|' 分割
    parts = []
    curr = []
    depth = 0
    i = 0
    while i < len(raw_label):
        c = raw_label[i]
        if c == '\\':
            curr.append(c)
            i += 1
            if i < len(raw_label): curr.append(raw_label[i])
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
        # 带有 {} 的通常是输入或输出列表
        if p.startswith('{') and p.endswith('}'):
            sub_parts = p[1:-1].split('|')
            # 剥离如 <s0>, <d1> 这样的端口符号
            clean_subs = [re.sub(r'<[^>]+>', '', sub).strip() for sub in sub_parts]
            clean_subs = [clean_text(sub) for sub in clean_subs]
            
            # 如果出现在开头，就是 Inputs，否则是 Outputs
            if i == 0:
                parsed['inputs'] = clean_subs
            else:
                parsed['outputs'] = clean_subs
        else:
            # 普通文本（操作符或节点 ID）
            val = clean_text(p)
            if 'operator' not in parsed:
                parsed['operator'] = val
            else:
                parsed['id'] = val
                
    return parsed

def generate_yEd_html_table(parsed: dict) -> str:
    """基于提取的数据生成 yEd 识别的 HTML Table 字符串"""
    if not parsed:
        return ""
        
    # 计算最宽的列数，用于正确设置 colspan 对齐表格
    cols = 1
    if 'inputs' in parsed: cols = max(cols, len(parsed['inputs']))
    if 'outputs' in parsed: cols = max(cols, len(parsed['outputs']))
    
    html = ['<html><table border="1" cellpadding="3" cellspacing="0" style="font-family: Consolas, monospace; font-size: 11pt;">']
    
    # 1. 渲染输入 Inputs (0, 1, 2, 3...)
    if 'inputs' in parsed and parsed['inputs']:
        html.append('<tr>')
        n = len(parsed['inputs'])
        for i, val in enumerate(parsed['inputs']):
            span = cols // n + (1 if i < cols % n else 0)
            html.append(f'<td colspan="{span}" align="center">{escape(val)}</td>')
        html.append('</tr>')
        
    # 2. 渲染操作符 Operator (如 RISCVISD::CALL)
    if 'operator' in parsed:
        html.append(f'<tr><td colspan="{cols}" align="center"><b>{escape(parsed["operator"])}</b></td></tr>')
        
    # 3. 渲染节点编号 ID (如 t34)
    if 'id' in parsed:
        html.append(f'<tr><td colspan="{cols}" align="center">ID: {escape(parsed["id"])}</td></tr>')
        
    # 4. 渲染输出 Outputs (如 ch, glue)
    if 'outputs' in parsed and parsed['outputs']:
        html.append('<tr>')
        n = len(parsed['outputs'])
        for i, val in enumerate(parsed['outputs']):
            span = cols // n + (1 if i < cols % n else 0)
            html.append(f'<td colspan="{span}" align="center">{escape(val)}</td>')
        html.append('</tr>')
        
    html.append('</table></html>')
    return "".join(html)

def make_node_xml(node_id, label, x, y, w, h):
    """构建 XML，利用 CDATA 嵌入 HTML 标签"""
    parsed_info = parse_llvm_record(label)
    
    if parsed_info:
        html_str = generate_yEd_html_table(parsed_info)
        # 用 CDATA 包裹，yEd 会将其识别为 HTML 并渲染成表格
        final_label = f"<![CDATA[{html_str}]]>"
    else:
        # 兜底
        final_label = escape(label)

    return f"""    <node id="{node_id}">
      <data key="d0">
        <y:ShapeNode>
          <y:Geometry x="{x}" y="{-y}" width="{w}" height="{h}"/>
          <y:Fill color="#F9F9F9" transparent="false"/>
          <y:BorderStyle color="#000000" type="line" width="1.0"/>
          <y:NodeLabel alignment="center" autoSizePolicy="content" hasBackgroundColor="false" hasLineColor="false">{final_label}</y:NodeLabel>
          <y:Shape type="roundrectangle"/>
        </y:ShapeNode>
      </data>
    </node>"""

def parse_edge_style(e):
    color = e.get("color", "#000000")
    if color == "blue": color = "#0000FF"
    elif color == "red": color = "#FF0000"
    
    style_str = e.get("style", "line")
    width = "2.0" if "bold" in style_str else "1.0"
    y_style = "line"
    if "dashed" in style_str: y_style = "dashed"
    elif "dotted" in style_str: y_style = "dotted"
    
    return color, width, y_style

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 dot2xml.py input.dot output.graphml")
        sys.exit(1)

    dot_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    data = run_graphviz_to_json(dot_path)
    
    all_gvid_nodes = {}
    if "objects" in data:
        collect_nodes_recursive(data["objects"], all_gvid_nodes)

    written_node_ids = set()
    lines = [YED_GRAPHML_HEADER]

    for gvid, n in all_gvid_nodes.items():
        raw_name = n.get("name", str(gvid))
        node_id = normalize_id(raw_name)
        written_node_ids.add(node_id)
        
        label = n.get("label", raw_name)
        pos = n.get("pos", "0,0").split(",")
        x = float(pos[0]) if len(pos) > 1 else 0.0
        y = float(pos[1]) if len(pos) > 1 else 0.0
        
        # 放大坐标以适应表格带来的宽高增加 (扩大2.5倍坐标距)
        x = x * 2.5
        y = y * 2.5
        w = float(n.get("width", 1.0)) * 72.0
        h = float(n.get("height", 0.5)) * 72.0
        
        lines.append(make_node_xml(node_id, label, x, y, w, h))

    edges = data.get("edges", [])
    edge_id_counter = 0
    
    def get_final_id(gvid):
        if gvid in all_gvid_nodes: return normalize_id(all_gvid_nodes[gvid].get("name", str(gvid)))
        return normalize_id(gvid)

    for e in edges:
        src_id = get_final_id(e.get("tail"))
        tgt_id = get_final_id(e.get("head"))

        edge_color, edge_width, edge_style = parse_edge_style(e)
        edge_id_counter += 1
        lines.append(f"""    <edge id="e{edge_id_counter}" source="{src_id}" target="{tgt_id}">
      <data key="d1">
        <y:PolyLineEdge>
          <y:LineStyle color="{edge_color}" type="{edge_style}" width="{edge_width}"/>
          <y:Arrows source="none" target="standard"/>
        </y:PolyLineEdge>
      </data>
    </edge>""")

    lines.append(YED_GRAPHML_FOOTER)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Success! Wrote: {out_path}")

if __name__ == "__main__":
    main()
