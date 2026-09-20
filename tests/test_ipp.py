"""IPP-Kodierung/-Dekodierung und Hilfsfunktionen."""

from __future__ import annotations

import pytest

from custom_components.ipp_print import ipp
from custom_components.ipp_print.const import VERSION


def test_encode_attribute_golden_bytes() -> None:
    # tag(1) + Namenslänge(2) + Name + Wertlänge(2) + Wert
    assert ipp.encode_attribute(ipp.CHARSET, "attributes-charset", "utf-8") == (
        b"\x47\x00\x12attributes-charset\x00\x05utf-8"
    )
    assert ipp.encode_attribute(ipp.INTEGER, "copies", 3) == b"\x21\x00\x06copies\x00\x04\x00\x00\x00\x03"
    assert ipp.encode_attribute(ipp.BOOLEAN, "ipp-attribute-fidelity", False) == (
        b"\x22\x00\x16ipp-attribute-fidelity\x00\x01\x00"
    )


def test_encode_multi_value_uses_empty_name_for_additional_values() -> None:
    assert ipp.encode_attribute(ipp.KEYWORD, "k", ["a", "b"]) == b"\x44\x00\x01k\x00\x01a" + b"\x44\x00\x00\x00\x01b"


def test_build_request_golden_bytes() -> None:
    data = ipp.build_request(
        ipp.OP_GET_PRINTER_ATTRIBUTES, 7, [(ipp.CHARSET, "attributes-charset", "utf-8")], [(ipp.INTEGER, "copies", 2)], b"DOC"
    )
    assert data == (
        b"\x01\x01"  # IPP 1.1
        b"\x00\x0b"  # Get-Printer-Attributes
        b"\x00\x00\x00\x07"  # Request-ID
        b"\x01" b"\x47\x00\x12attributes-charset\x00\x05utf-8"  # Operationsgruppe
        b"\x02" b"\x21\x00\x06copies\x00\x04\x00\x00\x00\x02"  # Auftragsgruppe
        b"\x03" b"DOC"  # Ende + Dokumentdaten
    )


def test_parse_response_reads_groups_values_and_payload() -> None:
    raw = (
        b"\x02\x00\x00\x01\x00\x00\x00\x09"  # Version 2.0, Status 0x0001 (ok, Attribute angepasst), Request-ID 9
        b"\x01" + ipp.encode_attribute(ipp.CHARSET, "attributes-charset", "utf-8")
        + b"\x04" + ipp.encode_attribute(ipp.ENUM, "printer-state", 3)
        + ipp.encode_attribute(ipp.BOOLEAN, "printer-is-accepting-jobs", True)
        + ipp.encode_attribute(ipp.KEYWORD, "sides-supported", ["one-sided", "two-sided-long-edge"])
        + b"\x03rest"
    )
    response = ipp.parse_response(raw)
    assert (response.status, response.request_id) == (1, 9)
    printer = response.attributes(ipp.TAG_PRINTER)
    assert printer["printer-state"] == [3]
    assert printer["printer-is-accepting-jobs"] == [True]
    assert printer["sides-supported"] == ["one-sided", "two-sided-long-edge"]
    assert response.attributes(ipp.TAG_OPERATION)["attributes-charset"] == ["utf-8"]
    assert response.payload == b"rest"


@pytest.mark.parametrize("raw", [b"", b"\x01\x01\x00", b"\x02\x00\x00\x00\x00\x00\x00\x01\x01\x47\x00", b"\x02\x00\x00\x00\x00\x00\x00\x01\x47"])
def test_parse_response_rejects_garbage(raw: bytes) -> None:
    with pytest.raises(ipp.IppError):
        ipp.parse_response(raw)


