"""GOLDEN-001: one complete case, represented as structured facts by hand.

This is the milestone fixture. It exists so the trust core can be exercised end to end
— graph, calculator, rules, reconciliation, verdict — with **no model, no OCR and no
retrieval** anywhere in the path. When document readers arrive (steps 12-15 of the
build order) their only job is to reproduce these facts from the four page texts
below; the numbers must not move.

Everything here is SYNTHETIC and is labelled as such: a fictional insurer, a fictional
UIN, a fictional hospital, a fictional patient and a fictional letter. What is *not*
invented is the structure — a room-rent sub-limit, an associated-medical-expense
definition that excludes pharmacy/diagnostics/ICU, a proportionate deduction applied
outside that definition, and a 10% co-payment — because that is the shape of the
question the project exists to answer.

The case is built to contain one instance of each thing the architecture must handle:

  * a non-payable item                    attendant charges (policy annexure)
  * a room-rent restriction               Rs. 5,000/day against Rs. 8,000/day
  * a proportionate adjustment            5/8 of the associated medical expenses
  * a rule-barred scope                   ICU, pharmacy and diagnostics cut anyway
  * a contested question                  the ratio's scope (reading R0 vs R1)
  * an indeterminate fact                 the hospital's billing practice is unevidenced
  * an unexplained deduction              "Other deductions" with no basis stated
  * a procedural defect                   no policy term cited for the disallowance
  * a second computation                  the restoration route, which must agree
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

from ..evidence.spans import EvidenceBuilder, ExtractionMeta, PageIndex
from ..pipeline import StructuredCase
from ..rules.items import NonPayableItem
from ..rules.model import RuleStatus
from ..schema import (
    BillFacts,
    BillLine,
    CanonicalCategory,
    Document,
    DocRole,
    Evidence,
    Fact,
    FactStatus,
    FactStore,
    MappingSource,
    Origin,
    Page,
    PolicyClause,
    PolicyFacts,
    SettlementDeduction,
    SettlementFacts,
    sha256_text,
)

SNAPSHOT_ID = "SNAP-GOLDEN-001-1"
CASE_ID = "GOLDEN-001"
AS_OF = date(2026, 10, 3)
CLAIM_DATE = date(2026, 7, 14)


# ---------------------------------------------------------------------------
# The four page texts. A reader must reproduce the facts below from these strings.
# ---------------------------------------------------------------------------
W_HEAD = """SYNTHETIC GENERAL INSURANCE CO. (ILLUSTRATIVE DOCUMENT)
Shield Health Plan - Policy Wording
"""
W_2_14 = """Clause 2.14 "Admissible claim amount" means the amount of the claim after excluding
non-payable items, applying the room rent restriction and any proportionate
adjustment under Clause 5, and before the co-payment in Clause 3.6."""
W_3_6 = """Clause 3.6 Co-payment: the insured shall bear co-payment at the rate stated in the
Schedule, applied to the admissible claim amount defined in Clause 2.14."""
W_5_1 = """Clause 5.1 Proportionate adjustment: where the insured occupies a room whose tariff
exceeds the eligible room rent, the associated medical expenses shall be reduced in
the same ratio that the eligible room rent bears to the actual room tariff."""
W_5_2 = """Clause 5.2 "Associated medical expenses" means nursing charges, surgeon fees,
anaesthesia charges and operation theatre charges. It does not include pharmacy,
consumables, implants, medical devices, diagnostics or ICU charges."""
W_6_1 = """Clause 6.1 Items not payable: attendant charges and food and refreshment charges are
not payable under this policy."""
WORDING_TEXT = "\n".join([W_HEAD, W_2_14, "", W_3_6, "", W_5_1, "", W_5_2, "", W_6_1, ""])

SCHEDULE_TEXT = """SYNTHETIC GENERAL INSURANCE CO. (ILLUSTRATIVE DOCUMENT)
Shield Health Plan - Policy Schedule
Policy No: SG-LAB-0001          UIN: SYNTH/HLP/0001/V01/26-27
Policy Period: 01-06-2025 to 31-05-2026
Sum Insured: Rs. 5,00,000
Room Rent (per day, single private room): Rs. 5,000
Co-payment: 10% of the admissible claim amount defined in Clause 2.14
"""

BILL_TEXT = """CITY CARE HOSPITAL (ILLUSTRATIVE DOCUMENT)
Bill No: B-2026-0042
Patient: SYNTHETIC PATIENT A      IP No: IP-88123
Admission: 14-07-2026   Discharge: 19-07-2026
Room Category: Deluxe Room (Rs. 8,000 per day)

