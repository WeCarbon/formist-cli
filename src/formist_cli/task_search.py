"""``nod search`` — a port of noddit's ``core/search.ts``."""

from __future__ import annotations

from formist_cli.task_model import Node


def search_words(query: str) -> list[str]:
    return [w for w in query.lower().split() if w]


def _haystack(node: Node) -> str:
    bits = [node.title, node.body]
    for a in node.attachments:
        bits.append(a.label)
        bits.append(a.target)
    return "\n".join(bits).lower()


def search(nodes: list[Node], words: list[str]) -> list[Node]:
    terms = [w.lower() for w in words if w]
    hits = [n for n in nodes if all(t in _haystack(n) for t in terms)]
    hits.sort(key=lambda n: n.updated_at or n.created, reverse=True)
    return hits
