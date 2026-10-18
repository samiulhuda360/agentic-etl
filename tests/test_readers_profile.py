from __future__ import annotations

from typing import Any

import pytest

from agentic_etl.profile import NumberStyle, date_format, number_style, parse_number, profile, profile_column
from agentic_etl.readers import detect_encoding, read
from tests.conftest import path


def test_every_file_reads_with_the_expected_rows(manifest: dict[str, Any]) -> None:
    for f in manifest["files"]:
        raw = read(path(f["file"]))
        assert raw.df.height == f["rows"] + f["quarantine"], f["id"]
        assert all(str(t) == "String" for t in raw.df.schema.values()), f["id"]


def test_preamble_footer_and_meta_are_kept_aside() -> None:
    raw = read(path("files/harbour_bank_2026-03.csv"))
    assert raw.meta["Account"] == "12-3456-0123456-00"
    assert "@Account" in raw.df.columns
    assert any(s.startswith("Closing Balance") for s in raw.skipped)
    assert raw.df.columns[:5] == ["Date", "Reference", "Details", "Amount", "Balance"]


@pytest.mark.parametrize(
    ("rel", "encoding", "delimiter"),
    [
        ("files/harbour_bank_export_eu.csv", "cp1252", ";"),
        ("files/crm_export_utf16.txt", "utf-16", "\t"),
        ("files/timesheet_bom.csv", "utf-8-sig", ";"),
        ("files/sensor_units.psv", "utf-8", "|"),
    ],
)
def test_encodings_and_delimiters(rel: str, encoding: str, delimiter: str) -> None:
    raw = read(path(rel))
    assert raw.spec.encoding == encoding
    assert raw.spec.delimiter == delimiter


def test_detect_encoding_bytes() -> None:
    assert detect_encoding(b"\xef\xbb\xbfa,b") == "utf-8-sig"
    assert detect_encoding("é".encode("cp1252")) == "cp1252"
    assert detect_encoding("é".encode()) == "utf-8"


def test_excel_merged_header_and_sheet_stacking() -> None:
    raw = read(path("files/acme_fasteners_2026.xlsx"))
    assert raw.spec.header_rows == 2
    assert raw.spec.sheets == ["Screws", "Bolts", "Anchors"]
    assert "Read me" in raw.spec.skipped_sheets
    assert "Pricing Price ex GST" in raw.df.columns
    assert raw.meta["Supplier"] == "Acme Fasteners Ltd"
    assert set(raw.df["@sheet"].unique()) == {"Screws", "Bolts", "Anchors"}


def test_excel_skips_a_summary_sheet_with_another_layout() -> None:
    raw = read(path("files/pos_weekly.xlsx"))
    assert raw.spec.sheets == ["AK01", "AK02"]
    assert raw.spec.skipped_sheets == ["Summary"]


def test_fixed_width_keeps_values_with_spaces_whole() -> None:
    raw = read(path("files/kowhai_erp_export.txt"))
    assert raw.spec.format == "fixed"
    assert raw.df["SUPPLIER"][0] == "Kowhai Electrical"
    assert " " in raw.df["DESCRIPTION"][0]
    t3 = read(path("files/payroll_export.txt"))
    assert t3.meta == {"Exported": "2026-03-14"}
    assert any("END OF FILE" in s for s in t3.skipped)


def test_nested_json_is_flattened_and_exploded() -> None:
    raw = read(path("files/pos_export_2026-03-02.json"))
    assert raw.spec.record_path == "export.orders"
    assert raw.spec.explode == "items"
    assert {"items.#", "items.sku", "@export.store"} <= set(raw.df.columns)
    nd = read(path("files/pos_ak02_2026-03-03.ndjson"))
    assert nd.spec.format == "ndjson"
    assert "store.code" in nd.df.columns


def test_date_formats_and_day_first_tie_break() -> None:
    assert date_format(["13/03/2026", "01/04/2026"])[1] == "%d/%m/%Y"
    assert date_format(["03/13/2026", "04/01/2026"])[1] == "%m/%d/%Y"
    kind, fmt, alt = date_format(["01/02/2026", "03/04/2026"])
    assert fmt == "%d/%m/%Y" and "%m/%d/%Y" in alt
    assert date_format(["1772352000"])[1] == "epoch_s"
    assert date_format(["20260401"])[1] == "%Y%m%d"


def test_number_styles() -> None:
    st = number_style(["1.234,56", "-45,20"])
    assert st is not None and st.decimal == "," and st.thousands == "."
    assert parse_number("1.234,56", st) == pytest.approx(1234.56)
    us = number_style(["(45.20)", "1,200.00"])
    assert us is not None and us.paren_negative
    assert parse_number("(45.20)", us) == pytest.approx(-45.2)
    eur = number_style(["12,50 €"])
    assert eur is not None and eur.currency == "EUR"
    assert parse_number("-$1,234.56", NumberStyle(thousands=",")) == pytest.approx(-1234.56)
    assert parse_number("21.4°C", NumberStyle()) == pytest.approx(21.4)


def test_column_kinds() -> None:
    assert profile_column("p", ["+64 9 555 1234", "021 555 1234"]).kind == "phone"
    assert profile_column("e", ["a@b.co", "c@d.org"]).kind == "email"
    assert profile_column("h", ["7:30", "0:00"]).kind == "hours_minutes"
    assert profile_column("t", ["(F)", None]).kind in ("text", "code")
    assert profile_column("Temp (F)", ["70.1"]).unit == "f"
    assert profile_column("n", ["N/A", "", "3"]).nulls == 2


def test_candidate_keys_find_composites() -> None:
    prof = profile(read(path("files/timesheet_week12.xlsx")))
    assert ["Employee ID", "Date"] in prof.keys
