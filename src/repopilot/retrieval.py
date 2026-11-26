from __future__ import annotations

import re
from pathlib import Path

import networkx as nx
import tree_sitter_python
from tree_sitter import Language, Parser

from .repository import files, safe_path
from .storage import atomic_json, digest, read_json


class RepoMap:
    def __init__(self, repo: Path, cache: Path):
        self.repo, self.cache = repo, cache
        self.parser = Parser(Language(tree_sitter_python.language()))
        self.entries = read_json(cache).get("files", {}) if cache.exists() else {}
        self.updated: list[str] = []

    def refresh(self) -> None:
        self.updated = []
        existing = set()
        for path in files(self.repo):
            if path.suffix != ".py" or path.stat().st_size > 1_000_000:
                continue
            relative = path.relative_to(self.repo).as_posix()
            existing.add(relative)
            data = path.read_bytes()
            sha = digest(data)
            if self.entries.get(relative, {}).get("sha") == sha:
                continue
            tree = self.parser.parse(data)
            definitions, refs = [], set()
            stack = [tree.root_node]
            while stack:
                node = stack.pop()
                if node.type in {"function_definition", "class_definition"}:
                    name = node.child_by_field_name("name")
                    if name:
                        definitions.append({"name": data[name.start_byte:name.end_byte].decode("utf-8"), "line": node.start_point.row + 1, "end": node.end_point.row + 1})
                elif node.type == "identifier":
                    refs.add(data[node.start_byte:node.end_byte].decode("utf-8"))
                stack.extend(reversed(node.children))
            self.entries[relative] = {"sha": sha, "definitions": definitions, "refs": sorted(refs), "parse_error": tree.root_node.has_error}
            self.updated.append(relative)
        self.entries = {k: v for k, v in self.entries.items() if k in existing}
        atomic_json(self.cache, {"parser": "tree-sitter-python-0.25", "files": self.entries})

    def rank(self, query: str, use_map: bool = True, limit: int = 8) -> list[dict]:
        self.refresh()
        terms = set(re.findall(r"[A-Za-z_]\w*", query))
        graph = nx.DiGraph()
        graph.add_nodes_from(self.entries)
        owners: dict[str, list[str]] = {}
        for file, entry in self.entries.items():
            for definition in entry["definitions"]:
                owners.setdefault(definition["name"], []).append(file)
        scores = {}
        for file, entry in self.entries.items():
            names = {d["name"] for d in entry["definitions"]}
            if use_map:
                scores[file] = 1 + 5 * len(names & terms) + len(set(entry["refs"]) & terms) + (10 if file in query else 0)
            else:
                text = (self.repo / file).read_text(encoding="utf-8", errors="replace")
                scores[file] = 1 + sum(text.count(t) for t in terms)
            if use_map:
                for ref in entry["refs"]:
                    for owner in owners.get(ref, []):
                        if owner != file:
                            graph.add_edge(file, owner, weight=graph.get_edge_data(file, owner, {}).get("weight", 0) + 1)
        # networkx's scipy PageRank is optional; use its graph with a deterministic
        # sparse power iteration so a heavyweight scipy wheel is unnecessary.
        ranks = {file: value / sum(scores.values()) for file, value in scores.items()}
        if use_map and ranks:
            personalization = ranks.copy()
            for _ in range(100):
                dangling = sum(ranks[n] for n in graph if graph.out_degree(n) == 0)
                new = {n: (0.15 + 0.85 * dangling) * personalization[n] for n in graph}
                for n in graph:
                    total = sum(graph[n][dest]["weight"] for dest in graph[n])
                    if total:
                        for dest in graph[n]:
                            new[dest] += 0.85 * ranks[n] * graph[n][dest]["weight"] / total
                delta = sum(abs(new[n] - ranks[n]) for n in graph)
                ranks = new
                if delta < 1e-8:
                    break
        result = []
        for file in sorted(ranks, key=lambda f: (-ranks[f], f))[:limit]:
            definitions = self.entries[file]['definitions']
            # Keep the complete graph on disk; only query-relevant symbols enter
            # a tool observation. Large third-party modules can have hundreds.
            selected = sorted(definitions, key=lambda d: (d['name'] not in terms, d['line']))[:12] if use_map else []
            result.append({'file': file, 'score': round(ranks[file], 6), 'symbols': selected,
                'symbols_omitted': len(definitions) - len(selected) if use_map else 0})
        return result

    def search(self, query: str, use_map: bool = True) -> dict:
        ranked = self.rank(query, use_map)
        hits = []
        terms = set(re.findall(r"[A-Za-z_]\w*", query))
        for item in ranked:
            path = safe_path(self.repo, item["file"])
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if any(term in line for term in terms):
                    hits.append({"file": item["file"], "line": n, "text": line[:300]})
                    if len(hits) >= 40:
                        break
        return {"ranked_files": ranked, "hits": hits[:40], "incrementally_updated": self.updated}
