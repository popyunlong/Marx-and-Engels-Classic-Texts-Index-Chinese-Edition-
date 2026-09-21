"""Public weekly-chart policy; never used for access, quotas or search results."""

ANALYTICS_HEADER = "X-Community-Analytics"


def omit_community_sample(headers) -> bool:
    """A probe can opt out of the public chart, without gaining any privileges."""
    return str(headers.get(ANALYTICS_HEADER) or "").strip().casefold() == "exclude"


# Reproduced from the 2026-09-14 recommendation acceptance packages. Eight
# dedicated sessions each ran all six probes and two normal-looking queries.
# Quarantine only those complete probe batches, not the phrases themselves:
# a real reader searching the same words must still be counted. Keep raw events
# intact, and apply before aggregation/LIMIT in both current and carried weeks.
LEGACY_PROBE_TEXTS = (
    "民公认是坚持改革开放路线",
    "相伴的还有历史文化领域的",
    "的执政能力和领导水平同改",
    "的诗歌却呈现出了对离群索",
    "当一个对象仅仅被理解为构",
    "文在动植物界中重新认识了",
)
LEGACY_SEARCH_EXCLUSION_SQL = """
    AND (actor_hash, bucket_start) NOT IN (
        SELECT actor_hash, bucket_start
        FROM community_trend_events
        WHERE day = '2026-09-14' AND kind = 'search'
          AND item_key IN (?, ?, ?, ?, ?, ?)
        GROUP BY actor_hash, bucket_start
        HAVING COUNT(DISTINCT item_key) = 6
    )
"""
