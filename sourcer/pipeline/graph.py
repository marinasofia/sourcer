"""LangGraph wiring: source -> dedupe -> parse -> score -> output."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from langgraph.graph import END, START, StateGraph

from .dedupe import dedupe_node
from .output import output_node
from .scoring import parse_node, score_node
from .source import source_node
from .state import State

NODES = (
    ("source", source_node),
    ("dedupe", dedupe_node),
    ("parse", parse_node),
    ("score", score_node),
    ("output", output_node),
)


def build_graph() -> Any:
    """Compile the linear pipeline graph."""
    g = StateGraph(State)
    for name, fn in NODES:
        g.add_node(name, fn)
    g.add_edge(START, NODES[0][0])
    for (a, _), (b, _) in pairwise(NODES):
        g.add_edge(a, b)
    g.add_edge(NODES[-1][0], END)
    return g.compile()
