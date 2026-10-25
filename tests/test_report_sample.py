"""確認報告格式和 tests/sample/report.html 一致。

這個測試失敗代表報告的程式或範本改了，但範例沒有更新。確認改動是預期的之後，執行：

    uv run python tests/sample/make_sample.py
"""

import json

from .sample.make_sample import OUTPUT, build_sample_html


def _data(html: str) -> dict:
    return json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])


def test_sample_report_is_up_to_date():
    expected = OUTPUT.read_text(encoding="utf-8")
    actual = build_sample_html()
    if actual != expected:
        # 先比資料，比較容易看出差在哪裡
        assert _data(actual) == _data(expected), "報告資料和範例不同，請執行 uv run python tests/sample/make_sample.py"
        assert actual == expected, "報告範本和範例不同，請執行 uv run python tests/sample/make_sample.py"


def test_sample_covers_every_state():
    data = _data(OUTPUT.read_text(encoding="utf-8"))
    flights = [f for leg in data["legs"] for f in leg["flights"]]
    assert {f["st"] for f in flights} == {"new", "down", "up", "same"}
    assert {leg["o"] for leg in data["legs"] if leg["out"]} == {"TPE", "TSA"}
    assert {leg["out"] for leg in data["legs"]} == {True, False}
    assert any(len(leg["flights"]) > 1 for leg in data["legs"])  # 同一天多個航班
    assert len({tuple(r["days"]) for r in data["routes"]}) > 1  # 不同航線的天數不同
    assert "倫敦範例" not in {r["name"] for r in data["routes"]}  # enabled: false 不列
