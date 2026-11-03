"""Table spacing must use card margins; Feishu collapses Markdown blank lines."""

from __future__ import annotations

import pytest
from hermes_lark_streaming.cardkit import build_answer_elements
from hermes_lark_streaming.cardkit.md import split_table_spacing_blocks

_TABLE = "| Item | Value |\n| --- | --- |\n| A | B |"


@pytest.mark.parametrize(
    "source, expected",
    [
        (f"Before\n\n{_TABLE}\n\nAfter", ["Before", _TABLE, "After"]),
        (f"Before\n{_TABLE}\nAfter", [f"Before\n{_TABLE}\nAfter"]),
        (f"Before\n\n{_TABLE}", ["Before", _TABLE]),
        (f"{_TABLE}\n\nAfter", [_TABLE, "After"]),
        (_TABLE, [_TABLE]),
        (f"Before\n\n\n{_TABLE}\n \nAfter", ["Before", _TABLE, "After"]),
        (f"{_TABLE}\n\n{_TABLE}", [_TABLE, _TABLE]),
        ("First\n\nSecond", ["First\n\nSecond"]),
        (f"```markdown\nBefore\n\n{_TABLE}\n\nAfter\n```", None),
        (f"~~~markdown\nBefore\n\n{_TABLE}\n\nAfter\n~~~", None),
        (f"````markdown\n```\nBefore\n\n{_TABLE}\n\nAfter\n````", None),
        (f"```markdown\nBefore\n\n{_TABLE}\n\nAfter", None),
        ("Before\n\n    | A | B |\n    | --- | --- |\n    | 1 | 2 |\n\nAfter", None),
        ("Before\n\n| unfinished |\n| --", None),
        (f"```\n{_TABLE}\n```\n\nBefore\n\n{_TABLE}\n\nAfter",
         [f"```\n{_TABLE}\n```\n\nBefore", _TABLE, "After"]),
        (f"Before\n\n{_TABLE}\n\nMiddle\n\n{_TABLE}\n\nAfter",
         ["Before", _TABLE, "Middle", _TABLE, "After"]),
    ],
)
def test_table_spacing_boundaries(source: str, expected: list[str] | None) -> None:
    assert split_table_spacing_blocks(source) == (expected if expected is not None else [source])


def test_crlf_boundaries_preserve_table_line_endings() -> None:
    table = _TABLE.replace("\n", "\r\n")
    assert split_table_spacing_blocks(f"Before\r\n\r\n{table}\r\n\r\nAfter") == [
        "Before", table, "After",
    ]


def test_real_margins_keep_device_size_aliases_and_unique_ids() -> None:
    sizes = {"body": {"default": "normal", "mobile": "x-large"}}
    elements = build_answer_elements(f"Before\n\n{_TABLE}\n\nAfter", text_sizes=sizes)
    assert [element["content"] for element in elements] == ["Before", _TABLE, "After"]
    assert [element["margin"] for element in elements] == [
        "0px 0px 0px 0px", "16px 0px 0px 0px", "16px 0px 0px 0px",
    ]
    assert len({element["element_id"] for element in elements}) == 3
    assert all(len(element["element_id"]) <= 20 for element in elements)
    assert all(element["text_size"] == "hls_body" for element in elements)


def test_table_without_blank_lines_retains_one_element() -> None:
    content = f"Before\n{_TABLE}\nAfter"
    elements = build_answer_elements(content)
    assert len(elements) == 1
    assert elements[0]["content"] == content
    assert elements[0]["margin"] == "0px 0px 0px 0px"
