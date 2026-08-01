# -*- coding: utf-8 -*-
"""期刊订阅·中文源国内采集中继（在站长的国内机器上定时运行）。

背景：NCPSSD 等国内学术站自 2026-06 起拦截境外/机房 IP（期刊详情页 302 回首页、
连接重置），生产服务器（境外）抓不到中文刊 → 订阅批次只剩英文。国内网络不受影响。

本脚本在国内机器上：
  1) 用与服务器完全相同的采集代码抓取全部中文网页源（web_html）；
  2) 对 NCPSSD 文章就地补全真实出版日期与摘要（服务器侧的详情接口同样被拦，必须在这里补）；
  3) 打包成 journal_relay.json 经 scp 原子推送到服务器 APPDATA 目录。
服务器端 journal_alerts.fetch_source_articles 对 web_html 源「中继优先」：中继新鲜即采用，
批次/去重/时间窗/翻译/审核管线不变；中继过期则回退直抓并显式报错。

用法（在项目根目录）：
  python scripts/journal_relay_push.py            # 采集并推送
  python scripts/journal_relay_push.py --dry-run  # 只采集，写本地文件，不推送
计划任务注册见 run_journal_relay.ps1。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import journal_alerts as ja  # noqa: E402

SSH_HOST = os.environ.get("MARX_RELAY_SSH_HOST", "root@38.76.174.234")
SSH_KEY = os.environ.get(
    "MARX_RELAY_SSH_KEY", str(Path.home() / ".ssh" / "id_marx_cloud_ed25519")
)
REMOTE_PATH = os.environ.get(
    "MARX_RELAY_REMOTE_PATH", "/var/www/.marx_search_full/journal_relay.json"
)
# 上次成功推送的本地缓存：推送是整文件替换，某源本次瞬断（如 NCPSSD 偶发 SSL EOF）
# 若不沿用上次数据，该刊会从中继里消失一周 → 服务器回退直抓必失败。
# 沿用条目保留原 fetched_at，交由服务器端按新鲜度（≤10 天）自然淘汰。
CACHE_PATH = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "marx-journal-relay" / "journal_relay_last.json"


def _enrich_ncpssd(article: dict, budget: list[int]) -> bool:
    """对 NCPSSD 文章就地补全（镜像服务器 _apply_article_detail 的合并语义，作用于字典）。
    返回是否实际调用了详情接口（用于预算记账与统计）。"""
    meta = article.get("metadata") or {}
    lngid = str(meta.get("ncpssd_id") or "").strip()
    if not lngid or budget[0] <= 0:
        return False
    placeholder = ja._is_placeholder_pub_date(article.get("published_at"), lngid)
    missing_abstract = not str(article.get("abstract") or "").strip()
    if not placeholder and not missing_abstract:
        return False
    budget[0] -= 1
    try:
        detail = ja.fetch_ncpssd_detail(lngid)
    except Exception:
        return True
    if not detail:
        return True
    if detail.get("abstract") and missing_abstract:
        article["abstract"] = detail["abstract"]
    if detail.get("authors") and not (article.get("authors") or []):
        article["authors"] = detail["authors"]
    if detail.get("published_at") and placeholder:
        article["published_at"] = detail["published_at"]
    for key in ("pages", "doi", "issue"):
        if detail.get(key) and not str(article.get(key) or "").strip():
            article[key] = detail[key]
    if detail.get("pdf_url") and not str(article.get("pdf_url") or "").strip():
        article["pdf_url"] = detail["pdf_url"]
    if detail.get("keywords"):
        article.setdefault("metadata", {})["keywords"] = detail["keywords"]
    return True


def collect_payload(lookback_days: int, enrich_budget: int) -> tuple[dict, list[str]]:
    sources = [
        s for s in ja.DEFAULT_JOURNAL_SOURCES
        if s.get("language") == "zh" and s.get("source_type") == "web_html"
    ]
    budget = [enrich_budget]
    payload: dict = {
        "version": 1,
        "generated_at": ja.utc_now_text(),
        "generator": "journal_relay_push.py",
        "sources": {},
    }
    errors: list[str] = []
    for idx, source in enumerate(sources):
        if idx:
            time.sleep(2.5)  # 源间礼貌延时：连发 19 组请求曾触发 NCPSSD 瑞数 WAF 限流（解析成 0 篇）
        name = str(source.get("name") or "")
        src = dict(source)
        src.setdefault("id", 0)
        articles = None
        # 单源重试一次：NCPSSD 偶发 SSL EOF / 连接重置多为瞬断，隔几秒即恢复。
        for attempt in (1, 2):
            try:
                articles = ja.fetch_source_articles(src, lookback_days=lookback_days)
                break
            except Exception as exc:
                if attempt == 1:
                    time.sleep(4)
                    continue
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"  [失败] {name}: {exc}", flush=True)
        if articles is None:
            continue
        enriched = 0
        for article in articles:
            if _enrich_ncpssd(article, budget):
                enriched += 1
        payload["sources"][name] = {
            "fetched_at": ja.utc_now_text(),
            "articles": articles,
        }
        print(f"  [OK] {name}: {len(articles)} 篇（详情补全 {enriched} 次，余额 {budget[0]}）", flush=True)
    _merge_last_success(payload)
    _save_cache(payload)
    return payload, errors


def _merge_last_success(payload: dict) -> None:
    """本次抓失败或**抓到 0 篇**的源沿用上次成功条目（保留原 fetched_at，由服务器按新鲜度淘汰）。

    0 篇几乎总意味着 WAF 挑战页/页面结构变化而非「期刊真的没文章」——2026-07-13 实测
    连跑三轮触发 NCPSSD 限流后多个源解析为 0 篇；当成功推上去会把好数据顶掉一周。"""
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        cached_sources = cached.get("sources") or {}
    except Exception:
        return
    for name, entry in cached_sources.items():
        if not isinstance(entry, dict) or not entry.get("articles"):
            continue
        current = payload["sources"].get(name)
        if current is None or not current.get("articles"):
            payload["sources"][name] = entry
            print(f"  [沿用上次] {name}: {len(entry['articles'])} 篇（fetched_at={entry.get('fetched_at', '')[:16]}）", flush=True)


def _save_cache(payload: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"  [警告] 本地缓存写入失败：{exc}", flush=True)


def push(local_file: Path) -> None:
    tmp_remote = REMOTE_PATH + ".tmp"
    ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=25", "-i", SSH_KEY]
    subprocess.run(
        ["scp", *ssh_opts, str(local_file), f"{SSH_HOST}:{tmp_remote}"],
        check=True, timeout=180,
    )
    subprocess.run(
        ["ssh", "-n", "-T", *ssh_opts, SSH_HOST,
         f"mv -f {tmp_remote} {REMOTE_PATH} && chmod 644 {REMOTE_PATH}"],
        check=True, timeout=60,
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="采集中文期刊源并推送中继到服务器")
    ap.add_argument("--lookback", type=int, default=60, help="采集回看天数（默认 60）")
    # 预算须足以覆盖单日全部新文章：服务器侧 upsert 不回填已存在行的摘要/日期，
    # 漏补的文章会永久缺摘要——宁可这里多花几分钟一次补齐。
    ap.add_argument("--budget", type=int, default=300, help="NCPSSD 详情接口调用预算（默认 300）")
    ap.add_argument("--dry-run", action="store_true", help="只采集写本地文件，不推送")
    ap.add_argument("--out", default="", help="本地输出路径（默认临时目录）")
    args = ap.parse_args()

    print(f"开始采集（lookback={args.lookback}d, budget={args.budget}）…", flush=True)
    payload, errors = collect_payload(args.lookback, args.budget)
    ok_sources = len(payload["sources"])
    total = sum(len(v["articles"]) for v in payload["sources"].values())
    print(f"采集完成：{ok_sources} 源 / {total} 篇；失败 {len(errors)} 源", flush=True)
    if not ok_sources:
        print("全部源失败，不推送。", flush=True)
        return 2

    out = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "journal_relay.json"
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"已写 {out}（{out.stat().st_size} 字节）", flush=True)

    if args.dry_run:
        print("dry-run：跳过推送。", flush=True)
        return 0
    push(out)
    print(f"已推送到 {SSH_HOST}:{REMOTE_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
