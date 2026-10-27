"""CLI 入口。

  flight-bot run --mode daily [--dry-run | --plan-only]
  flight-bot run --mode weekly [--dry-run]
  flight-bot search TPE NRT 2027-03-14 [--return 2027-03-19] [--max-stops 0]
  flight-bot calendar TPE NRT 2026-12-01 2027-05-01 [--max-stops 0] [--trip-length 5]
  flight-bot test-webhook
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import traceback
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import planner
from .config import load_config
from .notify.discord import Discord
from .providers.google import GoogleFlightsProvider

log = logging.getLogger("flight_bot")

TAIPEI = timezone(timedelta(hours=8))
STATE_KEY = "state/state.sqlite"
STATE_BAK_KEY = "state/state.sqlite.bak"
REPORT_PREFIX = "reports/daily/"
SITE_DIR = Path("site")


def load_dotenv(path: str = ".env") -> None:
    """簡易 .env 讀取；已存在的環境變數優先。"""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def today_taipei() -> date:
    return datetime.now(TAIPEI).date()


# ---------- run ----------


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    today = date.fromisoformat(args.today) if args.today else today_taipei()

    if args.plan_only:
        return print_plan(cfg, today)

    from .storage import backend as backends
    from .storage.raw import RawWriter
    from .storage.state import State

    discord = Discord(dry_run=args.dry_run)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

    try:
        backend = backends.from_env()
    except RuntimeError:
        if not args.dry_run:
            raise
        backend = None
        log.warning("沒有設定儲存位置，dry-run 改用空的 state")

    workdir = Path(tempfile.mkdtemp(prefix="flight-bot-"))
    state_path = workdir / "state.sqlite"
    existing = backend.get(STATE_KEY) if backend else None
    if existing:
        state_path.write_bytes(existing)
        log.info("已下載 state（%d KB）", len(existing) // 1024)
    else:
        log.info("沒有 state，建立新的")
    state = State(state_path)

    try:
        if args.mode == "daily":
            run_daily(cfg, state, discord, backend, today, run_id, args.dry_run, RawWriter(run_id, today))
        else:
            run_weekly(cfg, state, discord, backend, today, args.dry_run)
    except Exception:
        tb = traceback.format_exc()
        log.error(tb)
        discord.send(content=f"**flight-bot 異常**\n程式發生未預期的例外\n```\n{tb[-1500:]}\n```")
        state.close()
        return 1

    state.close()
    if backend and not args.dry_run:
        if existing:
            backend.copy(STATE_KEY, STATE_BAK_KEY)
        backend.put(STATE_KEY, state_path.read_bytes())
        log.info("已上傳 state 到 %s", backend.name)
    return 0


def run_daily(cfg, state, discord, backend, today, run_id, dry_run, raw) -> None:
    from .daily import Daily
    from .runner import Runner

    runner = Runner(cfg.runner)
    state.start_run(run_id, "daily")
    daily = Daily(cfg, GoogleFlightsProvider(), state, raw, discord, runner, today, run_id)
    try:
        report = daily.run()
    finally:
        stats = runner.stats
        state.finish_run(run_id, stats.total("ok"), stats.total("failed"), stats.total("empty"),
                         stats.aborted, 0, stats.by_kind())
        state.commit()
        if backend and not dry_run:
            for key in raw.flush(backend):
                log.info("已寫入 %s", key)

    log.info("完成：逐日查詢 %d 次、單程航班 %d 班（%d 個日期）、符合條件 %d 班",
             report.scan_tasks, report.flights, len(report.legs), len(report.hits))
    log.info("請求統計：%s", runner.stats.by_kind())
    publish_report(cfg, daily, runner, backend, today, run_id, dry_run)


def publish_report(cfg, daily, runner, backend, today, run_id, dry_run) -> None:
    """產生網頁報告到 site/（GitHub Pages 部署用）；正式執行時另存一份到 R2 當歷史記錄。"""
    from .report import build_data, render

    day = today.isoformat()
    keys = backend.list(REPORT_PREFIX) if backend else []
    dates = sorted({k.removeprefix(REPORT_PREFIX).removesuffix(".html") for k in keys if k.endswith(".html")} | {day})
    dates = [d for d in dates if d <= day][-cfg.report.keep_days:]

    data = build_data(cfg, daily.report, runner.stats, run_id, today, daily.started,
                      datetime.now(timezone.utc), dates)
    archive = render(data, base="../")
    if backend and not dry_run:
        backend.put(f"{REPORT_PREFIX}{day}.html", archive.encode(), "text/html; charset=utf-8")

    (SITE_DIR / "d").mkdir(parents=True, exist_ok=True)
    (SITE_DIR / ".nojekyll").write_text("")
    (SITE_DIR / "index.html").write_text(render(data), encoding="utf-8")
    # 每份報告打開時讀這個檔案更新日期選單，舊報告也能跳到比它新的日期
    (SITE_DIR / "dates.json").write_text(json.dumps(dates), encoding="utf-8")
    (SITE_DIR / "d" / f"{day}.html").write_text(archive, encoding="utf-8")
    for d in dates:
        if d == day or backend is None:
            continue
        old = backend.get(f"{REPORT_PREFIX}{d}.html")
        if old:
            (SITE_DIR / "d" / f"{d}.html").write_bytes(old)
    log.info("報告已產生：%s（歷史 %d 天）", (SITE_DIR / "index.html").resolve(), len(dates))


def run_weekly(cfg, state, discord, backend, today, dry_run) -> None:
    from .notify import weekly

    if not cfg.weekly_summary.enabled:
        log.info("每週摘要已停用")
        return
    embeds, files = weekly.build(cfg, state, today)
    discord.send(embeds=embeds, files=files)
    if backend and not dry_run:
        week = today.strftime("%G-W%V")
        for name, data in files.items():
            backend.put(f"reports/{week}-{name.removesuffix('.png')}.png", data, "image/png")
    elif dry_run and files:
        out = Path(".local-storage/reports")
        out.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (out / name).write_bytes(data)
        log.info("圖片存在 %s", out)


def print_plan(cfg, today: date) -> int:
    print(f"今天：{today}")
    for r in cfg.routes():
        print(f"- {r.name}：{','.join(r.origins)} ⇄ {','.join(r.dests)} 去程 {r.query_start} ~ {r.query_end} "
              f"天數 {list(r.trip_days)} 轉機 ≤{r.max_stops}")
    tasks = planner.per_date_tasks(cfg, today)
    per_leg = Counter(f"{t.origin}→{t.dest}" for t in tasks)
    print(f"\n逐日查詢：{len(tasks)} 次（同一組機場、同一天只查一次，多條航線共用）")
    for k, n in sorted(per_leg.items()):
        print(f"  {k}: {n}")
    return 0


# ---------- 單次查詢 ----------


def cmd_search(args) -> int:
    from . import airports

    provider = GoogleFlightsProvider()
    depart = date.fromisoformat(args.date)
    ret = date.fromisoformat(args.ret) if args.ret else None
    result = provider.search(args.origin, args.dest, depart, ret, args.max_stops)
    rng = f"（典型區間 {result.typical_range[0]:,}–{result.typical_range[1]:,}）" if result.typical_range else ""
    print(f"{args.origin} → {args.dest} {depart}{' / ' + str(ret) if ret else ''}："
          f"{len(result.flights)} 個航班，Google 價格等級 {result.price_level or '—'}{rng}")
    for f in sorted(result.flights, key=lambda f: f.price):
        names = "、".join(airports.airline(a) for a in f.airlines)
        via = "" if not f.layovers_min else " 轉機 " + "、".join(
            f"{s.to_airport} {m // 60}h{m % 60:02d}m" for s, m in zip(f.segments, f.layovers_min))
        print(f"  TWD {f.price:>7,}  {f.depart_at:%m/%d %H:%M} → {f.arrive_at:%m/%d %H:%M}  "
              f"{' '.join(f.flight_nos):<16} {names}  {f.duration_min // 60}h{f.duration_min % 60:02d}m{via}")
    return 0


def cmd_calendar(args) -> int:
    provider = GoogleFlightsProvider()
    quotes = provider.calendar(args.origin, args.dest, date.fromisoformat(args.start),
                               date.fromisoformat(args.end), args.max_stops, args.trip_length)
    priced = [q for q in quotes if q.price is not None]
    print(f"{args.origin} → {args.dest}：{len(quotes)} 天，{len(priced)} 天有價格")
    for q in quotes:
        ret = f" → {q.return_date}" if q.return_date else ""
        print(f"  {q.depart_date}{ret}  {'—' if q.price is None else f'TWD {q.price:,}'}")
    return 0


def cmd_test_webhook(args) -> int:
    discord = Discord()
    if discord.dry_run:
        print("沒有設定 DISCORD_WEBHOOK_URL（環境變數或 .env），只印出內容：")
    discord.send(content="flight-bot webhook 測試", embeds=[{
        "title": "測試訊息", "description": "如果看得到這則訊息，webhook 設定正確。", "color": 0x2ECC71,
    }])
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(prog="flight-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="執行每日查詢或每週摘要")
    p.add_argument("--mode", choices=["daily", "weekly"], required=True)
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--dry-run", action="store_true", help="實際查詢，但不發送 Discord、不上傳 R2")
    p.add_argument("--plan-only", action="store_true", help="只印出今天的查詢清單和數量")
    p.add_argument("--today", help="覆寫今天的日期（測試用，YYYY-MM-DD）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("search", help="逐日查詢一個日期")
    p.add_argument("origin")
    p.add_argument("dest")
    p.add_argument("date")
    p.add_argument("--return", dest="ret")
    p.add_argument("--max-stops", type=int, default=0)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("calendar", help="用日期價格 API 查一段日期")
    p.add_argument("origin")
    p.add_argument("dest")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("--max-stops", type=int, default=0)
    p.add_argument("--trip-length", type=int)
    p.set_defaults(func=cmd_calendar)

    p = sub.add_parser("test-webhook", help="發送一則測試訊息")
    p.set_defaults(func=cmd_test_webhook)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
