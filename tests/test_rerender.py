import json

from flight_bot import main
from flight_bot.report import extract_data, render
from flight_bot.storage.backend import LocalBackend

from .sample.make_sample import OUTPUT

PREFIX = main.REPORT_PREFIX


def test_extract_data_round_trips():
    html = OUTPUT.read_text(encoding="utf-8")
    assert render(extract_data(html)) == html


def test_extract_data_rejects_old_reports():
    assert extract_data("<html>沒有資料</html>") is None
    assert extract_data('<script id="data" type="application/json">{"run": {}}</script>') is None


def test_rerender_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SITE_DIR", tmp_path / "site")
    backend = LocalBackend(tmp_path / "storage")
    data = extract_data(OUTPUT.read_text(encoding="utf-8"))
    for day in ["2026-10-20", "2026-10-21", "2026-10-22"]:
        stale = render({**data, "run": {**data["run"], "date": day}}, base="../").replace("<title>", "<title>舊範本 ")
        backend.put(f"{PREFIX}{day}.html", stale.encode())
    backend.put(f"{PREFIX}2026-10-23.html", "<html>格式太舊</html>".encode())

    done = main.rerender_reports(backend, keep_days=3)

    assert done == ["2026-10-21", "2026-10-22"]
    site = tmp_path / "site"
    assert json.loads((site / "dates.json").read_text()) == ["2026-10-21", "2026-10-22", "2026-10-23"]
    # 重新產生的報告換成目前的範本，歷史清單只到自己那天；R2 上的也一起更新
    page = (site / "d" / "2026-10-22.html").read_text()
    assert "舊範本" not in page
    assert extract_data(page)["history"] == ["2026-10-21", "2026-10-22"]
    assert backend.get(f"{PREFIX}2026-10-22.html").decode() == page
    assert "舊範本" in backend.get(f"{PREFIX}2026-10-20.html").decode()  # 超過 keep_days 的不動
    # 格式太舊的原樣保留；首頁是最後一份能重新產生的
    assert (site / "d" / "2026-10-23.html").read_text() == "<html>格式太舊</html>"
    assert extract_data((site / "index.html").read_text())["run"]["date"] == "2026-10-22"
    assert extract_data((site / "index.html").read_text())["base"] == ""


def test_rerender_dry_run_keeps_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SITE_DIR", tmp_path / "site")
    backend = LocalBackend(tmp_path / "storage")
    stale = OUTPUT.read_text(encoding="utf-8").replace("<title>", "<title>舊範本 ")
    backend.put(f"{PREFIX}2026-10-25.html", stale.encode())

    assert main.rerender_reports(backend, keep_days=30, dry_run=True) == ["2026-10-25"]
    assert backend.get(f"{PREFIX}2026-10-25.html").decode() == stale
    assert "舊範本" not in (tmp_path / "site" / "d" / "2026-10-25.html").read_text()
