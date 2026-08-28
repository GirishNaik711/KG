from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from observability.telemetry import trace_agent_run


class RecommendationState(TypedDict, total=False):
    request: str
    recommendation: str


def recommend(state: RecommendationState) -> RecommendationState:
    return {"recommendation": "Review unmanaged logistics spend first; the sample has a missing PO quality flag."}


def build_graph():
    graph = StateGraph(RecommendationState)
    graph.add_node("recommend", recommend)
    graph.add_edge(START, "recommend")
    graph.add_edge("recommend", END)
    return graph.compile()


recommendation_graph = build_graph()


@trace_agent_run
def invoke_recommendation(request: str) -> str:
    result = recommendation_graph.invoke({"request": request})
    return result["recommendation"]