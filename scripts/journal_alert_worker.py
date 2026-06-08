from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai import ZAIClient, load_ai_config
from journal_alerts import (
    collect_batch,
    is_collect_due,
    is_send_due,
    load_alert_settings,
    load_smtp_config,
    public_base_url,
    run_journal_alerts_once,
    send_batch,
)
from runtime_env import load_deployment_settings

from datetime import datetime, timedelta, timezone

_BEIJING_TZ = timezone(timedelta(hours=8))


def _send_time_reached(settings: dict) -> bool:
    """当前北京时间是否已到达控制台配置的发送时间 HH:MM。"""
    raw = str(settings.get("send_time") or "08:00").strip()
    try:
        hour, minute = (int(x) for x in raw.split(":", 1))
    except ValueError:
        hour, minute = 8, 0
    now = datetime.now(timezone.utc).astimezone(_BEIJING_TZ)
    return (now.hour, now.minute) >= (hour, minute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Journal alerts worker: collect a batch (T-1 19:00) and send the digest on the send day."
    )
    parser.add_argument("--once", action="store_true", help="Legacy: collect then send if due (one cycle).")
    parser.add_argument(
        "--stage",
        choices=("collect", "send"),
        help="collect: fetch a new batch the night before sending. send: deliver the approved review.",
    )
    parser.add_argument(
        "--force",
        "--force-send",
        dest="force",
        action="store_true",
        help="Bypass schedule gating (collect even if not the eve of a send day / send even if not the send day).",
    )
    args = parser.parse_args()
    if not args.once and not args.stage:
        parser.error("Provide --stage=collect|send (or legacy --once).")

    deployment = load_deployment_settings()
    settings = load_alert_settings()
    base_url = public_base_url(deployment)

    if settings.get("automation_paused") and not args.force:
        print("journal-alerts paused: automation_paused is on; skipping (use --force to override).")
        return

    if args.stage == "collect":
        if not args.force and not is_collect_due(settings):
            print(
                "journal-alerts stage=collect skipped: not the eve of a send day "
                f"(freq={settings.get('send_frequency')}, weekday={settings.get('send_weekday')})."
            )
            return
        ai_client = ZAIClient(load_ai_config())
        result = collect_batch(ai_client=ai_client, settings=settings)
        print(
            "journal-alerts stage=collect status={status} batch={batch_id} review={review_status} "
            "sources={sources_checked} found={articles_found} filtered={filtered_out} "
            "inserted={articles_inserted} batch_total={batch_total} pending={batch_pending}".format(**result)
        )
        if result.get("error"):
            print(result["error"], file=sys.stderr)
        return

    if args.stage == "send":
        if not args.force and not is_send_due(settings):
            print(
                "journal-alerts stage=send skipped: not the send day "
                f"(freq={settings.get('send_frequency')}, weekday={settings.get('send_weekday')})."
            )
            return
        # send 定时器每小时触发：仅在到达「发送时间」后才真正发送；批次发出后会离开未发送集合，不会重复。
        if not args.force and not _send_time_reached(settings):
            print(
                f"journal-alerts stage=send waiting: send_time={settings.get('send_time')} "
                "not reached yet (Beijing)."
            )
            return
        outcome = send_batch(base_url=base_url, smtp_config=load_smtp_config(), force=args.force)
        print(
            "journal-alerts stage=send sent={sent} batch={batch_id} reason={reason}".format(
                sent=outcome.get("sent", 0),
                batch_id=outcome.get("batch_id"),
                reason=outcome.get("reason", "ok"),
            )
        )
        for err in outcome.get("errors") or []:
            print(err, file=sys.stderr)
        return

    # Legacy --once: collect then send if due (preserves old behaviour for manual/cron use).
    ai_client = ZAIClient(load_ai_config())
    should_send = args.force or is_send_due(settings)
    result = run_journal_alerts_once(
        ai_client=ai_client,
        base_url=base_url,
        smtp_config=load_smtp_config(),
        send=should_send,
    )
    print(
        "journal-alerts status={status} send={send} freq={freq} sources={sources_checked} "
        "found={articles_found} inserted={articles_inserted} emails={emails_sent}".format(
            send=should_send, freq=settings.get("send_frequency"), **result
        )
    )
    if result.get("error"):
        print(result["error"], file=sys.stderr)


if __name__ == "__main__":
    main()
