"""Orthogonal architecture drawings, shared by Web and standalone SVG files."""

import math
import re
from xml.etree.ElementTree import Element, SubElement, tostring

TONES = {
    "runtime": ("#edf3fb", "#91a9c6"), "pilot": ("#e9f6ef", "#77a98c"),
    "store": ("#f1edfa", "#aa97c2"), "owner": ("#fff5e3", "#c4a461"),
    "output": ("#e5f4f1", "#69a392"), "neutral": ("#ffffff", "#a8b7bf"),
}
COLORS = {"flow": "#386b55", "data": "#5d7f9e", "event": "#b47b43", "control": "#8b71a7"}
BOUNDARIES = {
    "host": ("#f5faf7", "#54896a", "执行位置 · 可信Host程序"),
    "container": ("#f1f8fc", "#478fa9", "执行位置 · 容器负载"),
    "remote": ("#f6f3fc", "#8870ad", "执行位置 · 获准远端节点"),
    "external": ("#fff8ed", "#b58a49", "依赖边界 · 外部进程或服务"),
    "client": ("#faf8f1", "#a08b59", "入口边界 · 用户控制端"),
    "session": ("#f7f5fc", "#9784b2", "数据范围 · 会话作用域"),
    "workspace": ("#f5f8fc", "#8096af", "数据范围 · 文件与持久存储"),
    "runtime": ("#f3f7fd", "#91a9c6", "调用边界 · 原生运行时"),
}


def inside(point, box):
    x, y = point
    return box["x"] < x < box["x"] + box["w"] and box["y"] < y < box["y"] + box["h"]


def border(point, box):
    x, y = point
    return (x in (box["x"], box["x"] + box["w"]) and box["y"] <= y <= box["y"] + box["h"]
            or y in (box["y"], box["y"] + box["h"]) and box["x"] <= x <= box["x"] + box["w"])


def intersection(a, b, c, d):
    """None, a shared point, or a positive-length collinear overlap."""
    av, cv = a[0] == b[0], c[0] == d[0]
    if av == cv:
        fixed, axis = (0, 1) if av else (1, 0)
        if a[fixed] != c[fixed]:
            return None
        lo = max(min(a[axis], b[axis]), min(c[axis], d[axis]))
        hi = min(max(a[axis], b[axis]), max(c[axis], d[axis]))
        if lo > hi:
            return None
        if lo < hi:
            return "overlap"
        return (a[0], lo) if av else (lo, a[1])
    if not av:
        return intersection(c, d, a, b)
    point = (a[0], c[1])
    return point if min(a[1], b[1]) <= point[1] <= max(a[1], b[1]) and min(c[0], d[0]) <= point[0] <= max(c[0], d[0]) else None


def path_distance(point, points):
    x, y = point
    return min(abs(x-a[0]) + max(min(a[1], b[1])-y, 0, y-max(a[1], b[1])) if a[0] == b[0]
               else abs(y-a[1]) + max(min(a[0], b[0])-x, 0, x-max(a[0], b[0]))
               for a, b in zip(points, points[1:], strict=False))


