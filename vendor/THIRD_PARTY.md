# Vendored third-party components

These files are runtime dependencies captured in the production baseline. Do
not replace or delete them until the only import path has been verified and the
new version has passed the full integration gate.

| Path | Upstream/version | License | Deterministic tree checksum |
| --- | --- | --- | --- |
| `vendor/textual/jieba` | jieba 0.42.1 | MIT (`vendor/textual/LICENSE.jieba`) | `e576b6f0f1004130c12ccbcec03afdd53d1add8a50fad7db4c28f1da12cfd02f` |
| `vendor/ip2region` | ip2region xdb Python snapshot dated 2025-10-30; upstream revision was not recorded by the legacy deployment | Apache-2.0 (`vendor/ip2region/LICENSE`) | `90ab2090c7794fe623a26250efcec9fa71f13d2bd926827930c7660c73b29547` |

The checksum is SHA-256 over UTF-8 lines of
`<repository-relative-path><TAB><file-sha256><LF>`, sorted in Git path order.
For `vendor/textual`, the checksum includes its license and the complete
`jieba` subtree; for `vendor/ip2region`, it includes all four tracked files.
Any intentional vendor update must update this table in the same commit.

## Offline IP location data

`data/geo/ip2region_v4.xdb` and `ip2region_v6.xdb` are Apache-2.0 data
from lionsoul2014/ip2region (license: `vendor/ip2region/LICENSE`). Exact upstream
revisions, source URLs, sizes and SHA-256 values are pinned in
`data/geo/manifest.json`. These immutable release resources intentionally take
precedence over legacy shared data. Explicit XDB environment overrides must
match the release checksum. No user IP is sent to the upstream provider.
