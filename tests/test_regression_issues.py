from claimcheck.application.document_processing import process_document_bytes
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from io import BytesIO
from uuid import uuid4
import hashlib

def _pdf_bytes(lines: list[str]) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
    })
    operations = ["BT /F1 12 Tf 50 740 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            operations.append("0 -18 Td")
        operations.append(f"({escaped}) Tj")
    operations.append("ET")
    stream = DecodedStreamObject()
    stream.set_data(" ".join(operations).encode("ascii", errors="backslashreplace"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()

def test_regression_room_rent_percent_paise_conversion():
    # If room rent is 1 percent of 5,00,000, it should be 5,000 (5,00,000 paise)
    pdf_bytes = _pdf_bytes([
        "Sum Insured: 5,00,000",
        "Room Rent Limit: 1 percent of Sum Insured per day"
    ])
    result = process_document_bytes(
        pdf_bytes,
        assigned_role="policy_schedule",
        case_id=uuid4(),
        document_id=uuid4(),
        expected_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        expected_pages=1,
        processing_run_id=uuid4()
    )
    found = False
    for f in result.fields:
        if f.field_path == "policy.room_rent_limit_paise":
            assert f.value == 500000  # 5000 Rs * 100
            found = True
    assert found

def test_regression_settlement_paise_preservation():
    # Final payable with decimal like 86,638.50 should be preserved exactly as paise (8663850)
    pdf_bytes = _pdf_bytes([
        "Net Payable Amount: 86,638.50"
    ])
    result = process_document_bytes(
        pdf_bytes,
        assigned_role="settlement",
        case_id=uuid4(),
        document_id=uuid4(),
        expected_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        expected_pages=1,
        processing_run_id=uuid4()
    )
    found = False
    for f in result.fields:
        if f.field_path == "settlement.final_payable_paise":
            assert f.value == 8663850
            found = True
    assert found