def validate(diagram, components, pages):
    width, height = diagram["width"], diagram["height"]
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in (width, height)):
        raise ValueError("Invalid diagram size")
    boxes = {n["id"]: n for n in [*diagram.get("groups", []), *diagram["nodes"]]}
    if len(boxes) != len(diagram.get("groups", [])) + len(diagram["nodes"]):
        raise ValueError("Duplicate diagram node")
    for node in boxes.values():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", node["id"]):
            raise ValueError("Invalid diagram identity")
        if node.get("tone", "neutral") not in TONES or node.get("shape", "box") not in {"box", "diamond", "store", "junction"}:
            raise ValueError("Invalid diagram node style")
        if "component" in node and node["component"] not in components or "target" in node and node["target"] not in pages:
            raise ValueError("Invalid diagram source or target")
        for key in ("x", "y", "w", "h"):
            if not isinstance(node[key], (int, float)) or not math.isfinite(node[key]) or node[key] < 0:
                raise ValueError("Invalid diagram coordinates")
        if node["w"] <= 0 or node["h"] <= 0 or node["x"] + node["w"] > width or node["y"] + node["h"] > height:
            raise ValueError("Diagram node outside canvas")
    nodes = diagram["nodes"]
    boundaries = {b["id"]: b for b in diagram.get("boundaries", [])}
    if len(boundaries) != len(diagram.get("boundaries", [])) or set(boundaries) & set(boxes):
        raise ValueError("Duplicate architecture boundary")
    for region in boundaries.values():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", region["id"]) or region["kind"] not in BOUNDARIES:
            raise ValueError("Invalid architecture boundary kind")
        for key in ("x", "y", "w", "h"):
            if not isinstance(region[key], (int, float)) or not math.isfinite(region[key]) or region[key] < 0:
                raise ValueError("Invalid architecture boundary coordinates")
        if any(not isinstance(region[k], str) or not region[k] or len(region[k]) > 500 for k in ("label", "subtitle")) or region["w"] <= 0 or region["h"] <= 0 or region["x"] + region["w"] > width or region["y"] + region["h"] > height:
            raise ValueError("Architecture boundary outside canvas or missing contract")
        if region.get("target") and region["target"] not in pages:
            raise ValueError("Invalid architecture boundary target")
        if not region["members"] or len(set(region["members"])) != len(region["members"]):
            raise ValueError("Architecture boundary needs explicit members")
        for identity in region["members"]:
            node = next((n for n in nodes if n["id"] == identity), None)
            if node is None or not (region["x"] <= node["x"] and region["y"] + 60 <= node["y"] and node["x"] + node["w"] <= region["x"] + region["w"] and node["y"] + node["h"] <= region["y"] + region["h"]):
                raise ValueError("Architecture boundary membership/label clearance mismatch")
        seen = {region["id"]}
        child = region
        while child.get("parent"):
            parent = boundaries.get(child["parent"])
            if parent is None or parent["id"] in seen or not (parent["x"] <= child["x"] and parent["y"] + 60 <= child["y"] and child["x"] + child["w"] <= parent["x"] + parent["w"] and child["y"] + child["h"] <= parent["y"] + parent["h"]):
                raise ValueError("Invalid architecture boundary nesting")
            seen.add(parent["id"])
            child = parent
        if region["kind"] in {"host", "container", "remote", "external", "client"}:
            for node in nodes:
                contained = region["x"] <= node["x"] and region["y"] <= node["y"] and node["x"] + node["w"] <= region["x"] + region["w"] and node["y"] + node["h"] <= region["y"] + region["h"]
                if contained and node["id"] not in region["members"]:
                    raise ValueError("Architecture execution boundary contains an undeclared node")
    def ancestors(region):
        result = set()
        while region.get("parent"):
            region = boundaries[region["parent"]]
            result.add(region["id"])
        return result
    physical = [b for b in boundaries.values() if b["kind"] in {"host", "container", "remote", "external", "client"}]
    for i, left in enumerate(physical):
        for right in physical[i + 1:]:
            overlapping = max(left["x"], right["x"]) < min(left["x"] + left["w"], right["x"] + right["w"]) and max(left["y"], right["y"]) < min(left["y"] + left["h"], right["y"] + right["h"])
            if overlapping and left["id"] not in ancestors(right) and right["id"] not in ancestors(left):
                raise ValueError(f"Overlapping unrelated architecture execution boundaries: {left['id']} / {right['id']}")
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if max(a["x"], b["x"]) < min(a["x"] + a["w"], b["x"] + b["w"]) and max(a["y"], b["y"]) < min(a["y"] + a["h"], b["y"] + b["h"]):
                raise ValueError(f"Overlapping diagram nodes: {a['id']} / {b['id']}")
    segments = []
    for i, edge in enumerate(diagram["edges"]):
        if edge["from"] not in boxes or edge["to"] not in boxes or edge["kind"] not in COLORS:
            raise ValueError("Invalid diagram edge")
        points = edge["points"]
        if len(points) < 2 or any(len(p) != 2 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) or not 0 <= p[0] <= width or not 0 <= p[1] <= height for p in points):
            raise ValueError("Invalid diagram route")
        if not border(points[0], boxes[edge["from"]]) or not border(points[-1], boxes[edge["to"]]):
            raise ValueError(f"Edge port is not on its node: {edge['from']} -> {edge['to']}")
        for a, b in zip(points, points[1:], strict=False):
            if a == b or a[0] != b[0] and a[1] != b[1]:
                raise ValueError("Diagram routes must use nonzero orthogonal segments")
            for node in nodes:
                # Midpoint clipping also catches a segment spanning an entire box.
                if a[0] == b[0]:
                    lo, hi = max(min(a[1], b[1]), node["y"]), min(max(a[1], b[1]), node["y"] + node["h"])
                    hit = lo < hi and node["x"] < a[0] < node["x"] + node["w"]
                else:
                    lo, hi = max(min(a[0], b[0]), node["x"]), min(max(a[0], b[0]), node["x"] + node["w"])
                    hit = lo < hi and node["y"] < a[1] < node["y"] + node["h"]
                if hit:
                    raise ValueError(f"Route crosses node {node['id']}: {edge['from']} -> {edge['to']}")
            segments.append((i, tuple(a), tuple(b)))
        if edge.get("label"):
            anchor = edge.get("label_at")
            if not isinstance(anchor, list) or len(anchor) != 2 or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in anchor):
                raise ValueError("Invalid diagram edge caption position")
            if not 0 <= anchor[0] <= width or not 0 <= anchor[1] <= height or path_distance(anchor, points) > 80 or any(inside(anchor, node) for node in nodes):
                raise ValueError(f"Diagram edge caption detached or inside a node: {edge['from']}->{edge['to']}")
    for j, (i, a, b) in enumerate(segments):
        for k, c, d in segments[j + 1:]:
            if i == k:
                continue
            hit = intersection(a, b, c, d)
            if hit is None:
                continue
            first, second = diagram["edges"][i], diagram["edges"][k]
            shared = {first["from"], first["to"]} & {second["from"], second["to"]}
            endpoints = {tuple(first["points"][0]), tuple(first["points"][-1])} & {tuple(second["points"][0]), tuple(second["points"][-1])}
            if hit == "overlap" or not shared or hit not in endpoints:
                raise ValueError(f"Intersecting routes: {first['from']}->{first['to']} / {second['from']}->{second['to']} ({hit})")


