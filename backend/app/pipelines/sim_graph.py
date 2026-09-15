"""LangGraph-compatible SIM graph with a deterministic evidence-gated fallback.

The graph never emits a duplicate-program verdict. Missing LangGraph or LLM
is a normal offline path, not an implicit success.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from app.models.outcomes import ErrorCode, OutcomeCode
from app.models.sim import (
    SIM_AXES,
    SIM_CORE_AXES,
    SimAxis,
    SimAxisComparison,
    SimAxisStatus,
    SimCandidate,
    SimComparison,
    SimEvidence,
    SimGraphState,
    SimReviewGrade,
)
from app.pipelines.availability import module_available

END = "__end__"
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")

NodeFn = Callable[[SimGraphState], SimGraphState]


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall((text or "").lower()))


def _overlap(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def retrieve(state: SimGraphState) -> SimGraphState:
    state.retrieved = tuple(state.candidates)
    state.warnings.append("retrieval is pass-through; no vector index is queried")
    return state


def prune(state: SimGraphState) -> SimGraphState:
    kept: list[SimCandidate] = []
    for candidate in state.retrieved:
        exclusive = tuple(axis for axis in candidate.exclusive_nonoverlap_axes if axis in SIM_CORE_AXES)
        if len(exclusive) >= 2:
            state.warnings.append(
                f"{candidate.candidate_id} pruned: exclusive non-overlap on {','.join(a.value for a in exclusive)}"
            )
            continue
        kept.append(candidate)
    state.pruned = tuple(kept)
    return state


def compare(state: SimGraphState) -> SimGraphState:
    request_by_axis = _index_evidence(state.request_evidence, side="request")
    comparisons: list[SimComparison] = []
    for candidate in state.pruned:
        candidate_by_axis = _index_evidence(candidate.evidence, side="candidate")
        axes: list[SimAxisComparison] = []
        assessable = 0
        similar_or_partial = 0
        summaries: list[str] = []
        for axis in SIM_AXES:
            req = request_by_axis.get(axis, ())
            cand = candidate_by_axis.get(axis, ())
            if not req or not cand:
                axes.append(
                    SimAxisComparison(
                        axis=axis,
                        status=SimAxisStatus.INSUFFICIENT,
                        reason_code=ErrorCode.EVIDENCE_MISSING,
                        request_evidence_refs=tuple(item.evidence_ref for item in req),
                        candidate_evidence_refs=tuple(item.evidence_ref for item in cand),
                        summary="evidence missing on at least one side; axis is not scored",
                    )
                )
                continue
            score = _overlap(
                " ".join(item.excerpt for item in req),
                " ".join(item.excerpt for item in cand),
            )
            if score >= 0.5:
                status = SimAxisStatus.SIMILAR
                reason = None
            elif score >= 0.15:
                status = SimAxisStatus.PARTIAL
                reason = "PARTIAL_OVERLAP"
            else:
                status = SimAxisStatus.DIFFERENT
                reason = "LOW_OVERLAP"
            assessable += 1
            if status in {SimAxisStatus.SIMILAR, SimAxisStatus.PARTIAL}:
                similar_or_partial += 1
            summary = f"{axis.value} {status.value} overlap={score:.2f}"
            summaries.append(summary)
            axes.append(
                SimAxisComparison(
                    axis=axis,
                    status=status,
                    reason_code=reason,
                    request_evidence_refs=tuple(item.evidence_ref for item in req),
                    candidate_evidence_refs=tuple(item.evidence_ref for item in cand),
                    summary=summary,
                )
            )
        core_assessable = sum(
            1
            for item in axes
            if item.axis in SIM_CORE_AXES and item.status is not SimAxisStatus.INSUFFICIENT
        )
        core_focus = sum(
            1
            for item in axes
            if item.axis in SIM_CORE_AXES
            and item.status in {SimAxisStatus.SIMILAR, SimAxisStatus.PARTIAL}
        )
        if core_assessable == 0:
            grade = SimReviewGrade.ON_HOLD
        elif core_focus >= 2:
            grade = SimReviewGrade.FOCUS_REVIEW
        elif core_focus == 1:
            grade = SimReviewGrade.GENERAL_REVIEW
        else:
            grade = SimReviewGrade.LOW_PRIORITY
        comparisons.append(
            SimComparison(
                candidate_id=candidate.candidate_id,
                title=candidate.title,
                axes=tuple(axes),
                review_grade=grade,
                comparison_summary="; ".join(summaries) or "no assessable SIM axes",
                assessable_axis_count=assessable,
            )
        )
    state.comparisons = tuple(comparisons)
    return state


def rank(state: SimGraphState) -> SimGraphState:
    order = {
        SimReviewGrade.FOCUS_REVIEW: 0,
        SimReviewGrade.GENERAL_REVIEW: 1,
        SimReviewGrade.LOW_PRIORITY: 2,
        SimReviewGrade.ON_HOLD: 3,
    }
    state.ranked = tuple(
        sorted(
            state.comparisons,
            key=lambda item: (order[item.review_grade], item.candidate_id),
        )
    )
    return state


def _index_evidence(items: tuple[SimEvidence, ...], *, side: str) -> dict[SimAxis, tuple[SimEvidence, ...]]:
    grouped: dict[SimAxis, list[SimEvidence]] = {axis: [] for axis in SIM_AXES}
    for item in items:
        if item.side != side:
            continue
        grouped[item.axis].append(item)
    return {axis: tuple(values) for axis, values in grouped.items()}


@dataclass
class CompiledSimGraph:
    nodes: dict[str, NodeFn]
    edges: dict[str, str]
    entry: str
    backend: str

    def invoke(self, state: SimGraphState | Mapping[str, Any]) -> SimGraphState:
        current = state if isinstance(state, SimGraphState) else state_from_mapping(state)
        current.graph_backend = self.backend
        current.langgraph_used = self.backend == "langgraph"
        name = self.entry
        seen = 0
        while name and name != END:
            if name not in self.nodes:
                raise KeyError(f"unknown SIM node: {name}")
            current = self.nodes[name](current)
            name = self.edges.get(name, END)
            seen += 1
            if seen > 16:
                raise RuntimeError("SIM graph exceeded fallback step bound")
        for comparison in current.ranked:
            payload = asdict(comparison)
            if "duplicate" in payload or payload.get("duplicate_verdict"):
                raise RuntimeError("SIM graph must not emit a duplicate verdict")
        return current


class FallbackStateGraph:
    """Minimal StateGraph stand-in used when langgraph is not installed."""

    def __init__(self, _state_type: type[Any] | None = None) -> None:
        self._nodes: dict[str, NodeFn] = {}
        self._edges: dict[str, str] = {}
        self._entry: str | None = None

    def add_node(self, name: str, fn: NodeFn) -> None:
        self._nodes[name] = fn

    def add_edge(self, start: str, end: str) -> None:
        self._edges[start] = end

    def set_entry_point(self, name: str) -> None:
        self._entry = name

    def compile(self) -> CompiledSimGraph:
        if not self._entry:
            raise ValueError("SIM graph entry point is not set")
        return CompiledSimGraph(
            nodes=dict(self._nodes),
            edges=dict(self._edges),
            entry=self._entry,
            backend="fallback",
        )


def build_sim_graph(*, prefer_langgraph: bool = True) -> CompiledSimGraph:
    if prefer_langgraph and module_available("langgraph"):
        try:
            return _build_langgraph()
        except Exception:
            compiled = _build_fallback()
            compiled.backend = "fallback"
            return compiled
    return _build_fallback()


def _wire(graph: Any) -> Any:
    graph.add_node("retrieve", retrieve)
    graph.add_node("prune", prune)
    graph.add_node("compare", compare)
    graph.add_node("rank", rank)
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "prune")
    graph.add_edge("prune", "compare")
    graph.add_edge("compare", "rank")
    graph.add_edge("rank", END)
    return graph


def _build_fallback() -> CompiledSimGraph:
    return _wire(FallbackStateGraph(SimGraphState)).compile()


def _build_langgraph() -> CompiledSimGraph:
    # LangGraph, when present, is adapted to the same invoke() contract.
    from langgraph.graph import END as LG_END
    from langgraph.graph import StateGraph

    graph = StateGraph(dict)

    def _wrap(fn: NodeFn) -> Callable[[dict[str, Any]], dict[str, Any]]:
        def inner(payload: dict[str, Any]) -> dict[str, Any]:
            result = fn(state_from_mapping(payload))
            return state_to_mapping(result)

        return inner

    graph.add_node("retrieve", _wrap(retrieve))
    graph.add_node("prune", _wrap(prune))
    graph.add_node("compare", _wrap(compare))
    graph.add_node("rank", _wrap(rank))
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "prune")
    graph.add_edge("prune", "compare")
    graph.add_edge("compare", "rank")
    graph.add_edge("rank", LG_END)
    compiled = graph.compile()

    class _LangGraphAdapter(CompiledSimGraph):
        def invoke(self, state: SimGraphState | Mapping[str, Any]) -> SimGraphState:
            payload = state_to_mapping(state if isinstance(state, SimGraphState) else state_from_mapping(state))
            payload["graph_backend"] = "langgraph"
            payload["langgraph_used"] = True
            raw = compiled.invoke(payload)
            result = state_from_mapping(raw)
            result.graph_backend = "langgraph"
            result.langgraph_used = True
            return result

    return _LangGraphAdapter(nodes={}, edges={}, entry="retrieve", backend="langgraph")


def state_from_mapping(payload: Mapping[str, Any]) -> SimGraphState:
    def evidence(items: Any) -> tuple[SimEvidence, ...]:
        result: list[SimEvidence] = []
        for item in items or ():
            if isinstance(item, SimEvidence):
                result.append(item)
                continue
            result.append(
                SimEvidence(
                    side=item["side"],
                    axis=SimAxis(item["axis"]),
                    excerpt=item["excerpt"],
                    evidence_ref=item["evidence_ref"],
                )
            )
        return tuple(result)

    def candidates(items: Any) -> tuple[SimCandidate, ...]:
        result: list[SimCandidate] = []
        for item in items or ():
            if isinstance(item, SimCandidate):
                result.append(item)
                continue
            result.append(
                SimCandidate(
                    candidate_id=item["candidate_id"],
                    title=item.get("title", ""),
                    evidence=evidence(item.get("evidence", ())),
                    exclusive_nonoverlap_axes=tuple(
                        SimAxis(axis) for axis in item.get("exclusive_nonoverlap_axes", ())
                    ),
                )
            )
        return tuple(result)

    def axis_comparisons(items: Any) -> tuple[SimAxisComparison, ...]:
        result: list[SimAxisComparison] = []
        for item in items or ():
            if isinstance(item, SimAxisComparison):
                result.append(item)
                continue
            result.append(
                SimAxisComparison(
                    axis=SimAxis(item["axis"]),
                    status=SimAxisStatus(item["status"]),
                    reason_code=item.get("reason_code"),
                    request_evidence_refs=tuple(item.get("request_evidence_refs", ())),
                    candidate_evidence_refs=tuple(item.get("candidate_evidence_refs", ())),
                    summary=item.get("summary", ""),
                )
            )
        return tuple(result)

    def comparisons(items: Any) -> tuple[SimComparison, ...]:
        result: list[SimComparison] = []
        for item in items or ():
            if isinstance(item, SimComparison):
                result.append(item)
                continue
            result.append(
                SimComparison(
                    candidate_id=item["candidate_id"],
                    title=item.get("title", ""),
                    axes=axis_comparisons(item.get("axes", ())),
                    review_grade=SimReviewGrade(item["review_grade"]),
                    comparison_summary=item.get("comparison_summary", ""),
                    assessable_axis_count=int(item.get("assessable_axis_count", 0)),
                    pruned=bool(item.get("pruned", False)),
                )
            )
        return tuple(result)

    return SimGraphState(
        request_evidence=evidence(payload.get("request_evidence", ())),
        candidates=candidates(payload.get("candidates", ())),
        retrieved=candidates(payload.get("retrieved", ())),
        pruned=candidates(payload.get("pruned", ())),
        comparisons=comparisons(payload.get("comparisons", ())),
        ranked=comparisons(payload.get("ranked", ())),
        warnings=list(payload.get("warnings", []) or []),
        graph_backend=str(payload.get("graph_backend", "fallback")),
        langgraph_used=bool(payload.get("langgraph_used", False)),
    )


def state_to_mapping(state: SimGraphState) -> dict[str, Any]:
    return {
        "request_evidence": [asdict(item) for item in state.request_evidence],
        "candidates": [
            {
                "candidate_id": item.candidate_id,
                "title": item.title,
                "evidence": [asdict(ev) for ev in item.evidence],
                "exclusive_nonoverlap_axes": [axis.value for axis in item.exclusive_nonoverlap_axes],
            }
            for item in state.candidates
        ],
        "retrieved": [
            {
                "candidate_id": item.candidate_id,
                "title": item.title,
                "evidence": [asdict(ev) for ev in item.evidence],
                "exclusive_nonoverlap_axes": [axis.value for axis in item.exclusive_nonoverlap_axes],
            }
            for item in state.retrieved
        ],
        "pruned": [
            {
                "candidate_id": item.candidate_id,
                "title": item.title,
                "evidence": [asdict(ev) for ev in item.evidence],
                "exclusive_nonoverlap_axes": [axis.value for axis in item.exclusive_nonoverlap_axes],
            }
            for item in state.pruned
        ],
        "comparisons": [asdict(item) for item in state.comparisons],
        "ranked": [asdict(item) for item in state.ranked],
        "warnings": list(state.warnings),
        "graph_backend": state.graph_backend,
        "langgraph_used": state.langgraph_used,
    }


def sim_outcome(state: SimGraphState) -> OutcomeCode:
    if not state.ranked:
        return OutcomeCode.NEEDS_REVIEW
    if all(item.assessable_axis_count == 0 for item in state.ranked):
        return OutcomeCode.NEEDS_REVIEW
    return OutcomeCode.SUCCEEDED
