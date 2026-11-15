"""Range aggregates for the admin dashboard; no per-day live/full-detail scans."""
from datetime import datetime, timedelta, timezone
import sqlite3


def history(days, *, threshold=50000, ratio=.8, full=False):
    import membership as M
    if not days:
        return []
    # Bound SQL parameters for old SQLite builds and all-history exports.
    if len(days) > 120:
        rows = []
        for offset in range(0, len(days), 120):
            rows.extend(history(days[offset:offset+120], threshold=threshold, ratio=ratio, full=full))
        return sorted(rows, key=lambda row: row['day'], reverse=True)
    now = M.utc_now_text()
    ranges = []
    for day in sorted(days):
        start = datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=8)))
        begin, end = (d.astimezone(timezone.utc).isoformat(timespec='seconds') for d in (start, start+timedelta(days=1)))
        ranges.append((day, begin, end, now if begin <= now < end else end))
    cte = 'WITH ranges(day,start_at,end_at,as_of) AS (VALUES '+','.join('(?,?,?,?)' for _ in ranges)+') '
    args = tuple(value for row in ranges for value in row)
    result = {day: dict(day=day, current_online=None, pending_orders=None, db_status='', ai_ok=None,
                       payment_ok=None, top_token_users=[], high_token_users=[], high_token_user_count=0,
                       reader_anomaly_count=None) for day in days}
    with M._connect() as conn:
        totals = conn.execute("SELECT COUNT(*) total_users, SUM(is_active=1) active_accounts, "
                              "SUM(email_verified_at!='') verified_users, SUM(is_active=0 OR deactivated_at!='') disabled_users FROM users").fetchone()
        for row in result.values():
            row.update(dict(totals))

        def merge(sql, params=()):
            for row in conn.execute(sql, params):
                if row['day'] in result:
                    result[row['day']].update(dict(row))

        merge("WITH identities AS (SELECT day,session_key,MAX(user_id) user_id FROM site_activity "
              "WHERE day BETWEEN ? AND ? GROUP BY day,session_key) "
              "SELECT a.day,COUNT(DISTINCT CASE WHEN COALESCE(a.user_id,i.user_id) IS NOT NULL "
              "THEN 'u:'||COALESCE(a.user_id,i.user_id) ELSE 's:'||a.session_key END) today_online,"
              "COUNT(DISTINCT a.user_id) registered_online_today,"
              "SUM(CASE WHEN feature='search' THEN request_count ELSE 0 END) searches_today,"
              "SUM(CASE WHEN feature='reader' THEN request_count ELSE 0 END) reader_views_today,"
              "SUM(CASE WHEN feature IN ('pricing','pricing_journal') THEN request_count ELSE 0 END) pricing_views_today,"
              "SUM(CASE WHEN feature='pricing_journal' THEN request_count ELSE 0 END) pricing_from_journal_today "
              "FROM site_activity a JOIN identities i ON i.day=a.day AND i.session_key=a.session_key "
              "WHERE a.day BETWEEN ? AND ? GROUP BY a.day", (min(days),max(days),min(days),max(days)))
        merge(cte+"SELECT d.day,COUNT(u.id) new_users_today FROM ranges d LEFT JOIN users u "
              "ON u.created_at>=d.start_at AND u.created_at<d.end_at GROUP BY d.day",args)
        merge(cte+"SELECT d.day,COUNT(DISTINCT s.user_id) active_members FROM ranges d LEFT JOIN subscriptions s "
              "ON s.status='active' AND s.starts_at<d.as_of AND s.expires_at>d.as_of GROUP BY d.day",args)
        merge(cte+"SELECT d.day,COUNT(DISTINCT a.user_id) member_online_today FROM ranges d JOIN site_activity a ON a.day=d.day "
              "WHERE EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=a.user_id AND s.status='active' "
              "AND s.starts_at<d.as_of AND s.expires_at>d.as_of) GROUP BY d.day",args)
        merge(cte+"SELECT d.day,COALESCE(SUM(o.amount_cents),0) paid_today_cents FROM ranges d LEFT JOIN orders o "
              "ON o.status='paid' AND o.paid_at>=d.start_at AND o.paid_at<d.end_at GROUP BY d.day",args)
        merge(cte+"SELECT d.day,COUNT(o.id) payment_errors_today FROM ranges d LEFT JOIN orders o "
              "ON o.status IN ('failed','cancelled','expired') AND o.created_at>=d.start_at AND o.created_at<d.end_at GROUP BY d.day",args)
        merge("SELECT day,COUNT(*) ai_requests_today,SUM(total_tokens) ai_tokens_today,SUM(success=0) ai_errors_today,"
              "SUM(provider='zhipu') zhipu_requests_today,SUM(CASE WHEN provider='zhipu' THEN total_tokens ELSE 0 END) zhipu_tokens_today "
              "FROM ai_usage WHERE day BETWEEN ? AND ? GROUP BY day",(min(days),max(days)))
        token_sql = cte+", totals AS (SELECT day,user_id,COUNT(*) request_count,SUM(total_tokens) total_tokens,"
        token_sql += "MAX(total_tokens) max_request_tokens,SUM(success=0) error_count,MIN(created_at) first_used_at,MAX(created_at) last_used_at "
        token_sql += "FROM ai_usage WHERE day BETWEEN ? AND ? GROUP BY day,user_id), ranked AS ("
        token_sql += "SELECT t.*,u.email,u.display_name,u.daily_ai_token_limit_override user_limit,p.daily_ai_token_limit plan_limit,p.name plan_name,"
        token_sql += "ROW_NUMBER() OVER(PARTITION BY t.day ORDER BY total_tokens DESC,request_count DESC,t.user_id ASC) ranking "
        token_sql += "FROM totals t JOIN users u ON u.id=t.user_id JOIN ranges d ON d.day=t.day LEFT JOIN subscriptions s ON s.id=("
        token_sql += "SELECT s2.id FROM subscriptions s2 WHERE s2.user_id=u.id AND s2.status='active' AND s2.starts_at<d.as_of AND s2.expires_at>d.as_of "
        token_sql += "ORDER BY s2.expires_at DESC,s2.created_at DESC,s2.id DESC LIMIT 1) LEFT JOIN plans p ON p.code=s.plan_code) SELECT * FROM ranked WHERE ranking<=20 ORDER BY day,ranking"
        for record in conn.execute(token_sql, (*args,min(days),max(days))):
            row = dict(record)
            limit = row['user_limit'] if row['user_limit'] is not None else row['plan_limit']
            tokens = int(row['total_tokens'] or 0)
            row['effective_limit'] = limit
            row['limit_ratio'] = None if limit is None else round(tokens/max(1,limit),3)
            reasons=[]
            if tokens >= threshold: reasons.append(f'超过 {threshold} token')
            if limit is not None and tokens >= limit: reasons.append('超过每日限额')
            elif limit is not None and tokens >= int(limit*ratio): reasons.append(f'达到限额 {int(ratio*100)}%')
            row['alert_reason']='；'.join(reasons)
            result[row['day']]['top_token_users'].append(row)
            if reasons: result[row['day']]['high_token_users'].append(row)
        try:
            shared = dict(conn.execute("SELECT (SELECT COUNT(*) FROM journal_subscriptions WHERE status='active') journal_subscriptions,"
                         "(SELECT COUNT(*) FROM journal_articles WHERE status='pending_review') journal_pending_articles,"
                         "(SELECT COUNT(*) FROM journal_articles WHERE status='ready') journal_ready_articles").fetchone())
            for row in result.values(): row.update(shared)
            merge(cte+"SELECT d.day,(SELECT COUNT(*) FROM journal_digest_deliveries WHERE status='sent' AND created_at>=d.start_at AND created_at<d.end_at) + "
                  "(SELECT COUNT(*) FROM journal_delivery_logs WHERE created_at>=d.start_at AND created_at<d.end_at) journal_recent_sends FROM ranges d",args)
        except sqlite3.OperationalError:
            for row in result.values():
                row.update(journal_subscriptions=0,journal_pending_articles=0,journal_ready_articles=0,journal_recent_sends=0)
    for day,row in result.items():
        row['high_token_user_count']=len(row['high_token_users'])
        if full:
            row['reader_anomaly_count']=len(M.list_reader_anomaly_visitors(day=day,limit=20))
    return [result[day] for day in sorted(days,reverse=True)]
