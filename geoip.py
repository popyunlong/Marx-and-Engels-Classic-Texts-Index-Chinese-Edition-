"""离线 IP→省份/国家 归类（基于 ip2region v4 xdb，纯本地、不联网、不外发用户 IP）。

· 数据：data/geo/ip2region_v4.xdb（Apache-2.0，lionsoul2014/ip2region）。
· 客户端：vendor/ip2region（同上游 python binding，已随仓库 vendored）。
· 缓存策略 content：启动时把整个 xdb（~10MB）读入内存一次，之后查询纯内存、微秒级；
  content 模式的 Searcher.search() 只读不可变 bytes，8 线程 waitress 下并发安全。

对外只暴露聚合后的省级计数（见 app 层），绝不把单个用户 IP 落到任何对外响应里。
"""
from __future__ import annotations

import ipaddress
import os
import sys
import threading

# vendor 目录（ip2region python 包）加入 import 路径
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

try:
    from runtime_env import EXTERNAL_DATA_DIR, BUNDLED_DATA_DIR
    _DEFAULT_CANDIDATES = {
        # 双栈各一库：新用户大多经 Cloudflare 双栈/国内移动网络以 IPv6 接入（电信 240e::/
        # 移动 2409:8::/联通 2408::），只带 v4 库时这些用户全部落「未知」——2026-07 实测正是
        # 注册分布图「新用户全归无法识别」的根因。
        "v4": [
            EXTERNAL_DATA_DIR / "geo" / "ip2region_v4.xdb",
            BUNDLED_DATA_DIR / "geo" / "ip2region_v4.xdb",
        ],
        "v6": [
            EXTERNAL_DATA_DIR / "geo" / "ip2region_v6.xdb",
            BUNDLED_DATA_DIR / "geo" / "ip2region_v6.xdb",
        ],
    }
except Exception:  # pragma: no cover - 纯数据层防御
    _DEFAULT_CANDIDATES = {"v4": [], "v6": []}

_XDB_ENV = {"v4": "MARX_IP2REGION_XDB", "v6": "MARX_IP2REGION_XDB6"}


def _xdb_path(family: str = "v4") -> str:
    override = (os.environ.get(_XDB_ENV.get(family, "")) or "").strip()
    if override:
        return override
    candidates = _DEFAULT_CANDIDATES.get(family) or []
    for cand in candidates:
        try:
            if cand.exists():
                return str(cand)
        except Exception:  # noqa: BLE001
            continue
    # 回退到约定路径（即便不存在，加载时会被 try 捕获、降级为「未知」）
    return str(candidates[0]) if candidates else f"data/geo/ip2region_{family}.xdb"


# --- 省级规范名（与 static/geo/china_provinces.min.json 的 key、与地图省界一一对应）---
CANONICAL_PROVINCES = [
    "北京市", "天津市", "河北省", "山西省", "内蒙古自治区", "辽宁省", "吉林省", "黑龙江省",
    "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省",
    "湖北省", "湖南省", "广东省", "广西壮族自治区", "海南省", "重庆市", "四川省", "贵州省",
    "云南省", "西藏自治区", "陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
    "台湾省", "香港特别行政区", "澳门特别行政区",
]

# 显示用简称（榜单/标签）
PROVINCE_SHORT = {
    "北京市": "北京", "天津市": "天津", "河北省": "河北", "山西省": "山西",
    "内蒙古自治区": "内蒙古", "辽宁省": "辽宁", "吉林省": "吉林", "黑龙江省": "黑龙江",
    "上海市": "上海", "江苏省": "江苏", "浙江省": "浙江", "安徽省": "安徽",
    "福建省": "福建", "江西省": "江西", "山东省": "山东", "河南省": "河南",
    "湖北省": "湖北", "湖南省": "湖南", "广东省": "广东", "广西壮族自治区": "广西",
    "海南省": "海南", "重庆市": "重庆", "四川省": "四川", "贵州省": "贵州",
    "云南省": "云南", "西藏自治区": "西藏", "陕西省": "陕西", "甘肃省": "甘肃",
    "青海省": "青海", "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆",
    "台湾省": "台湾", "香港特别行政区": "香港", "澳门特别行政区": "澳门",
}

# ip2region 省份字段（如「江苏省」「北京」「内蒙古」「新疆」）→ 规范名：按前两字匹配，34 省两字唯一。
_PREFIX_TO_CANON = {name[:2]: name for name in CANONICAL_PROVINCES}

