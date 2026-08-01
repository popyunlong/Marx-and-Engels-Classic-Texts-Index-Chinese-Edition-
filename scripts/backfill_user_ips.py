#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填历史注册用户的 last_ip（用于「注册用户省际分布」覆盖到老用户）。

两遍：
  1) 空 IP 用户：从 reader_access_events / ai_usage 取最近一条非空 client_ip 填入（membership 层，幂等）。
  2) 「有 IP 但定位不出来」(私有/CGNAT 等) 的用户：从其阅读/AI 日志里取最近一条**能定位的公网 IP**
     覆盖 last_ip——只动定位不出来的用户，绝不覆盖已能定位的；让原本落「未识别」的人尽量排进地图。

服务器上（会员库在 /var/www 下的 APPDATA_DIR）执行：
    APP_MODE=server runuser -u www-data -- env HOME=/var/www .venv/bin/python scripts/backfill_user_ips.py
加 --dry-run 只统计、不写库。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import membership  # noqa: E402
import geoip  # noqa: E402


def _located(ip: str) -> bool:
    c = geoip.classify_ip(ip or "")
    return c.get("scope") in ("domestic", "overseas") and bool(c.get("province") or c.get("country"))


def _count_candidates() -> int:
    with membership._connect() as conn:  # noqa: SLF001 - 脚本内部统计
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM users
            WHERE TRIM(COALESCE(last_ip, '')) = '' AND TRIM(COALESCE(register_ip, '')) = ''
              AND EXISTS (SELECT 1 FROM reader_access_events r WHERE r.user_id = users.id AND TRIM(r.client_ip) <> '')
            """
        ).fetchone()
    return int(row["n"]) if row else 0


def _rescue_unlocatable_ips(*, dry_run: bool = False) -> dict:
    """第二遍：把「代表 IP 定位不出来」(私有/CGNAT/空) 的用户改写为其日志里最近一条可定位公网 IP。
    只动定位不出来的用户，绝不覆盖已能定位的；幂等可重复。"""
    now = membership.utc_now_text()
    checked = 0
    rescued = 0
    with membership._connect() as conn:  # noqa: SLF001
        ai_has_ip = "client_ip" in membership._table_columns(conn, "ai_usage")
        users = conn.execute(
            "SELECT id, COALESCE(NULLIF(TRIM(last_ip), ''), TRIM(register_ip)) AS ip FROM users"
        ).fetchall()
        targets = [u["id"] for u in users if not _located((u["ip"] or "").strip())]
        for uid in targets:
            checked += 1
            cands = [
                r[0]
                for r in conn.execute(
                    "SELECT client_ip FROM reader_access_events WHERE user_id=? AND TRIM(client_ip)<>'' ORDER BY created_at DESC",
                    (uid,),
                ).fetchall()
            ]
            if ai_has_ip:
                cands += [
                    r[0]
                    for r in conn.execute(
                        "SELECT client_ip FROM ai_usage WHERE user_id=? AND TRIM(client_ip)<>'' ORDER BY created_at DESC",
                        (uid,),
                    ).fetchall()
                ]
            hit = next((ip for ip in cands if _located(ip)), None)
            if not hit:
                continue
            rescued += 1
            if not dry_run:
                conn.execute("UPDATE users SET last_ip=?, updated_at=? WHERE id=?", (hit, now, uid))
        if not dry_run:
            conn.commit()
    return {"checked": checked, "rescued": rescued}


def main() -> int:
    parser = argparse.ArgumentParser(description="回填历史用户 last_ip")
    parser.add_argument("--dry-run", action="store_true", help="只统计、不写库")
    args = parser.parse_args()

    membership.init_membership_db()
    total_before = membership.count_registered_users()
    with_ip_before = sum(c for _, c in membership.get_user_ip_counts())

    if args.dry_run:
        rescue = _rescue_unlocatable_ips(dry_run=True)
        print(f"注册用户总数: {total_before}")
        print(f"当前已有 IP 的用户(去重计数和): {with_ip_before}")
        print(f"第一遍(空 IP)候选: {_count_candidates()}")
        print(f"第二遍(定位不出来→可救回公网 IP): 检查 {rescue['checked']} / 可救回 {rescue['rescued']}")
        return 0

    result = membership.backfill_user_ips_from_events()
    rescue = _rescue_unlocatable_ips()
    with_ip_after = sum(c for _, c in membership.get_user_ip_counts())
    print("回填完成:")
    print(f"  第一遍 从阅读日志补齐: {result['reader']} 人 / 从 AI 用量补齐: {result['ai_usage']} 人")
    print(f"  第二遍 私有/无法定位→改用日志可定位公网 IP: 救回 {rescue['rescued']} 人 (检查 {rescue['checked']})")
    print(f"  注册用户总数: {total_before}")
    print(f"  代表 IP 计数和: {with_ip_before} -> {with_ip_after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
