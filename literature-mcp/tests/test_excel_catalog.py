"""Read-only Excel bibliography parser tests."""

import excel_catalog


class _Worksheet:
    def __init__(self, rows, title="References"):
        self.rows = [tuple(row) for row in rows]
        self.title = title

    def iter_rows(self, min_row=1, max_row=None, max_col=None, values_only=True):
        end = len(self.rows) if max_row is None else min(max_row, len(self.rows))
        for row in self.rows[min_row - 1 : end]:
            yield row if max_col is None else row[:max_col]


def test_header_detection_is_independent_of_column_order() -> None:
    worksheet = _Worksheet(
        [[
            "优先级（1-5）",
            "官方 URL",
            "作者",
            "文献题目",
            "DOI URL",
            "与你研究的关系",
            "期刊",
            "主题/作用",
            "年份",
        ]]
    )

    schema = excel_catalog._schema_for_sheet(worksheet)

    assert schema["header_row"] == 1
    assert schema["reference_candidate"] is True
    assert {
        "title",
        "authors",
        "year",
        "journal/source",
        "DOI",
        "DOI_URL",
        "official_URL",
        "priority",
        "topic",
        "research_relationship",
    } <= set(schema["recognized_fields"])


def test_mixed_url_column_normalises_doi_and_preserves_source_row() -> None:
    header = (
        "年份",
        "第一作者",
        "文献题目",
        "期刊",
        "主题/作用",
        "与你研究的关系",
        "优先级（1-5）",
        "官方/DOI完整URL",
    )
    mapping = excel_catalog._header_mapping(header)

    record = excel_catalog._parse_row(
        "推荐文献30篇",
        7,
        (2024, "Zhang", "Example", "Journal", None, "相关", 5, "https://doi.org/10.1234/ABC.5"),
        mapping,
    )

    assert record["source_sheet"] == "推荐文献30篇"
    assert record["source_row"] == 7
    assert record["DOI"] == "10.1234/abc.5"
    assert record["DOI_URL"] == "https://doi.org/10.1234/abc.5"
    assert record["official_URL"] is None
    assert record["topic"] is None
    assert record["errors"] == []


def test_mixed_url_column_keeps_non_doi_official_url() -> None:
    header = ("文献题目", "年份", "官方/DOI完整URL")
    mapping = excel_catalog._header_mapping(header)

    record = excel_catalog._parse_row(
        "References", 2, ("Example", 2020, "https://publisher.example/article"), mapping
    )

    assert record["DOI"] is None
    assert record["official_URL"] == "https://publisher.example/article"


def test_bad_row_is_reported_without_stopping_later_rows(monkeypatch) -> None:
    worksheet = _Worksheet(
        [
            ("文献题目", "年份", "DOI URL"),
            ("Broken", "not-a-year", "not-a-doi"),
            ("Good", 2024, "https://doi.org/10.1234/good"),
        ]
    )
    original = excel_catalog._parse_row

    def occasionally_fails(sheet, source_row, row, mapping):
        if source_row == 2:
            raise ValueError("bad row")
        return original(sheet, source_row, row, mapping)

    monkeypatch.setattr(excel_catalog, "_parse_row", occasionally_fails)

    result = excel_catalog._sheet_extraction(worksheet, 10)

    assert [item["source_row"] for item in result["records"]] == [3]
    assert result["unrecognized_records"][0]["source_row"] == 2
    assert result["unrecognized_records"][0]["reason"] == "该行发生未预期的解析错误"


def test_missing_title_and_doi_is_reported_as_unrecognized() -> None:
    worksheet = _Worksheet(
        [
            ("文献题目", "年份", "DOI URL", "期刊"),
            (None, 2024, None, "Example Journal"),
            ("Valid", None, None, None),
        ]
    )

    result = excel_catalog._sheet_extraction(worksheet, 10)

    assert result["records"][0]["source_row"] == 3
    assert result["unrecognized_records"][0]["source_row"] == 2
    assert result["unrecognized_records"][0]["raw_values"]["年份"] == "2024"


def test_invalid_values_are_record_errors_not_file_errors() -> None:
    header = ("文献题目", "年份", "DOI", "优先级")
    mapping = excel_catalog._header_mapping(header)

    record = excel_catalog._parse_row(
        "References", 9, ("Keep me", "unknown", "bad DOI", "urgent"), mapping
    )

    assert record["title"] == "Keep me"
    assert record["year"] is None
    assert record["priority"] == "urgent"
    assert "年份无法识别" in record["errors"]
    assert "DOI 无法识别" in record["errors"]
