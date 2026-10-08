"""Order determination and reading enumeration (Phase-2 §11.5, the three cases).

Case A — the policy states the order  -> one graph, one number.
Case B — the order is implied by a cross-reference -> the graph records the
         inference and shows it to the user.
Case C — the order is ambiguous -> **every defensible reading is executed**, and:

    * if the readings differ materially  -> UNDETERMINED, both readings shown;
    * if the difference is immaterial    -> the reading that claims **less** is used,
      and the assumption is labelled.

The asymmetry is deliberate: where the difference is too small to be worth arguing
about, the product never takes the reading that inflates the claim.
"""

from __future__ import annotations

from typing import Sequence

from ..graph.model import (
    CalcGraph,
    OrderAmbiguity,
    OrderReading,
    OrderProvenance,
)
from ..schema import ReadingId
from .executor import CaseContext, execute_graph


def enumerate_readings(graph: CalcGraph, ctx: CaseContext) -> tuple[OrderReading, ...]:
    """Execute every named reading of the graph and collect the payables."""
    readings: list[OrderReading] = []
    for reading_id, variant in graph.variants.items():
        result = execute_graph(graph, ctx, reading_id=reading_id)
        readings.append(
            OrderReading(
                reading_id=reading_id,
                label=variant.label,
                payable_paise=result.payable_paise,
                sequence=tuple(o.node_id for o in result.outcomes),
                assumptions=result.assumptions,
            )
        )
    if not readings:
        result = execute_graph(graph, ctx, reading_id=ReadingId.PRIMARY.value)
        readings.append(
            OrderReading(
                reading_id=ReadingId.PRIMARY.value,
                label="primary",
                payable_paise=result.payable_paise,
                sequence=tuple(o.node_id for o in result.outcomes),
            )
        )
    return tuple(readings)


def analyse_order(graph: CalcGraph, ctx: CaseContext) -> OrderAmbiguity:
    """Classify the order (A/B/C) and judge the materiality of the enumerated readings."""
    readings = enumerate_readings(graph, ctx)
    payables = [r.payable_paise for r in readings]
    spread = (max(payables) - min(payables)) if payables else 0
    threshold = graph.materiality_paise
    readings_material = len(readings) > 1 and spread > threshold

    if graph.order_provenance is OrderProvenance.STATED:
        case: str = "A"
        reason = "the policy states the order in which the steps apply"
    elif graph.order_provenance in (OrderProvenance.IMPLIED, OrderProvenance.INFERRED):
        case = "B"
        reason = "the order is inferred from the policy's cross-references and is shown as an assumption"
    elif graph.order_provenance is OrderProvenance.DEMO_ONLY:
        case = "C"
        reason = ("the ordering is a demo-only fixture ordering and may not be used in a "
                  "production case")
    else:
        case = "C"
        reason = ("the policy does not state the order of the steps and no reading can be "
                  "enumerated from the documents; the affected amount is UNDETERMINED")

    if case == "C":
        return OrderAmbiguity(
            case="C", readings=readings, spread_paise=spread,
            material=spread > threshold, threshold_paise=threshold,
            selected_reading=None, reason=reason, readings_material=readings_material,
        )

    # The order is known: the policy-supported reading is the one used. When a
    # contested question changes the amount, the difference is reported and the
    # *lowest* reading bounds what may be claimed.
    primary = next((r for r in readings if r.reading_id == ReadingId.PRIMARY.value), readings[0])
    note = reason
    if readings_material:
        from .money import format_inr
        note += (f"; the readings of the contested question differ by {format_inr(spread)}, "
                 f"above the materiality threshold of {format_inr(threshold)} - every reading "
                 f"is shown, and the difference claimed is the smallest of them")
    return OrderAmbiguity(
        case=case, readings=readings, spread_paise=spread, material=False,
        threshold_paise=threshold, selected_reading=primary.reading_id, reason=note,
        readings_material=readings_material,
    )


def readings_summary(ambiguity: OrderAmbiguity) -> str:
    lines = [f"order case {ambiguity.case}: {ambiguity.reason}"]
    for r in ambiguity.readings:
        lines.append(f"  - {r.reading_id} ({r.label}): payable {r.payable_paise} paise")
    return "\n".join(lines)
