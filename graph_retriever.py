import json
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
MAX_FILES = 15
MIN_SCORE = 8   # files scoring below this are not returned (cuts BFS noise with no term/dir signal)

FRONTEND_GRAPH = PROJECT_ROOT / "frontend_graph" / "graphify-out" / "graph.json"
BACKEND_GRAPH  = PROJECT_ROOT / "backend_graph"  / "graphify-out" / "graph.json"

_NODE_RE = re.compile(r"NODE .+? \[src=(.+?) loc=(.+?) community=(.+?)\]")

STOPWORDS = {
    "test", "workflow", "flow", "user", "system", "page", "screen",
    "the", "a", "an", "and", "or", "to", "for", "of", "in",
    # CRUD verbs — too generic, start BFS from every create/update/delete function
    "create", "update", "delete", "get", "fetch", "add", "remove",
    "set", "view", "show", "edit", "save", "submit", "send",
}


def extract_terms(query: str) -> list[str]:
    words = re.findall(r"[a-zA-Z_]+", query.lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


_GRAPH_ROOTS = {
    str(FRONTEND_GRAPH): "iserv_portal/src",
    str(BACKEND_GRAPH):  "iserv_server/src",
}


def _make_node(src: str, loc: str, community: str, graph_path: Path) -> dict:
    rel = src.strip().replace("\\", "/")
    root = _GRAPH_ROOTS.get(str(graph_path), "")
    full = f"{root}/{rel}" if root and not rel.startswith(root) else rel
    return {
        "source_file": full,
        "source_location": loc.strip(),
        "community": community.strip(),
        "graph_source": "frontend" if "iserv_portal" in root else "backend",
    }


def _run_graphify_query(term: str, graph_path: Path) -> list[dict]:
    if not graph_path.exists():
        return []
    result = subprocess.run(
        ["graphify", "query", term, "--graph", str(graph_path)],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    nodes = []
    for line in result.stdout.splitlines():
        m = _NODE_RE.search(line)
        if m:
            nodes.append(_make_node(m.group(1), m.group(2), m.group(3), graph_path))
    return nodes


def _run_graphify_path(from_term: str, to_term: str) -> list[dict]:
    nodes = []
    for graph_path in (FRONTEND_GRAPH, BACKEND_GRAPH):
        if not graph_path.exists():
            continue
        result = subprocess.run(
            ["graphify", "path", from_term, to_term, "--graph", str(graph_path)],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        for line in result.stdout.splitlines():
            m = _NODE_RE.search(line)
            if m:
                nodes.append(_make_node(m.group(1), m.group(2), m.group(3), graph_path))
    return nodes


def _dedup(nodes: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for node in nodes:
        sf = node["source_file"]
        if sf not in seen:
            seen[sf] = node
        elif seen[sf]["source_location"] == "L1" and node["source_location"] != "L1":
            seen[sf] = node
    return list(seen.values())


def _score(node: dict, query_terms: list[str]) -> int:
    path = node["source_file"]
    path_lower = path.lower()
    score = 0

    for term in query_terms:
        if term in path_lower:
            score += 15

    if "/routes/" in path_lower:
        score += 12
    if "/controllers/" in path_lower:
        score += 12
    if "/models/" in path_lower:
        score += 10
    if "/services/" in path_lower:
        score += 10
    if "/pages/" in path_lower:
        score += 10
    if "/middleware/" in path_lower:
        score += 8

    if node["source_location"] != "L1":
        score += 5

    if "/context/" in path_lower:
        score -= 15
    if "/provider/" in path_lower or "/providers/" in path_lower:
        score -= 15
    if "/layout/" in path_lower or "/layouts/" in path_lower:
        score -= 10
    if path_lower.endswith("/app.js"):
        score -= 20
    if path_lower.endswith("/index.js"):
        score -= 15

    return score


def category(path: str) -> str:
    p = path.lower()
    if "/routes/" in p:
        return "route"
    if "/controllers/" in p:
        return "controller"
    if "/models/" in p:
        return "model"
    if "/middleware/" in p:
        return "middleware"
    if "/services/" in p:
        return "service"
    if "/pages/" in p:
        return "page"
    if "/components/" in p:
        return "component"
    return "other"


def _diversify(nodes: list[dict], max_files: int) -> list[dict]:
    limits = {
        "route": 2,
        "controller": 2,
        "model": 3,
        "middleware": 1,
        "service": 2,
        "page": 2,
        "component": 2,
        "other": 2,
    }
    selected = []
    counts: dict[str, int] = {}

    for node in nodes:
        cat = category(node["source_file"])
        if counts.get(cat, 0) >= limits.get(cat, 999):
            continue
        selected.append(node)
        counts[cat] = counts.get(cat, 0) + 1
        if len(selected) >= max_files:
            break

    return selected


def _find_backend_routes_controllers(terms: list[str]) -> list[dict]:
    """
    BFS from model-class nodes can't reach controllers/routes because the
    import direction is controller→model, not model→controller. When BFS
    returns no backend routes or controllers, fall back to a direct glob.
    """
    backend_src = PROJECT_ROOT / "iserv_server" / "src"
    results = []
    seen: set[str] = set()
    for subdir in ("routes", "controllers"):
        subdir_path = backend_src / subdir
        if not subdir_path.exists():
            continue
        for f in subdir_path.rglob("*.js"):
            stem = f.stem.lower()
            # Bidirectional: "ticket" in "ticket.controller" ✓  AND  "auth" in "authentication" ✓
            if any(t in stem or stem in t for t in terms):
                rel = f.relative_to(PROJECT_ROOT).as_posix()
                if rel not in seen:
                    seen.add(rel)
                    results.append({
                        "source_file": rel,
                        "source_location": "L1",
                        "community": "_direct_lookup",
                        "graph_source": "backend",
                    })
    return results


def _community_fallback(term: str, graph_path: Path) -> list[dict]:
    """
    Fallback when graphify query returns 0 results.
    Scans graph.json for nodes whose community_name contains the query term.
    Bridges natural-language queries to graph structure — e.g. 'authentication'
    matches community_name 'Authentication and Security' even though no node
    label is literally 'authentication'.
    """
    if not graph_path.exists():
        return []
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    term_lower = term.lower()
    root = _GRAPH_ROOTS.get(str(graph_path), "")
    results: list[dict] = []
    for node in data.get("nodes", []):
        cn = node.get("community_name", "").lower()
        if not cn or term_lower not in cn:
            continue
        sf = node.get("source_file", "").strip().replace("\\", "/")
        if not sf:
            continue
        full = f"{root}/{sf}" if root and not sf.startswith(root) else sf
        results.append({
            "source_file": full,
            "source_location": node.get("source_location", "L1"),
            "community": str(node.get("community", "")),
            "graph_source": "frontend" if "iserv_portal" in root else "backend",
        })
    return results


def retrieve(query: str) -> list[dict]:
    terms = extract_terms(query)
    steps = [s.strip() for s in re.split(r"\s*->\s*", query)]

    raw: list[dict] = []
    for term in terms:
        nodes_f = _run_graphify_query(term, FRONTEND_GRAPH)
        nodes_b = _run_graphify_query(term, BACKEND_GRAPH)
        raw.extend(nodes_f)
        raw.extend(nodes_b)
        # If graphify found nothing, the term is likely a verbose English word
        # with no matching node label (e.g. "authentication" when graph has "auth").
        # Scan graph.json for labels that are substrings of the term and retry.
        if not nodes_f and not nodes_b:
            raw.extend(_community_fallback(term, FRONTEND_GRAPH))
            raw.extend(_community_fallback(term, BACKEND_GRAPH))

    for i in range(len(steps) - 1):
        raw.extend(_run_graphify_path(steps[i], steps[i + 1]))

    # Always supplement with direct glob: BFS import direction (controller→model)
    # means routes are never reachable from model-entry BFS, and controllers are
    # only reachable when a function node (not file node) is the BFS start.
    raw.extend(_find_backend_routes_controllers(terms))
    deduped = _dedup(raw)

    ranked = sorted(deduped, key=lambda n: _score(n, terms), reverse=True)
    ranked = [n for n in ranked if _score(n, terms) >= MIN_SCORE]
    return _diversify(ranked, MAX_FILES)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python graph_retriever.py <query>")
        print('Examples: python graph_retriever.py login')
        print('          python graph_retriever.py "create ticket"')
        sys.exit(1)
    query = " ".join(sys.argv[1:])
    nodes = retrieve(query)
    terms = extract_terms(query)

    print(f"\n{len(nodes)} files selected for: '{query}'")
    print(f"terms: {terms}\n")
    for n in nodes:
        s = _score(n, terms)
        cat = category(n["source_file"])
        print(
            f"  [{cat:12}] score={s:3}  "
            f"{n['source_file']} @ {n['source_location']}"
        )
