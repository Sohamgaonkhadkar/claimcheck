

def test_a_currency_amount_is_not_a_document_number() -> None:
    """'Rs 1000000' is money. Reporting it as a document number is a leak on every bill."""
    from claimcheck.ingest.sanitize import find_identifiers

    assert not find_identifiers("Sum insured: Rs 1000000. Policy period: 12 months",
                                include_field_hints=False)
    assert not find_identifiers("Total payable Rs. 250000", include_field_hints=False)
    assert not find_identifiers("Charges of INR 1750000 apply", include_field_hints=False)


def test_a_real_document_number_is_still_an_identifier() -> None:
    from claimcheck.ingest.sanitize import find_identifiers

    kinds = [k for k, _s, _e in find_identifiers("Bill No INT2043376 dated 4/5/2024",
                                                 include_field_hints=False)]
    assert "DOCUMENT_NUMBER" in kinds
    kinds = [k for k, _s, _e in find_identifiers("Reference MPF2521251 in the record",
                                                 include_field_hints=False)]
    assert "DOCUMENT_NUMBER" in kinds


def test_a_plain_english_word_before_a_number_is_not_a_document_number() -> None:
    """'limited to 100000' is a limit, not an identifier. Two-letter words are not prefixes."""
    from claimcheck.ingest.sanitize import find_identifiers

    for text in ("Maternity expenses are limited to 100000 per confinement",
                 "the deductible of 500000 applies",
                 "cover is 250000 for the first year"):
        assert not find_identifiers(text, include_field_hints=False), text


def test_a_lowercase_system_identifier_is_still_an_identifier() -> None:
    from claimcheck.ingest.sanitize import find_identifiers

    kinds = [k for k, _s, _e in find_identifiers("record int2043376 in the system",
                                                 include_field_hints=False)]
    assert "DOCUMENT_NUMBER" in kinds