# 海外国家码→中文（常见来源；缺失时回退英文国名）
COUNTRY_CN = {
    "US": "美国", "CA": "加拿大", "GB": "英国", "UK": "英国", "DE": "德国", "FR": "法国",
    "IT": "意大利", "ES": "西班牙", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士", "RU": "俄罗斯",
    "JP": "日本", "KR": "韩国", "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "VN": "越南",
    "ID": "印度尼西亚", "PH": "菲律宾", "IN": "印度", "AU": "澳大利亚", "NZ": "新西兰",
    "BR": "巴西", "AR": "阿根廷", "MX": "墨西哥", "ZA": "南非", "EG": "埃及", "AE": "阿联酋",
    "SA": "沙特阿拉伯", "TR": "土耳其", "PL": "波兰", "UA": "乌克兰", "IE": "爱尔兰",
    "BE": "比利时", "AT": "奥地利", "NO": "挪威", "DK": "丹麦", "FI": "芬兰", "PT": "葡萄牙",
    "CZ": "捷克", "HU": "匈牙利", "GR": "希腊", "IL": "以色列", "KZ": "哈萨克斯坦",
    "PK": "巴基斯坦", "BD": "孟加拉国", "NP": "尼泊尔", "LA": "老挝", "KH": "柬埔寨",
    "MM": "缅甸", "MN": "蒙古", "LK": "斯里兰卡",
}


def normalize_province(prov: str) -> str | None:
    s = (prov or "").strip()
    if not s or s == "0":
        return None
    return _PREFIX_TO_CANON.get(s[:2])


# --- 离线 searcher 单例（v4/v6 各一，懒加载、线程安全）---
_lock = threading.Lock()
_state: dict = {
    "v4": {"loaded": False, "searcher": None, "error": ""},
    "v6": {"loaded": False, "searcher": None, "error": ""},
}


def _get_searcher(family: str = "v4"):
    slot = _state[family]
    if slot["loaded"]:
        return slot["searcher"]
    with _lock:
        if slot["loaded"]:
            return slot["searcher"]
        searcher = None
        try:
            import io as _io
            import ip2region.util as util
            import ip2region.searcher as xdb

            path = _xdb_path(family)
            handle = _io.open(path, "rb")
            try:
                util.verify(handle)
                header = util.load_header(handle)
                version = util.version_from_header(header)
                if version is None:
                    raise RuntimeError("无法识别 xdb 版本")
                buffer = util.load_content(handle)
            finally:
                handle.close()
            searcher = xdb.new_with_buffer(version, buffer)
        except Exception as exc:  # noqa: BLE001
            slot["error"] = str(exc)
            searcher = None
        slot["searcher"] = searcher
        slot["loaded"] = True
    return slot["searcher"]


def geoip_ready() -> bool:
    # 主库（v4）就绪即视为可用；v6 库缺失只降级 IPv6 归类，不整体报「未就绪」。
    return _get_searcher("v4") is not None


def geoip_status() -> dict:
    return {
        "ready": _get_searcher("v4") is not None,
        "error": _state["v4"].get("error", ""),
        "xdb": _xdb_path("v4"),
        "ready_v6": _get_searcher("v6") is not None,
        "error_v6": _state["v6"].get("error", ""),
        "xdb_v6": _xdb_path("v6"),
    }


def _is_locatable(ip: str):
    """返回可定位的 ipaddress 对象，否则 None（空/非法/内网/保留地址不计入分布）。"""
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_reserved
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return None
    return addr


def classify_ip(ip: str) -> dict:
    """把一个 IP 归类为 国内省份 / 海外国家 / 未知。

    返回:
      {"scope": "domestic", "province": "广东省"}
      {"scope": "overseas", "country_code": "US", "country": "美国"}
      {"scope": "unknown"}
    """
    addr = _is_locatable(ip)
    if addr is None:
        return {"scope": "unknown"}
    searcher = _get_searcher("v6" if addr.version == 6 else "v4")
    if searcher is None:
        return {"scope": "unknown"}
    try:
        region = searcher.search(ip)
    except Exception:  # noqa: BLE001
        return {"scope": "unknown"}
    if not region:
        return {"scope": "unknown"}
    parts = region.split("|")
    country = (parts[0] if len(parts) > 0 else "").strip()
    prov = (parts[1] if len(parts) > 1 else "").strip()
    code = (parts[4] if len(parts) > 4 else "").strip().upper()

    canon = normalize_province(prov)
    if canon:
        return {"scope": "domestic", "province": canon}
    if code == "CN" or country.startswith("中国"):
        # 中国 IP 但省份不可判定（极少数 0/空）：计入未知，不强行落到地图
        return {"scope": "unknown"}
    if not country or country == "0":
        return {"scope": "unknown"}
    cn = COUNTRY_CN.get(code) or country
    return {"scope": "overseas", "country_code": code or country, "country": cn}