Particulars                              Rate     Qty      Amount
Deluxe Room Rent                          8,000     5       40,000
Nursing Charges                           2,000     5       10,000
Surgeon Fees                                                 50,000
Anaesthesia Charges                                          15,000
Operation Theatre Charges                                    20,000
ICU Charges                               8,000     1        8,000
Pharmacy and Medicines                                       15,000
Consumables and Dressings                                    15,000
Diagnostics and Investigations                               25,000
Attendant Charges                                             2,000

Total                                                      2,00,000
"""

SETTLEMENT_TEXT = """SYNTHETIC GENERAL INSURANCE CO. (ILLUSTRATIVE DOCUMENT)
CLAIM SETTLEMENT LETTER
Claim No: CLM-2026-7788        Policy No: SG-LAB-0001
Hospitalisation: 14-07-2026 to 19-07-2026
Claimed Amount: Rs. 2,00,000

Deductions
Attendant charges - not payable as per policy                    2,000
Room rent restriction (eligible Rs. 5,000 per day)              15,000
Proportionate deduction on associated medical expenses
  restricted in the ratio of eligible to actual room
  rent (62.5%)                                                  53,625
Co-payment 10%                                                  12,937.50
Other deductions                                                 2,340

Net Payable: Rs. 1,14,097.50
"""


def _doc(role: DocRole, text: str, idx: int) -> Document:
    return Document(
        document_id=f"DOC-{CASE_ID}-{idx}",
        case_id=CASE_ID,
        role=role,
        media_type="application/pdf",
        sha256=sha256_text(text),
        byte_size=len(text.encode()),
        object_key=f"cases/{CASE_ID}/doc{idx}.pdf",
        page_count=1,
        licence="synthetic — no third-party content",
    )


# ---------------------------------------------------------------------------
def build_case() -> StructuredCase:
    schedule = _doc(DocRole.POLICY_SCHEDULE, SCHEDULE_TEXT, 1)
    wording = _doc(DocRole.POLICY_WORDING, WORDING_TEXT, 2)
    bill_doc = _doc(DocRole.BILL, BILL_TEXT, 3)
    letter = _doc(DocRole.SETTLEMENT, SETTLEMENT_TEXT, 4)
    pages = (
        Page(page_id="P1", document_id=schedule.document_id, page_number=1,
             text=SCHEDULE_TEXT, quality_score=0.97),
        Page(page_id="P2", document_id=wording.document_id, page_number=1,
             text=WORDING_TEXT, quality_score=0.95),
        Page(page_id="P3", document_id=bill_doc.document_id, page_number=1,
             text=BILL_TEXT, quality_score=0.93),
        Page(page_id="P4", document_id=letter.document_id, page_number=1,
             text=SETTLEMENT_TEXT, quality_score=0.96),
    )
    index = PageIndex(pages)
    builder = EvidenceBuilder(index, (schedule, wording, bill_doc, letter))
    meta = ExtractionMeta(method="manual_fixture", engine="none", confidence=1.0)

    def span(document: Document, quote: str) -> Evidence:
        return builder.build(document_id=document.document_id, page_number=1,
                             quote=quote, extraction=meta, strict=True)

    # -- policy spans --------------------------------------------------------
    ev_si = span(schedule, "Sum Insured: Rs. 5,00,000")
    ev_room = span(schedule, "Room Rent (per day, single private room): Rs. 5,000")
    ev_copay = span(schedule, "Co-payment: 10% of the admissible claim amount defined in Clause 2.14")
    ev_ame = span(wording, 'Clause 5.2 "Associated medical expenses" means nursing charges, surgeon fees,\nanaesthesia charges and operation theatre charges.')
    ev_np = span(wording, "Clause 6.1 Items not payable: attendant charges and food and refreshment charges are\nnot payable under this policy.")

    # -- bill spans ----------------------------------------------------------
    ev_room_line = span(bill_doc, "Deluxe Room Rent                          8,000     5       40,000")
    ev_nursing_line = span(bill_doc, "Nursing Charges                           2,000     5       10,000")
    ev_surgeon_line = span(bill_doc, "Surgeon Fees                                                 50,000")
    ev_icu_line = span(bill_doc, "ICU Charges                               8,000     1        8,000")
    ev_pharmacy_line = span(bill_doc, "Pharmacy and Medicines                                       15,000")
    ev_diag_line = span(bill_doc, "Diagnostics and Investigations                               25,000")
    ev_attendant_line = span(bill_doc, "Attendant Charges                                             2,000")
    ev_total_line = span(bill_doc, "Total                                                      2,00,000")

    # -- settlement spans ----------------------------------------------------
    ev_s_np = span(letter, "Attendant charges - not payable as per policy                    2,000")
    ev_s_room = span(letter, "Room rent restriction (eligible Rs. 5,000 per day)              15,000")
    ev_s_ratio = span(letter, "  rent (62.5%)                                                  53,625")
    ev_s_copay = span(letter, "Co-payment 10%                                                  12,937.50")
    ev_s_other = span(letter, "Other deductions                                                 2,340")
    ev_s_net = span(letter, "Net Payable: Rs. 1,14,097.50")

    # -- facts ---------------------------------------------------------------
    def money(fid: str, value: int, evidence: Evidence, raw: str) -> Fact:
        return Fact(fact_id=fid, type="loadbearing.money", value=value, unit="inr_paise",
                    origin=Origin.PRINTED, status=FactStatus.READ, confidence=1.0,
                    evidence_id=evidence.evidence_id, raw_text=raw)

    facts = FactStore([
        money("policy.sum_insured", 50_000_000, ev_si, "Rs. 5,00,000"),
        money("policy.room_rent_limit_per_day", 500_000, ev_room, "Rs. 5,000"),
        Fact(fact_id="policy.co_pay_percent", type="loadbearing.ratio", value=Fraction(10),
             unit="percent", origin=Origin.PRINTED, status=FactStatus.READ, confidence=1.0,
             evidence_id=ev_copay.evidence_id, raw_text="10%"),
        Fact(fact_id="policy.ame_definition_present", type="loadbearing.bool", value=True,
             unit="bool", origin=Origin.PRINTED, status=FactStatus.READ, confidence=1.0,
             evidence_id=ev_ame.evidence_id, raw_text='Clause 5.2 "Associated medical expenses"'),
        Fact(fact_id="bill.room_days", type="loadbearing.count", value=5, unit="days",
             origin=Origin.PRINTED, status=FactStatus.READ, confidence=1.0,
             evidence_id=ev_room_line.evidence_id, raw_text="5"),
        money("bill.room_rate_actual_per_day", 800_000, ev_room_line, "8,000"),
        money("bill.attendant_charges", 200_000, ev_attendant_line, "2,000"),
        money("settlement.claimed", 20_000_000, ev_s_net, "Rs. 2,00,000"),
        money("settlement.net_payable", 11_409_750, ev_s_net, "Rs. 1,14,097.50"),
        money("settlement.other_deductions", 234_000, ev_s_other, "2,340"),
        # The hospital's billing practice is not evidenced by anything in the file:
        # it is missing, not false. The rule that turns on it stays INDETERMINATE.
        Fact(fact_id="hospital.follows_differential_billing", type="loadbearing.bool",
             value=None, unit="bool", origin=Origin.PRINTED, status=FactStatus.MISSING,
             confidence=None, evidence_id=None, raw_text=None,
             note="no document in the case record states the hospital's billing practice"),
    ])

    # -- bill ---------------------------------------------------------------
    def line(lid: str, description: str, amount: int, category: CanonicalCategory,
             evidence: Evidence, qty: Fraction | None = None, rate: int | None = None) -> BillLine:
        return BillLine(line_id=lid, raw_description=description, amount=amount,
                        category=category, mapping_source=MappingSource.EXACT,
                        mapping_confidence=1.0, quantity=qty, unit_rate=rate,
                        evidence_id=evidence.evidence_id,
                        raw_amount_text=f"{amount / 100:,.2f}")

    bill = BillFacts(
        bill_id="BILL-" + CASE_ID,
        document_id=bill_doc.document_id,
        lines=(
            line("L1", "Deluxe Room Rent", 4_000_000, CanonicalCategory.ROOM, ev_room_line, Fraction(5), 800_000),
            line("L2", "Nursing Charges", 1_000_000, CanonicalCategory.NURSING, ev_nursing_line, Fraction(5), 200_000),
            line("L3", "Surgeon Fees", 5_000_000, CanonicalCategory.SURGEON, ev_surgeon_line),
            line("L4", "Anaesthesia Charges", 1_500_000, CanonicalCategory.ANAESTHESIA, ev_surgeon_line),
            line("L5", "Operation Theatre Charges", 2_000_000, CanonicalCategory.OT_CHARGES, ev_surgeon_line),
            line("L6", "ICU Charges", 800_000, CanonicalCategory.ICU, ev_icu_line, Fraction(1), 800_000),
            line("L7", "Pharmacy and Medicines", 1_500_000, CanonicalCategory.PHARMACY, ev_pharmacy_line),
            line("L8", "Consumables and Dressings", 1_500_000, CanonicalCategory.CONSUMABLES, ev_pharmacy_line),
            line("L9", "Diagnostics and Investigations", 2_500_000, CanonicalCategory.DIAGNOSTICS, ev_diag_line),
            line("L10", "Attendant Charges", 200_000, CanonicalCategory.MISCELLANEOUS, ev_attendant_line),
        ),
        bill_total=20_000_000,
        admission_date=date(2026, 7, 14),
        discharge_date=date(2026, 7, 19),
        room_days=5,
        room_rate=800_000,
        room_category="Deluxe Room",
        hospital_name="CITY CARE HOSPITAL (ILLUSTRATIVE)",
    )

    # -- policy -------------------------------------------------------------
    clauses = {
        "c2.14": PolicyClause("c2.14", "Clause 2.14 Admissible claim amount", W_2_14,
                              evidence_id=ev_copay.evidence_id, clause_type="definition"),
        "c3.6": PolicyClause("c3.6", "Clause 3.6 Co-payment", W_3_6,
                             evidence_id=ev_copay.evidence_id, clause_type="financial"),
        "c5.1": PolicyClause("c5.1", "Clause 5.1 Proportionate adjustment", W_5_1,
                             evidence_id=ev_np.evidence_id, clause_type="financial"),
        "c5.2": PolicyClause("c5.2", "Clause 5.2 Associated medical expenses", W_5_2,
                             evidence_id=ev_ame.evidence_id, clause_type="definition"),
        "c6.1": PolicyClause("c6.1", "Clause 6.1 Items not payable", W_6_1,
                             evidence_id=ev_np.evidence_id, clause_type="exclusion"),
    }
    non_payable_items = (
        NonPayableItem(
            item_id="NP.ATTENDANT", canonical_name="Attendant Charges",
            aliases=("attendant", "attendant charges", "attendant allowance"),
            stance="NOT_PAYABLE", source="policy_document", source_version="SG-LAB-0001",
            conditions=(), exceptions=("where the policy has no such annexure",),
            status=RuleStatus.VERIFIED, evidence_id=ev_np.evidence_id,
            quote="attendant charges and food and refreshment charges are not payable under this policy",
        ),
        NonPayableItem(
            item_id="NP.FOOD", canonical_name="Food and Refreshment Charges",
            aliases=("food", "refreshment", "diet charges", "food and beverages"),
            stance="NOT_PAYABLE", source="policy_document", source_version="SG-LAB-0001",
            status=RuleStatus.VERIFIED, evidence_id=ev_np.evidence_id,
            quote="attendant charges and food and refreshment charges are not payable under this policy",
        ),
    )
    policy = PolicyFacts(
        policy_id="POL-SG-LAB-0001",
        insurer="SYNTHETIC GENERAL INSURANCE CO. (illustrative)",
        product_name="Shield Health Plan (synthetic)",
        policy_era="post-2021",
        uin="SYNTH/HLP/0001/V01/26-27",
        documents={"policy_schedule": schedule.document_id, "policy_wording": wording.document_id},
        inception_date=date(2025, 6, 1),
        renewal_date=date(2025, 6, 1),
        claim_date=CLAIM_DATE,
        sum_insured=facts.get("policy.sum_insured"),
        room_rent_limit=facts.get("policy.room_rent_limit_per_day"),
        icu_limit=None,
        co_pay_percent=facts.get("policy.co_pay_percent"),
        co_pay_base_stated=True,      # Clause 3.6 + Clause 2.14 state the base and the order
        co_pay_applies_to=(),
        deductible=None,
        sub_limits=(),
        waiting_periods=(),
        ame_definition_present=True,
        ame_heads=(CanonicalCategory.NURSING, CanonicalCategory.SURGEON,
                   CanonicalCategory.ANAESTHESIA, CanonicalCategory.OT_CHARGES),
        ame_definition_evidence=ev_ame.evidence_id,
        non_payable_items=non_payable_items,
        calc_method_note=("Clause 2.14: the admissible claim amount is taken after non-payable "
                          "items, the room rent restriction and any proportionate adjustment "
                          "under Clause 5, and before the co-payment in Clause 3.6"),
        stated_order=("NON_PAYABLE_DEDUCTION", "ROOM_RENT_LIMIT",
                      "PROPORTIONATE_ADJUSTMENT", "COPAY", "SUM_INSURED_CEILING"),
        clauses=clauses,
        moratorium_months=None,
    )

    # -- settlement ---------------------------------------------------------
    settlement = SettlementFacts(
        settlement_id="STL-CLM-2026-7788",
        document_id=letter.document_id,
        claimed_amount=20_000_000,
        deductions=(
            SettlementDeduction(head="attendant_charges",
                                raw_head_text="Attendant charges - not payable as per policy",
                                amount=200_000, evidence_id=ev_s_np.evidence_id),
            SettlementDeduction(head="room_rent_restriction",
                                raw_head_text="Room rent restriction (eligible Rs. 5,000 per day)",
                                amount=1_500_000, evidence_id=ev_s_room.evidence_id),
            SettlementDeduction(
                head="proportionate_deduction",
                raw_head_text=("Proportionate deduction on associated medical expenses restricted "
                               "in the ratio of eligible to actual room rent (62.5%)"),
                amount=5_362_500, ratio_stated=Fraction(5, 8),
                evidence_id=ev_s_ratio.evidence_id,
                reason_text="proportionate deduction on associated medical expenses"),
            SettlementDeduction(head="co_payment", raw_head_text="Co-payment 10%",
                                amount=1_293_750, evidence_id=ev_s_copay.evidence_id),
            SettlementDeduction(head="other_deductions", raw_head_text="Other deductions",
                                amount=234_000, evidence_id=ev_s_other.evidence_id),
        ),
        final_payable=11_409_750,
        decision_date=date(2026, 8, 28),
        intimation_date=date(2026, 7, 20),
        last_document_date=date(2026, 7, 31),
        payment_date=date(2026, 9, 2),
        repudiated=False,
        partial_disallowance=True,
        cites_specific_policy_terms=False,      # the letter names no clause
        demands_documents_from_policyholder=False,
        includes_ombudsman_details=False,
        cis_provided=True,
    )

    evidence = {e.evidence_id: e for e in (
        ev_si, ev_room, ev_copay, ev_ame, ev_np, ev_room_line, ev_nursing_line,
        ev_surgeon_line, ev_icu_line, ev_pharmacy_line, ev_diag_line, ev_attendant_line,
        ev_total_line, ev_s_np, ev_s_room, ev_s_ratio, ev_s_copay, ev_s_other, ev_s_net,
    )}

    return StructuredCase(
        case_id=CASE_ID,
        corpus_snapshot_id=SNAPSHOT_ID,
        origin="synthetic",
        policy=policy,
        bill=bill,
        settlement=settlement,
        facts=facts,
        pages=index,
        documents={
            "policy_schedule": schedule.document_id,
            "policy_wording": wording.document_id,
            "bill": bill_doc.document_id,
            "settlement": letter.document_id,
        },
        evidence=evidence,
        candidate_head_sets={
            "policy_definition": ["nursing", "surgeon", "anaesthesia", "ot_charges"],
            "policy_definition_plus_barred": [
                "nursing", "surgeon", "anaesthesia", "ot_charges", "icu", "pharmacy",
                "diagnostics",
            ],
            "all_heads_except_room": [
                "nursing", "surgeon", "anaesthesia", "ot_charges", "icu", "pharmacy",
                "consumables", "diagnostics",
            ],
        },
        as_of=AS_OF,
        history_notes=(
            "synthetic case: no real insurer, policy, hospital or patient is represented",
            "the four page texts are the readers' target; the facts above are what a reader "
            "must produce from them",
        ),
    )
