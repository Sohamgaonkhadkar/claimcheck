"""The calculation-graph data model (Phase-2 §11.2).

A node is a *typed* step: what it operates on (``input_base``), with what parameters
(``params``), under what condition (``applicability``), and where each of those came
from (``source`` + ``evidence``). Edges express dependency, sequence and the
alternative/compensating relations that make honest net claims possible.

Nothing here computes. Nothing here imports a model. This module is pure structure,
so a graph can be diffed, stored, reviewed by a human and re-executed years later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Any, Literal, Mapping, Sequence

from ..schema import CanonicalCategory, Money, OrderProvenance, ReadingId, Ratio

# ---------------------------------------------------------------------------
# Step types (closed set; extensible only by review, Phase-2 §11.3)
# ---------------------------------------------------------------------------
class StepType(str, Enum):
    NON_PAYABLE_DEDUCTION = "NON_PAYABLE_DEDUCTION"
    ROOM_RENT_LIMIT = "ROOM_RENT_LIMIT"
    ICU_LIMIT = "ICU_LIMIT"
    PROPORTIONATE_ADJUSTMENT = "PROPORTIONATE_ADJUSTMENT"
    PROPORTIONATE_CLAWBACK = "PROPORTIONATE_CLAWBACK"
    RECOMPUTE_DOWNSTREAM = "RECOMPUTE_DOWNSTREAM"
    SUBLIMIT_CAP = "SUBLIMIT_CAP"
    PACKAGE_CAP = "PACKAGE_CAP"
    DEDUCTIBLE = "DEDUCTIBLE"
    COPAY = "COPAY"
    SUM_INSURED_CEILING = "SUM_INSURED_CEILING"


# ---------------------------------------------------------------------------
# Bases
# ---------------------------------------------------------------------------
BaseKind = Literal[
    "gross_bill",
    "head_set",
    "room_charge_actual",
    "icu_charge_actual",
    "sum_insured",
    "claimed",
    "running",
    "node_output",
    "node_delta",
    "running_after",
]


@dataclass(frozen=True)
class BaseRef:
    """What a step operates on. Resolution is recorded, never assumed."""

    kind: BaseKind
    head_set: tuple[CanonicalCategory, ...] = ()
    node_id: str | None = None
    stated_in: str | None = None  # evidence_id of the wording that establishes the base
    inference_note: str | None = None

    @property
    def is_stated(self) -> bool:
        return self.stated_in is not None and self.inference_note is None

    def describe(self) -> str:
        if self.kind == "head_set":
            return "head_set{" + ", ".join(c.value for c in self.head_set) + "}"
        if self.kind == "node_output":
            return f"node_output({self.node_id})"
        if self.kind == "node_delta":
            return f"node_delta({self.node_id})"
        if self.kind == "running_after":
            return f"running_after({self.node_id})"
        return self.kind


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
ParamSource = Literal[
    "policy_document",   # printed in the policy wording or schedule (with evidence)
    "rule",              # taken from a rule version
    "user",              # asserted by the user
    "reading",           # set by a named reading/variant
    "derived",           # computed by the assembler from other stated values
    "settlement_letter", # FORBIDDEN as a calculation input (anti-circular-reasoning)
]


@dataclass(frozen=True)
class Param:
    name: str
    value: Any  # Money (int) | Fraction | str | list
    unit: str = ""
    source: ParamSource = "policy_document"
    evidence_ids: tuple[str, ...] = ()
    inference_note: str | None = None
    note: str | None = None

    @property
    def is_trusted_input(self) -> bool:
        """A calculation input may never be sourced from the settlement letter.

        Using the insurer's own applied ratio as the recomputation input would make
        the check circular (Phase-2 §16.3).
        """
        return self.source != "settlement_letter"


@dataclass(frozen=True)
class CalcNode:
    node_id: str
    step_type: StepType
    label: str
    input_base: BaseRef
    operation: str
    params: tuple[Param, ...] = ()
    applicability: Mapping[str, Any] | None = None
    order_provenance: OrderProvenance = OrderProvenance.STATED
    position: int | None = None
    contested: bool = False
    reading_id: str = ReadingId.PRIMARY.value
    produced_by: str = "assembler"
    evidence_ids: tuple[str, ...] = ()
    notes: str | None = None
    clause_ref: str | None = None

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def param_value(self, name: str) -> Any | None:
        p = self.param(name)
        return p.value if p else None

    def describe(self) -> str:
        return f"{self.node_id} [{self.step_type.value}] base={self.input_base.describe()} op={self.operation}"


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------
EdgeKind = Literal["feeds", "precedes", "conditional_on", "alternative_of", "compensates"]


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: EdgeKind
    note: str | None = None


# ---------------------------------------------------------------------------
# Variants (readings)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GraphVariant:
    """A named reading of the same policy (a defensible alternative interpretation)."""

    reading_id: str
    label: str
    description: str
    param_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    authoritative: bool = False


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
@dataclass
class CalcGraph:
    graph_id: str
    policy_id: str
    nodes: list[CalcNode] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    variants: dict[str, GraphVariant] = field(default_factory=dict)
    order_provenance: OrderProvenance = OrderProvenance.STATED
    order_sensitivity: bool = False
    corpus_snapshot_id: str | None = None
    rulepack_version: str | None = None
    materiality_paise: int = 100_000  # ₹1,000 default (architecture §14.8)
    notes: list[str] = field(default_factory=list)

    # -- construction helpers -------------------------------------------------
    def add_node(self, node: CalcNode) -> CalcNode:
        self.nodes.append(node)
        return node

    def add_edge(self, src: str, dst: str, kind: EdgeKind = "feeds", note: str | None = None) -> None:
        self.edges.append(Edge(src, dst, kind, note))

    def node(self, node_id: str) -> CalcNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def primary_nodes(self) -> tuple[CalcNode, ...]:
        return tuple(n for n in self.nodes if n.reading_id == ReadingId.PRIMARY.value)

    def edges_from(self, node_id: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.src == node_id)

    # -- validation ----------------------------------------------------------
    def validate(self) -> tuple[str, ...]:
        """Structural validation before execution. Returns problems (empty == valid)."""
        problems: list[str] = []
        ids = [n.node_id for n in self.nodes]
        if len(ids) != len(set(ids)):
            problems.append("duplicate node ids")
        id_set = set(ids)
        for e in self.edges:
            if e.src not in id_set:
                problems.append(f"edge source {e.src} not in graph")
            if e.dst not in id_set:
                problems.append(f"edge destination {e.dst} not in graph")
        for n in self.nodes:
            if n.input_base.kind == "node_output" and n.input_base.node_id not in id_set:
                problems.append(f"{n.node_id}: base references missing node {n.input_base.node_id}")
            for p in n.params:
                if not p.is_trusted_input:
                    problems.append(
                        f"{n.node_id}.{p.name}: parameter sourced from the settlement letter "
                        f"(circular reasoning forbidden)"
                    )
                # if p.source == "policy_document" and not p.evidence_ids:
                #     problems.append(
                #         f"{n.node_id}.{p.name}: policy-sourced parameter has no evidence"
                #     )
            if n.operation not in _ALLOWED_OPS:
                problems.append(f"{n.node_id}: unsupported operation {n.operation!r}")
        problems += self._cycle_problems()
        return tuple(problems)

    def _cycle_problems(self) -> list[str]:
        deps: dict[str, set[str]] = {n.node_id: set() for n in self.nodes}
        for n in self.nodes:
            if n.input_base.kind == "node_output" and n.input_base.node_id:
                deps[n.node_id].add(n.input_base.node_id)
        for e in self.edges:
            if e.kind in ("feeds", "precedes", "conditional_on"):
                deps.setdefault(e.dst, set()).add(e.src)
        # compensating edges are allowed to form a bounded fixed point, not a cycle here
        state: dict[str, int] = {}
        problems: list[str] = []

        def visit(node: str, stack: list[str]) -> None:
            if state.get(node) == 1:
                problems.append("cycle detected: " + " -> ".join(stack + [node]))
                return
            if state.get(node) == 2:
                return
            state[node] = 1
            for dep in deps.get(node, ()):
                visit(dep, stack + [node])
            state[node] = 2

        for node in deps:
            visit(node, [])
        return problems

    def ordered_nodes(self, reading_id: str = ReadingId.PRIMARY.value) -> list[CalcNode]:
        """Topological order, stable by (position, insertion)."""
        chosen = [n for n in self.nodes if n.reading_id == reading_id] or list(self.nodes)
        by_id = {n.node_id: n for n in chosen}
        deps: dict[str, set[str]] = {n.node_id: set() for n in chosen}
        for n in chosen:
            if n.input_base.kind == "node_output" and n.input_base.node_id in by_id:
                deps[n.node_id].add(n.input_base.node_id)  # type: ignore[arg-type]
        for e in self.edges:
            if e.kind in ("feeds", "precedes", "conditional_on") and e.dst in by_id and e.src in by_id:
                deps[e.dst].add(e.src)

        order: list[CalcNode] = []
        done: set[str] = set()
        while len(order) < len(chosen):
            ready = [n for n in chosen if n.node_id not in done
                     and deps[n.node_id] <= done]
            if not ready:
                # A cycle: fall back to insertion order for the remainder (validated earlier).
                ready = [n for n in chosen if n.node_id not in done]
            ready.sort(key=lambda n: (n.position if n.position is not None else 10_000,
                                      chosen.index(n)))
            pick = ready[0]
            order.append(pick)
            done.add(pick.node_id)
        return order

    def variant(self, reading_id: str) -> GraphVariant | None:
        return self.variants.get(reading_id)

    def effective_params(self, node: CalcNode, reading_id: str) -> dict[str, Any]:
        variant = self.variants.get(reading_id)
        values = {p.name: p.value for p in node.params}
        if variant and node.node_id in variant.param_overrides:
            values.update(variant.param_overrides[node.node_id])
        return values


_ALLOWED_OPS = frozenset(
    {"identity", "subtract", "cap", "floor", "multiply_ratio", "percentage",
     "apportion", "threshold", "ceiling"}
)


# ---------------------------------------------------------------------------
# Result types used by the order/ambiguity analysis
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderReading:
    reading_id: str
    label: str
    payable_paise: int
    sequence: tuple[str, ...]
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrderAmbiguity:
    """Two different questions, kept apart on purpose.

    ``case`` is about the **order** of steps (A stated / B implied / C unresolved).
    ``material`` is about the **readings** of a contested question (scope, base) that
    change the amount even when the order is known.

    A stated order with a materially contested scope must NOT be reported as an order
    ambiguity — that would mislabel the reason. It is reported as a reading spread,
    and the amount shown is the one that holds under every reading.
    """

    case: Literal["A", "B", "C"]
    readings: tuple[OrderReading, ...]
    spread_paise: int
    material: bool
    threshold_paise: int
    selected_reading: str | None
    reason: str
    readings_material: bool = False

    @property
    def is_undetermined(self) -> bool:
        """Only an unresolved ORDER makes the head undecidable at the gate level."""
        return self.case == "C" and self.material

    @property
    def narrowed_amount_note(self) -> str | None:
        if not self.readings_material:
            return None
        lo = min(r.payable_paise for r in self.readings)
        hi = max(r.payable_paise for r in self.readings)
        return (f"readings of a contested question differ by {self.spread_paise} paise "
                f"({lo} to {hi}); the amount shown is the one that holds in every reading")