def test_detect_format() -> None:
    assert ipp.detect_format(b"%PDF-1.7 ...") == "application/pdf"
    assert ipp.detect_format(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert ipp.detect_format(b"II*\x00abc") == "image/tiff"
    assert ipp.detect_format(b"%!PS-Adobe") == "application/postscript"
    assert ipp.detect_format(b"\x89PNG\r\n") is None
    assert ipp.detect_format(b"hello") is None
    assert ipp.detect_format(b"") is None


def test_count_pdf_pages_ignores_pages_tree() -> None:
    assert ipp.count_pdf_pages(b"<< /Type /Pages /Count 5 >> << /Type/Page >> << /Type /Page /Parent 1 >>") == 2
    assert ipp.count_pdf_pages(b"%PDF-1.7 nur komprimiert") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("192.168.1.20", "ipp://192.168.1.20:631/ipp/print"),
        (" Drucker.local ", "ipp://drucker.local:631/ipp/print"),
        ("ipp://192.168.1.20", "ipp://192.168.1.20:631/ipp/print"),
        ("ipp://10.0.0.5:9631/druck", "ipp://10.0.0.5:9631/druck"),
        ("ipps://printer", "ipps://printer:443/ipp/print"),
    ],
)
def test_normalize_printer_url(raw: str, expected: str) -> None:
    assert ipp.normalize_printer_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "http://x", "ftp://x"])
def test_normalize_printer_url_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        ipp.normalize_printer_url(raw)


def test_version_matches_manifest() -> None:
    import json
    from pathlib import Path

    manifest = json.loads((Path(ipp.__file__).parent / "manifest.json").read_text())
    assert manifest["version"] == VERSION


def test_translations_have_same_keys() -> None:
    import json
    from pathlib import Path

    folder = Path(ipp.__file__).parent / "translations"

    def keys(node, prefix=""):
        out = set()
        for key, value in node.items():
            out |= keys(value, f"{prefix}{key}.") if isinstance(value, dict) else {f"{prefix}{key}"}
        return out

    de = keys(json.loads((folder / "de.json").read_text()))
    en = keys(json.loads((folder / "en.json").read_text()))
    assert de == en


def test_brand_icons_exist() -> None:
    from pathlib import Path

    brand = Path(ipp.__file__).parent / "brand"
    assert (brand / "icon.png").is_file() and (brand / "icon@2x.png").is_file()


# --- Seitenbereich -------------------------------------------------------------------------------
def test_encode_range_attribute_golden_bytes() -> None:
    # rangeOfInteger: Tag 0x33, je Bereich 2 x 4 Byte; weitere Bereiche als zusätzliche Werte (leerer Name)
    assert ipp.encode_attribute(ipp.RANGE, "page-ranges", [(1, 3), (5, 5)]) == (
        b"\x33\x00\x0bpage-ranges\x00\x08\x00\x00\x00\x01\x00\x00\x00\x03"
        b"\x33\x00\x00\x00\x08\x00\x00\x00\x05\x00\x00\x00\x05"
    )


def test_range_attribute_roundtrip() -> None:
    raw = b"\x02\x00\x00\x00\x00\x00\x00\x01\x02" + ipp.encode_attribute(ipp.RANGE, "page-ranges", [(2, 3), (9, 12)]) + b"\x03"
    assert ipp.parse_response(raw).attributes(ipp.TAG_JOB)["page-ranges"] == [(2, 3), (9, 12)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2-3", [(2, 3)]),
        ("7", [(7, 7)]),
        ("1-3,5", [(1, 3), (5, 5)]),
        (" 1 - 3 , 5 ", [(1, 3), (5, 5)]),  # Leerzeichen
        ("1\u20133", [(1, 3)]),  # typografischer Strich, den iOS gern einsetzt
        ("1\u20143", [(1, 3)]),
        ("5,1-3", [(1, 3), (5, 5)]),  # wird sortiert
        ("1-3,2-5", [(1, 5)]),  # Überlappung wird zusammengefasst
        ("1-3,4-6", [(1, 6)]),  # direkt aneinander ebenfalls
        ("1,1,1", [(1, 1)]),
        ("1-3,", [(1, 3)]),  # Komma am Ende
    ],
)
def test_parse_page_ranges(text: str, expected: list[tuple[int, int]]) -> None:
    assert ipp.parse_page_ranges(text) == expected


@pytest.mark.parametrize(
    "text", ["", "   ", ",", "0", "3-1", "abc", "1-", "-3", "1-2-3", "1.5", "1;3", "0-3", "99999", "1,3,5,7,9,11,13,15,17,19,21"]
)
def test_parse_page_ranges_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        ipp.parse_page_ranges(text)


def test_format_page_ranges() -> None:
    assert ipp.format_page_ranges([(1, 3), (5, 5), (7, 9)]) == "1-3, 5, 7-9"
