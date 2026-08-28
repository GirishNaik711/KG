"""Generate interactive codebase-graph.html using embedded vis.js."""

import json

import networkx as nx

# vis.js CDN — embedded in the HTML so it works offline after first load
VIS_CDN = "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"

# Color palette by architectural role
ROLE_COLORS = {
    "entry_point": "#e74c3c",   # Red
    "api": "#3498db",           # Blue
    "model": "#2ecc71",         # Green
    "config": "#f39c12",        # Orange
    "test": "#95a5a6",          # Gray
    "utility": "#9b59b6",       # Purple
    "other": "#bdc3c7",         # Light gray
}

EDGE_COLORS = {
    "imports": "#2c3e50",
    "calls": "#e74c3c",
    "inherits": "#2ecc71",
    "implements": "#3498db",
    "uses": "#f39c12",
}


def generate_html(G: nx.DiGraph, title: str = "Codebase Knowledge Graph") -> str:
    """
    Generate a self-contained HTML file with interactive graph visualization.

    Uses vis.js loaded from CDN. The HTML includes all data inline so
    it can be opened directly in a browser.
    """
    # Prepare nodes
    vis_nodes = []
    for node, data in G.nodes(data=True):
        label = node.split("/")[-1] if "/" in node else node
        role = data.get("role", "other")
        color = ROLE_COLORS.get(role, ROLE_COLORS["other"])
        size = min(30, max(8, data.get("symbols", 0) * 2))

        vis_nodes.append({
            "id": node,
            "label": label,
            "title": f"{node}\nRole: {role}\nSymbols: {data.get('symbols', 0)}",
            "color": color,
            "size": size,
        })

    # Prepare edges
    vis_edges = []
    for u, v, data in G.edges(data=True):
        etype = data.get("type", "unknown")
        vis_edges.append({
            "from": u,
            "to": v,
            "color": {"color": EDGE_COLORS.get(etype, "#ccc")},
            "title": etype,
            "arrows": "to",
        })

    nodes_json = json.dumps(vis_nodes)
    edges_json = json.dumps(vis_edges)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <script src="{VIS_CDN}"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: -apple-system, sans-serif; }}
        #graph {{ width: 100vw; height: 100vh; }}
        #legend {{
            position: absolute; top: 10px; right: 10px;
            background: rgba(255,255,255,0.95); padding: 12px;
            border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.15);
            font-size: 12px;
        }}
        .legend-item {{ display: flex; align-items: center; margin: 4px 0; }}
        .legend-dot {{ width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; }}
        h3 {{ margin: 0 0 8px 0; font-size: 14px; }}
    </style>
</head>
<body>
    <div id="graph"></div>
    <div id="legend">
        <h3>Node Roles</h3>
        <div class="legend-item"><span class="legend-dot" style="background:#e74c3c"></span> Entry Point</div>
        <div class="legend-item"><span class="legend-dot" style="background:#3498db"></span> API</div>
        <div class="legend-item"><span class="legend-dot" style="background:#2ecc71"></span> Model</div>
        <div class="legend-item"><span class="legend-dot" style="background:#f39c12"></span> Config</div>
        <div class="legend-item"><span class="legend-dot" style="background:#9b59b6"></span> Utility</div>
        <h3 style="margin-top:12px">Edge Types</h3>
        <div class="legend-item"><span style="width:20px;height:2px;background:#2c3e50;margin-right:8px"></span> imports</div>
        <div class="legend-item"><span style="width:20px;height:2px;background:#e74c3c;margin-right:8px"></span> calls</div>
        <div class="legend-item"><span style="width:20px;height:2px;background:#2ecc71;margin-right:8px"></span> inherits</div>
    </div>
    <script>
        var nodes = new vis.DataSet({nodes_json});
        var edges = new vis.DataSet({edges_json});
        var container = document.getElementById("graph");
        var data = {{ nodes: nodes, edges: edges }};
        var options = {{
            physics: {{
                solver: "forceAtlas2Based",
                forceAtlas2Based: {{ gravitationalConstant: -50, centralGravity: 0.01 }},
                stabilization: {{ iterations: 200 }}
            }},
            interaction: {{ hover: true, tooltipDelay: 100 }},
            edges: {{ smooth: {{ type: "continuous" }} }}
        }};
        new vis.Network(container, data, options);
    </script>
</body>
</html>"""

    return html
