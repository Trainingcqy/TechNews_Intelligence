"""Construction of the turn graph."""

from __future__ import annotations

from functools import cache

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .nodes import after_gather, after_route, collect, finalize, gather, prepare, route, run_tool, synthesize
from .state import TurnGraphState


@cache
def build_turn_graph() -> CompiledStateGraph:
    graph = StateGraph(TurnGraphState)
    for name, fn in (("prepare", prepare), ("gather", gather), ("run_tool", run_tool), ("collect", collect),
                     ("route", route), ("synthesize", synthesize), ("finalize", finalize)):
        graph.add_node(name, fn)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "gather")
    graph.add_conditional_edges("gather", after_gather, ["run_tool", "collect", "route"])
    graph.add_edge("run_tool", "collect")
    graph.add_edge("collect", "gather")
    graph.add_conditional_edges("route", after_route, ["synthesize", END])
    graph.add_edge("synthesize", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