def text(parent, x, y, value, size=22, weight="400", anchor="middle", color="#243c49", **attrs):
    node = SubElement(parent, "text", {"x": str(x), "y": str(y), "font-size": str(size), "font-weight": weight, "text-anchor": anchor, "fill": color, **attrs})
    node.text = value
    return node


def svg(view, components):
    diagram = view["diagram"]
    width, height = diagram["width"], diagram["height"]
    prefix = "pilot_" + view["id"]
    root = Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": f"0 0 {width} {height}", "width": str(width), "height": str(height), "role": "group", "aria-label": view["label"], "data-architecture-view": view["id"], "font-family": "Microsoft YaHei, Segoe UI, sans-serif"})
    SubElement(root, "title").text = "Pilot · " + view["label"]
    SubElement(root, "desc").text = view["description"]
    SubElement(root, "rect", {"width": str(width), "height": str(height), "rx": "16", "fill": "#fbfcfd"})
    defs = SubElement(root, "defs")
    for kind, color in COLORS.items():
        marker = SubElement(defs, "marker", {"id": prefix + "_" + kind, "viewBox": "0 0 10 10", "refX": "9", "refY": "5", "markerWidth": "7", "markerHeight": "7", "orient": "auto-start-reverse"})
        SubElement(marker, "path", {"d": "M 0 0 L 10 5 L 0 10 z", "fill": color})
    for region in diagram.get("boundaries", []):
        fill, stroke, meaning = BOUNDARIES[region["kind"]]
        attrs = {"data-architecture-boundary": region["id"], "data-boundary-kind": region["kind"], "role": "group", "aria-label": region["label"]}
        boundary = SubElement(root, "g", attrs)
        SubElement(boundary, "title").text = meaning + "：" + region["subtitle"]
        SubElement(boundary, "rect", {"x": str(region["x"]), "y": str(region["y"]), "width": str(region["w"]), "height": str(region["h"]), "rx": "16", "fill": fill, "fill-opacity": ".5", "stroke": stroke, "stroke-width": "2", "stroke-dasharray": "9 5" if region["kind"] in {"session", "workspace", "runtime"} else "none"})
        header = SubElement(boundary, "g", {"role": "button", "tabindex": "0", "data-architecture-target": region["target"], "aria-label": region["label"] + "，打开设计", "style": "cursor:pointer"} if region.get("target") else {})
        text(header, region["x"] + 18, region["y"] + 26, region["label"], region.get("font_size", 18), "650", "start", stroke)
        text(header, region["x"] + 18, region["y"] + 49, region["subtitle"], region.get("subtitle_size", 14), anchor="start", color=stroke)
    for group in diagram.get("groups", []):
        fill, stroke = TONES[group.get("tone", "runtime")]
        SubElement(root, "rect", {"x": str(group["x"]), "y": str(group["y"]), "width": str(group["w"]), "height": str(group["h"]), "rx": "18", "fill": fill, "fill-opacity": ".35", "stroke": stroke, "stroke-width": "1.5", "stroke-dasharray": "7 5"})
        attrs = {"role": "button", "tabindex": "0", "data-architecture-target": group["target"], "aria-label": group["label"] + "，打开设计", "style": "cursor:pointer"} if group.get("target") else {}
        header = SubElement(root, "g", attrs)
        if group.get("target"):
            SubElement(header, "rect", {"x": str(group["x"] + 8), "y": str(group["y"] + 4),
                "width": str(group["w"] - 16), "height": "34", "fill": "transparent", "pointer-events": "all"})
        text(header, group["x"] + 18, group["y"] + 28, group["label"], 19, "650", "start")
        if group.get("subtitle"):
            text(header, group["x"] + group["w"] - 18, group["y"] + 28, group["subtitle"], 14, anchor="end", color="#668096")
    for edge in diagram["edges"]:
        color = COLORS[edge["kind"]]
        points = edge["points"]
        attrs = {"d": " ".join(("M" if i == 0 else "L") + " " + " ".join(str(v) for v in p) for i, p in enumerate(points)), "fill": "none", "stroke": color, "stroke-width": "2", "stroke-linejoin": "round", "marker-end": "url(#" + prefix + "_" + edge["kind"] + ")"}
        if edge["kind"] != "flow":
            attrs["stroke-dasharray"] = "6 5" if edge["kind"] != "control" else "3 5"
        if edge.get("bidirectional"):
            attrs["marker-start"] = attrs["marker-end"]
        SubElement(root, "path", attrs)
        if edge.get("label"):
            x, y = edge["label_at"]
            transform = {"transform": f"rotate({edge.get('rotate', 0)} {x} {y})"} if edge.get("rotate") else {}
            text(root, x, y, edge["label"], edge.get("label_size", 16), color=color, **{"paint-order": "stroke", "stroke": "#fbfcfd", "stroke-width": "6", "stroke-linejoin": "round", **transform})
    for node in diagram["nodes"]:
        component = components.get(node.get("component"), {})
        label, subtitle = node.get("label", component.get("label", "")), node.get("subtitle", component.get("subtitle", ""))
        attrs = {"data-architecture-node": node["id"]}
        if node.get("target"):
            attrs.update({"data-architecture-target": node["target"], "role": "button", "tabindex": "0", "aria-label": label + "，打开设计", "style": "cursor:pointer"})
        elif node.get("component"):
            attrs.update({"data-architecture-component": node["component"], "role": "button", "tabindex": "0", "aria-label": label + "，查看职责", "style": "cursor:pointer"})
        g = SubElement(root, "g", attrs)
        x, y, w, h = (node[k] for k in ("x", "y", "w", "h"))
        fill, stroke = TONES[node.get("tone", "neutral")]
        shape = node.get("shape", "box")
        common = {"fill": fill, "stroke": stroke, "stroke-width": "1.7"}
        if shape == "diamond":
            SubElement(g, "path", {"d": f"M {x+w/2} {y} L {x+w} {y+h/2} L {x+w/2} {y+h} L {x} {y+h/2} Z", **common})
        elif shape == "store":
            SubElement(g, "path", {"d": f"M {x} {y+12} C {x} {y-4} {x+w} {y-4} {x+w} {y+12} L {x+w} {y+h-12} C {x+w} {y+h+4} {x} {y+h+4} {x} {y+h-12} Z", **common})
            SubElement(g, "path", {"d": f"M {x} {y+12} C {x} {y+28} {x+w} {y+28} {x+w} {y+12}", "fill": "none", "stroke": stroke, "stroke-width": "1.4"})
        elif shape == "junction":
            SubElement(g, "circle", {"cx": str(x+w/2), "cy": str(y+h/2), "r": "4", "fill": stroke})
            continue
        else:
            SubElement(g, "rect", {"x": str(x), "y": str(y), "width": str(w), "height": str(h), "rx": "12", **common})
        labels = label.split("\n")
        for i, line in enumerate(labels):
            text(g, x+w/2, y+h/2-6 + (i-(len(labels)-1)/2)*26, line, node.get("font_size", 22), "650")
        if subtitle:
            text(g, x+w/2, y+h/2+(17 if shape == "diamond" else 25), subtitle,
                 node.get("subtitle_size", 14 if shape == "diamond" else 16), color="#647987")
    for note in diagram.get("annotations", []):
        text(root, note["x"], note["y"], note["text"], note.get("size", 16), anchor=note.get("anchor", "middle"), color="#6b7f8d")
    return tostring(root, encoding="unicode") + "\n"
