from __future__ import annotations

import contextlib
import contextvars
import difflib
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter as _call_perf_counter
from typing import Any, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml

from runtime_env import APPDATA_DIR
from ai_evidence import clean_evidence, exact_quote, PDF_WATERMARK_RE


CONFIG_PATH = Path(
    os.environ.get("MARX_AI_CONFIG_FILE")
    or (Path(__file__).resolve().parent / "config" / "ai.yaml")
).expanduser().resolve()
AI_OVERRIDE_PATH = APPDATA_DIR / "ai.override.yaml"

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
_MIMO_MIGRATION_ENABLED = str(os.environ.get("MIMO_MIGRATION_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
_MIMO_MODEL_ACCESS_ENABLED = str(os.environ.get("MIMO_MODEL_ACCESS_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
_MIMO_REFORM_CUTOFF = datetime(2026, 8, 14, 16, 0, 0, tzinfo=timezone.utc)


def _mimo_migration_live(now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (
        _MIMO_MODEL_ACCESS_ENABLED
        and _MIMO_MIGRATION_ENABLED
        and current.astimezone(timezone.utc) >= _MIMO_REFORM_CUTOFF
    )


def _internal_model(
    env_name: str, legacy_model: str, migrated_model: str, *, now: datetime | None = None,
) -> str:
    explicit = str(os.environ.get(env_name) or "").strip()
    if explicit:
        if explicit.lower().startswith("mimo-") and not _MIMO_MODEL_ACCESS_ENABLED:
            return legacy_model
        return explicit
    return migrated_model if _mimo_migration_live(now) else legacy_model


# 联想检索的线索扩展与重排是网站内部接地步骤，不属于用户选模权益。
# 固定使用 DeepSeek Flash 非思考；费用由站方承担（计费上下文由 app.py 标记）。
_LIGHT_TASK_DEFAULT_MODEL = DEFAULT_MODEL

# 联想检索「重排」与「扩展」两步都固定走更轻量的 flash 档。
# - 重排：只在已由 Python 接地定位的真实候选段落中判断匹配度并排序，对模型知识依赖低。
# - 扩展：生成检索线索（quotes/fragments/keywords）。原设计让它走主通道「强模型」以更好还原
#   译本措辞；但 2026-06-26 实测：主通道一旦是 deepseek-v4-pro（推理模型），扩展会①狂吐推理
#   token→单次~26s、叠加 3 次重试达~78s，超 Cloudflare ~100s 触发 "Failed to fetch"；②输出混入
#   推理致 JSON 解析失败→返回空线索（journal 长期 "expand yielded no usable clues"）。改走 flash
#   后实测 ~8-12s 且稳定解析出完整线索，故扩展也固定 flash（强模型在该结构化抽取任务上的措辞
#   优势并未兑现）。这是站方内部固定路由，不允许用户选模或环境变量把它切到付费强模型。
ASSOC_RERANK_MODEL = _LIGHT_TASK_DEFAULT_MODEL
ASSOC_EXPAND_MODEL = DEFAULT_MODEL
# 研究综述档的线索抽取也固定 Flash 非思考：研究长文的用户选模只作用于最终综述，
# 不作用于站方承担费用的接地检索步骤。
ASSOC_EXPAND_DEEP_MODEL = DEFAULT_MODEL
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_DEFAULT_MODEL = "mimo-v2.5"

# 智谱开放平台（bigmodel.cn）：OpenAI 兼容 /chat/completions，作为可选的「联网检索」通道，
# 与默认 DeepSeek 通道平行存在、互不影响；前台仅持有 ai_web 权限的用户可选。
ZHIPU_DEFAULT_MODEL = "glm-5.1"
ZHIPU_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_SEARCH_ENGINES = ("search_std", "search_pro", "search_pro_sogou", "search_pro_quark")

SUPPORTED_PROVIDERS = {"zai", "deepseek", "mimo"}
ZAI_PROVIDER_ALIASES = {"zai", "z.ai", "智谱", "zhipu", "glm"}
DEEPSEEK_PROVIDER_ALIASES = {"deepseek", "深度求索"}
MIMO_PROVIDER_ALIASES = {"mimo", "xiaomi", "小米"}

AI_EDITABLE_KEYS = (
    "provider",
    "model",
    "base_url",
    "api_key",
    "zhipu_api_key",
    "zhipu_model",
    "zhipu_base_url",
    "zhipu_search_engine",
    "zhipu_search_count",
    "zhipu_daily_token_limit",
    "request_timeout_seconds",
    "max_history_turns",
    "search_history_turns",
    "pdf_history_turns",
    "search_message_char_limit",
    "pdf_message_char_limit",
    "search_answer_max_tokens",
    "pdf_answer_max_tokens",
    "pdf_quick_answer_max_tokens",
    "pdf_selected_text_char_limit",
    "pdf_current_text_char_limit",
    "pdf_adjacent_excerpt_char_limit",
    "pdf_quick_selected_text_char_limit",
    "pdf_quick_current_text_char_limit",
    "pdf_quick_adjacent_excerpt_char_limit",
    "temperature",
)


LOGGER = logging.getLogger("marx.ai")


# Every real upstream call emits a preflight and a final event. app.py installs the sink that reserves/settles
# the user's monetary wallet and writes the provider-call ledger. ContextVars keep concurrent requests isolated.
_AI_CALL_SINK: Any = None
_AI_CALL_CONTEXT = contextvars.ContextVar("marx_ai_call_context", default={})


def configure_ai_call_sink(sink) -> None:
    global _AI_CALL_SINK
    _AI_CALL_SINK = sink


@contextlib.contextmanager
def ai_call_context(**metadata: Any) -> Iterator[None]:
    merged = {**dict(_AI_CALL_CONTEXT.get() or {}), **metadata}
    token = _AI_CALL_CONTEXT.set(merged)
    try:
        yield
    finally:
        _AI_CALL_CONTEXT.reset(token)


def _emit_ai_call_event(event: dict[str, Any]) -> Any:
    if _AI_CALL_SINK is None:
        return None
    return _AI_CALL_SINK({**dict(_AI_CALL_CONTEXT.get() or {}), **event})


def _usage_from_payload(payload: dict[str, Any] | None) -> dict[str, int]:
    usage = (payload or {}).get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cached = int(
        prompt_details.get("cached_tokens")
        or usage.get("prompt_cache_hit_tokens")
        or usage.get("cached_prompt_tokens")
        or 0
    )
    reasoning = int(completion_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0)
    return {
        "prompt_tokens": max(0, prompt),
        "cached_prompt_tokens": max(0, min(prompt, cached)),
        "completion_tokens": max(0, completion),
        "reasoning_tokens": max(0, min(completion, reasoning)),
    }


def _estimate_message_tokens(messages: Any) -> int:
    try:
        raw = json.dumps(messages or [], ensure_ascii=False)
    except (TypeError, ValueError):
        raw = str(messages or "")
    return max(1, (len(raw) + 1) // 2)


class AIServiceError(RuntimeError):
    pass


class _ReasoningOnlyResponse(Exception):
    """流式响应全程只有思维链、没有正文（推理模型把 max_tokens 烧在思考上）。

    仅在 ai.py 内部流转：由 _stream_chat_once 抛出、chat_complete_stream 捕获后「关思考」重试一次。
    绝不外泄给路由层——思维链任何情况下都不作为答案下发给读者。
    """


# ---------------------------------------------------------------------------
# AI 出网调用「并发闸」——护住 waitress 线程池
#
# 背景（2026-06-26 宕机根因）：上游（DeepSeek/智谱）变慢时，每个 AI 请求会把一个线程
# 死等在下面的 urlopen 上（最长 request_timeout_seconds，默认 120s）。8 线程的 waitress
# 很快被 AI 占满 → 首页/页面图/健康检查全部拿不到线程 → 整站冻结约 50 分钟。
#
# 对策：交互式 AI 与研究长任务**分池**。旧版把全站所有 AI 调用塞进同一个 5 名额闸门；
# 两篇研究综述的前置检索与长文生成就可能把快速问答一并挤成“访问量较大”。现在研究链路
# 固定使用独立小池，绝不占用交互池；快速问答等交互调用有自己的较大并发池。
#
# 刻意**不压缩任何超时**：研究型检索本就耗时长，闸门只限「同时并发数」、不限「单次时长」。
# 正常负载下短请求可在闸门前小幅等待，而不是 3 秒即失败。旋钮均可由环境变量覆盖。
_AI_HTTP_INTERACTIVE_CONCURRENCY = max(
    1,
    int(
        os.environ.get("MARX_AI_INTERACTIVE_CONCURRENCY")
        or os.environ.get("MARX_AI_CONCURRENCY")
        or "10"
    ),
)
_AI_HTTP_RESEARCH_CONCURRENCY = max(
    1, int(os.environ.get("MARX_RESEARCH_HTTP_CONCURRENCY", "2") or "2")
)
_AI_HTTP_ACQUIRE_TIMEOUT = max(
    0.0, float(os.environ.get("MARX_AI_ACQUIRE_TIMEOUT_SECONDS", "12") or "12")
)
_AI_HTTP_INTERACTIVE_SEMAPHORE = threading.BoundedSemaphore(_AI_HTTP_INTERACTIVE_CONCURRENCY)
_AI_HTTP_RESEARCH_SEMAPHORE = threading.BoundedSemaphore(_AI_HTTP_RESEARCH_CONCURRENCY)
_AI_HTTP_WORKLOAD = contextvars.ContextVar("marx_ai_http_workload", default="interactive")


@contextlib.contextmanager
def research_ai_http_context() -> Iterator[None]:
    """Route every nested outbound AI call to the research-only pool."""
    token = _AI_HTTP_WORKLOAD.set("research")
    try:
        yield
    finally:
        _AI_HTTP_WORKLOAD.reset(token)


@contextlib.contextmanager
def _ai_http_slot() -> Iterator[None]:
    """占用一个 AI 出网并发名额；满闸则在超时后抛 AIServiceError 判忙。

    用 ``with _ai_http_slot(): ...`` 包住 urlopen。名额在退出 with 时释放，对流式调用
    意味着「整段流期间」占用一个名额（一次活跃 AI 会话 = 一个名额），符合预期。
    """
    research = _AI_HTTP_WORKLOAD.get() == "research"
    semaphore = _AI_HTTP_RESEARCH_SEMAPHORE if research else _AI_HTTP_INTERACTIVE_SEMAPHORE
    acquired = semaphore.acquire(timeout=_AI_HTTP_ACQUIRE_TIMEOUT)
    if not acquired:
        raise AIServiceError("AI 当前访问量较大，请稍后重试。")
    try:
        yield
    finally:
        semaphore.release()


# 研究综述（后台长任务）的独立并发闸：一篇综述要顺序跑多次子调用、整篇可达数分钟。若与上面的交互式
# 5 名额共用，几篇并发综述就能把吉祥物/问答全部判忙。这里单限「同时进行的综述篇数」（默认 2）：整篇
# 综述入口处占一个名额，故研究综述至多同时占用 2 个交互名额，其余恒为交互式 AI 保留。可经环境变量调整。
_RESEARCH_REVIEW_CONCURRENCY = max(1, int(os.environ.get("MARX_RESEARCH_CONCURRENCY", "2") or "2"))
_RESEARCH_REVIEW_SEMAPHORE = threading.BoundedSemaphore(_RESEARCH_REVIEW_CONCURRENCY)


# ---------------------------------------------------------------------------
# 联网检索的「入口提纯」与「出口过滤」
#
# 智谱联网把检索词原样发给搜索引擎、又不过滤返回结果。而用户的口语问句
# （「为什么…一点感觉都没有怎么回事」这类）恰好是中文医疗/养生/两性内容农场重点
# SEO 的长尾句式，于是这些垃圾站霸屏检索结果、混进答案与来源卡。两道闸一起治：
#   入口：zhipu_search_query 剥掉口语外壳 + 必要时补马列领域锚点；
#   出口：_filter_web_sources 按内容农场域名/站点名 + “医疗信号且无主题锚点”剔除。
# 词表均为模块级常量，便于日后增补（也为后续做成后台可配预留位置）。
# ---------------------------------------------------------------------------

# 马列主题「强锚点」：明确的专名/概念，命中即可确信与本站主题相关（几乎无歧义）。
# 用于查询提纯（命中则无需再补领域词）与结果过滤（命中则不按医疗信号误删）。
_MARX_CORE_ANCHORS = (
    "马克思", "恩格斯", "列宁", "毛泽东", "斯大林", "资本论", "宣言",
    "政治经济学", "剩余价值", "唯物", "辩证", "无产阶级", "资产阶级",
    "共产主义", "社会主义", "拜物教", "异化", "生产关系", "生产力",
    "阶级", "意识形态", "社会形态", "费尔巴哈", "黑格尔", "空想社会",
    "历史唯物", "辩证法", "劳动力", "地租", "资本主义", "封建", "辩证唯物",
)
# 结果过滤额外容忍的「软锚点」泛词：单独不足以证明 on-topic，但与正文共现时不应误删。
_MARX_SOFT_ANCHORS = _MARX_CORE_ANCHORS + (
    "商品", "价值", "劳动", "经济", "哲学", "革命", "国家", "资本", "货币",
    "理论", "思想", "社会", "历史", "政治",
)

# 已知中文医疗健康/养生/两性内容农场与问答站点名（智谱来源里的 media 字段）。
_WEB_SOURCE_DENY_SITES = (
    "医联媒体", "我爱康", "寻医问药", "有问必答", "39健康", "39问医生",
    "快速问医生", "飞华健康", "民福康", "大众养生", "三九养生", "复禾健康",
    "妙手医生", "放心医苑", "求医网", "健康一线", "家庭医生在线", "好大夫在线",
    "微医", "新浪医药",
)
# 同类站点的域名兜底（站点名抓不到时按链接域名再拦一道）。
_WEB_SOURCE_DENY_DOMAINS = (
    "120ask.com", "xywy.com", "39.net", "iiyi.com", "haodf.com",
    "familydoctor.com.cn", "fh21.com.cn", "vodjk.com", "9939.com",
    "myzx.cn", "qiuyi.cn", "fx120.net", "jiankang.com", "5kang.com",
    "club.xywy.com", "yilianmeiti.com",
)
# 医疗/两性/养生类「强信号词」：标题或摘要含这类词、且通篇不含任何马列主题锚点时，
# 判为与本站主题无关的内容农场垃圾。只有“有信号且无锚点”才删，避免误伤正常马列文本。
_WEB_SOURCE_JUNK_TERMS = (
    "做爱", "性生活", "性功能", "性欲", "早泄", "阳痿", "射精", "勃起",
    "壮阳", "月经", "姨妈", "例假", "妇科", "白带", "私处", "下体",
    "怀孕", "备孕", "排卵", "前列腺", "尿频", "吃什么药", "挂号",
    "病因", "确诊", "就医", "减肥", "丰胸", "祛痘", "植发", "整形",
    "妇产", "男科", "性病",
)

# 口语问句的「求解外壳」：句首疑问/求助词。检索噪音，剥掉让查询更接近“关键词”。
_QUERY_HEAD_NOISE = re.compile(
    r"^(?:请问|麻烦问[一下]*|我想问[一下问]*|想请教[一下]*|谁能(?:告诉我)?|"
    r"有没有人?知道|帮我?查[一下]*|帮忙[查问]*|顺便问[一下]*|我想了解[一下]*|"
    r"为什么|为啥|为何|怎么样|怎样|咋样|咋|如何|怎么)+[\s，,、:：]*"
)
# 句尾追问/语气词（含医疗农场最爱的“怎么回事/是什么原因/怎么办”长尾收束）。
_QUERY_TAIL_NOISE = re.compile(
    r"[\s，,、]*(?:是?怎么回事|是什么原因|什么原因|是什么意思|什么意思|"
    r"该?怎么办|怎么解决|怎么样|怎样|咋样|求解答|求解|求助|呢|吗|吧|啊|呀|"
    r"嘛|啦|哦|哈)+[\s?？。.!！~～]*$"
)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)

# 联想检索线索抽取的进程内缓存：DeepSeek 即便 temperature=0 也并非严格确定，
# 缓存“同一输入→同一线索”，既保证可复现（同输入同结果），又省去重复 API 调用。
_ASSOC_EXPAND_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_ASSOC_EXPAND_CACHE_MAX = 256


def _extract_json_object(text: str):
    """从模型回复中宽容地解析出 JSON 对象/数组。

    依次尝试：剥离 Markdown 代码围栏 → 直接 json.loads → 截取首个配平的 ``{...}``/``[...]``
    再解析。任何失败都返回 ``{}``（解析不到结构时的安全降级），绝不向上抛异常。
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    # 从最先出现的 { 或 [ 起，做“括号平衡 + 字符串感知”扫描，截出第一个完整 JSON 值。
    openers = [pos for pos in (raw.find("{"), raw.find("[")) if pos != -1]
    if not openers:
        return {}
    start = min(openers)
    closer = "}" if raw[start] == "{" else "]"
    opener = raw[start]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except (json.JSONDecodeError, TypeError):
                    return {}
    return {}


@dataclass(frozen=True)
class AIConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    mimo_api_key: str
    mimo_base_url: str
    mimo_model: str
    mimo_enabled: bool
    zhipu_api_key: str
    zhipu_model: str
    zhipu_base_url: str
    zhipu_search_engine: str
    zhipu_search_count: int
    zhipu_daily_token_limit: int
    zhipu_enabled: bool
    request_timeout_seconds: int
    max_history_turns: int
    search_history_turns: int
    pdf_history_turns: int
    search_message_char_limit: int
    pdf_message_char_limit: int
    search_answer_max_tokens: int
    pdf_answer_max_tokens: int
    pdf_quick_answer_max_tokens: int
    pdf_selected_text_char_limit: int
    pdf_current_text_char_limit: int
    pdf_adjacent_excerpt_char_limit: int
    pdf_quick_selected_text_char_limit: int
    pdf_quick_current_text_char_limit: int
    pdf_quick_adjacent_excerpt_char_limit: int
    temperature: float
    enabled: bool
    problems: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "mimo_enabled": self.mimo_enabled,
            "mimo_base_url": self.mimo_base_url,
            "mimo_model": self.mimo_model,
            "zhipu_enabled": self.zhipu_enabled,
            "zhipu_model": self.zhipu_model,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_history_turns": self.max_history_turns,
            "search_history_turns": self.search_history_turns,
            "pdf_history_turns": self.pdf_history_turns,
            "search_answer_max_tokens": self.search_answer_max_tokens,
            "pdf_answer_max_tokens": self.pdf_answer_max_tokens,
            "pdf_quick_answer_max_tokens": self.pdf_quick_answer_max_tokens,
            "temperature": self.temperature,
            "problems": list(self.problems),
        }

    def to_edit_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "zhipu_api_key": self.zhipu_api_key,
            "zhipu_model": self.zhipu_model,
            "zhipu_base_url": self.zhipu_base_url,
            "zhipu_search_engine": self.zhipu_search_engine,
            "zhipu_search_count": self.zhipu_search_count,
            "zhipu_daily_token_limit": self.zhipu_daily_token_limit,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_history_turns": self.max_history_turns,
            "search_history_turns": self.search_history_turns,
            "pdf_history_turns": self.pdf_history_turns,
            "search_message_char_limit": self.search_message_char_limit,
            "pdf_message_char_limit": self.pdf_message_char_limit,
            "search_answer_max_tokens": self.search_answer_max_tokens,
            "pdf_answer_max_tokens": self.pdf_answer_max_tokens,
            "pdf_quick_answer_max_tokens": self.pdf_quick_answer_max_tokens,
            "pdf_selected_text_char_limit": self.pdf_selected_text_char_limit,
            "pdf_current_text_char_limit": self.pdf_current_text_char_limit,
            "pdf_adjacent_excerpt_char_limit": self.pdf_adjacent_excerpt_char_limit,
            "pdf_quick_selected_text_char_limit": self.pdf_quick_selected_text_char_limit,
            "pdf_quick_current_text_char_limit": self.pdf_quick_current_text_char_limit,
            "pdf_quick_adjacent_excerpt_char_limit": self.pdf_quick_adjacent_excerpt_char_limit,
            "temperature": self.temperature,
        }


@dataclass(frozen=True)
class AIAnswer:
    answer_markdown: str
    sources: list[dict[str, str]]
    used_web: bool
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "answer_markdown": self.answer_markdown,
            "sources": self.sources,
            "used_web": self.used_web,
            "warnings": self.warnings,
        }


def _load_yaml_dict(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _pick_raw(env_name: str, payload: dict[str, Any], key: str, default: Any) -> Any:
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return env_value
    if key in payload:
        return payload[key]
    return default


def _pick_str(env_name: str, payload: dict[str, Any], key: str, default: str) -> str:
    return str(_pick_raw(env_name, payload, key, default) or "").strip()


def _pick_int(env_name: str, payload: dict[str, Any], key: str, default: int) -> int:
    raw = _pick_raw(env_name, payload, key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _pick_float(env_name: str, payload: dict[str, Any], key: str, default: float) -> float:
    raw = _pick_raw(env_name, payload, key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _pick_bool(env_name: str, payload: dict[str, Any], key: str, default: bool) -> bool:
    raw = _pick_raw(env_name, payload, key, default)
    return _as_bool(raw, default)


def _normalize_provider(raw: Any) -> str:
    provider = str(raw or "").strip().lower()
    if provider in ZAI_PROVIDER_ALIASES:
        return "zai"
    if provider in DEEPSEEK_PROVIDER_ALIASES:
        return "deepseek"
    if provider in MIMO_PROVIDER_ALIASES:
        return "mimo"
    return provider or DEFAULT_PROVIDER


def load_ai_config(
    config_path: Path | None = None,
    override_path: Path | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> AIConfig:
    base_payload = _load_yaml_dict(config_path or CONFIG_PATH)
    override_payload = _load_yaml_dict(override_path or AI_OVERRIDE_PATH)
    payload = {**base_payload, **override_payload, **(extra_payload or {})}

    provider = _normalize_provider(
        _pick_str("APP_AI_PROVIDER", payload, "provider", DEFAULT_PROVIDER) or DEFAULT_PROVIDER
    )
    model = _pick_str("APP_AI_MODEL", payload, "model", DEFAULT_MODEL) or DEFAULT_MODEL
    # MiMo 入口总闸关闭时，即使旧环境仍残留 MiMo 主通道配置，也不能让它重新成为实际路由。
    if provider == "mimo" and not _MIMO_MODEL_ACCESS_ENABLED:
        provider = DEFAULT_PROVIDER
    if model.lower().startswith("mimo-") and not _MIMO_MODEL_ACCESS_ENABLED:
        model = DEFAULT_MODEL
    base_url = (
        str(
            os.environ.get("ZAI_BASE_URL")
            or os.environ.get("APP_AI_BASE_URL")
            or payload.get("base_url")
            or (DEEPSEEK_BASE_URL if provider == "deepseek" else MIMO_BASE_URL if provider == "mimo" else DEFAULT_BASE_URL)
        )
        .strip()
        .rstrip("/")
    )
    api_key = (
        str(
            os.environ.get("ZAI_API_KEY")
            or os.environ.get("APP_AI_API_KEY")
            or payload.get("api_key")
            or ""
        )
        .strip()
    )
    # MiMo key is deployment-only by design: it is never read from or written to YAML/database overrides.
    mimo_api_key = str(os.environ.get("MIMO_API_KEY") or "").strip()
    mimo_base_url = str(os.environ.get("MIMO_BASE_URL") or MIMO_BASE_URL).strip().rstrip("/")
    mimo_model = str(os.environ.get("MIMO_MODEL") or MIMO_DEFAULT_MODEL).strip() or MIMO_DEFAULT_MODEL
    # 智谱 GLM（可联网检索）的平行通道：留空 zhipu_api_key 即关闭，对 DeepSeek 主通道零影响。
    zhipu_api_key = (
        str(
            os.environ.get("ZHIPU_API_KEY")
            or os.environ.get("APP_AI_ZHIPU_API_KEY")
            or payload.get("zhipu_api_key")
            or ""
        )
        .strip()
    )
    zhipu_model = _pick_str("APP_AI_ZHIPU_MODEL", payload, "zhipu_model", ZHIPU_DEFAULT_MODEL) or ZHIPU_DEFAULT_MODEL
    zhipu_base_url = (
        str(
            os.environ.get("ZHIPU_BASE_URL")
            or os.environ.get("APP_AI_ZHIPU_BASE_URL")
            or payload.get("zhipu_base_url")
            or ZHIPU_DEFAULT_BASE_URL
        )
        .strip()
        .rstrip("/")
    )
    zhipu_search_engine = _pick_str(
        "APP_AI_ZHIPU_SEARCH_ENGINE", payload, "zhipu_search_engine", ZHIPU_SEARCH_ENGINES[0]
    )
    zhipu_search_count = _pick_int("APP_AI_ZHIPU_SEARCH_COUNT", payload, "zhipu_search_count", 5)
    # 智谱通道每用户每日 token 子配额（估算口径，约 8~12 次阅读讲解）；0＝不限。
    # GLM-5.1 输出价约 ¥24/百万 tokens + 联网按次计费，须有独立闸门控制成本。
    zhipu_daily_token_limit = _pick_int(
        "APP_AI_ZHIPU_DAILY_TOKEN_LIMIT", payload, "zhipu_daily_token_limit", 30000
    )

    max_history_turns = _pick_int("APP_AI_MAX_HISTORY_TURNS", payload, "max_history_turns", 12)
    request_timeout_seconds = _pick_int(
        "APP_AI_REQUEST_TIMEOUT_SECONDS",
        payload,
        "request_timeout_seconds",
        120,
    )

    search_history_turns = _pick_int(
        "APP_AI_SEARCH_HISTORY_TURNS",
        payload,
        "search_history_turns",
        max_history_turns,
    )
    pdf_history_turns = _pick_int(
        "APP_AI_PDF_HISTORY_TURNS",
        payload,
        "pdf_history_turns",
        max_history_turns,
    )
    search_message_char_limit = _pick_int(
        "APP_AI_SEARCH_MESSAGE_CHAR_LIMIT",
        payload,
        "search_message_char_limit",
        4000,
    )
    pdf_message_char_limit = _pick_int(
        "APP_AI_PDF_MESSAGE_CHAR_LIMIT",
        payload,
        "pdf_message_char_limit",
        3200,
    )
    search_answer_max_tokens = _pick_int(
        "APP_AI_SEARCH_ANSWER_MAX_TOKENS",
        payload,
        "search_answer_max_tokens",
        32768,
    )
    pdf_answer_max_tokens = _pick_int(
        "APP_AI_PDF_ANSWER_MAX_TOKENS",
        payload,
        "pdf_answer_max_tokens",
        32768,
    )
    pdf_quick_answer_max_tokens = _pick_int(
        "APP_AI_PDF_QUICK_ANSWER_MAX_TOKENS",
        payload,
        "pdf_quick_answer_max_tokens",
        16384,
    )
    pdf_selected_text_char_limit = _pick_int(
        "APP_AI_PDF_SELECTED_TEXT_CHAR_LIMIT",
        payload,
        "pdf_selected_text_char_limit",
        1600,
    )
    pdf_current_text_char_limit = _pick_int(
        "APP_AI_PDF_CURRENT_TEXT_CHAR_LIMIT",
        payload,
        "pdf_current_text_char_limit",
        2800,
    )
    pdf_adjacent_excerpt_char_limit = _pick_int(
        "APP_AI_PDF_ADJACENT_EXCERPT_CHAR_LIMIT",
        payload,
        "pdf_adjacent_excerpt_char_limit",
        280,
    )
    pdf_quick_selected_text_char_limit = _pick_int(
        "APP_AI_PDF_QUICK_SELECTED_TEXT_CHAR_LIMIT",
        payload,
        "pdf_quick_selected_text_char_limit",
        700,
    )
    pdf_quick_current_text_char_limit = _pick_int(
        "APP_AI_PDF_QUICK_CURRENT_TEXT_CHAR_LIMIT",
        payload,
        "pdf_quick_current_text_char_limit",
        1400,
    )
    pdf_quick_adjacent_excerpt_char_limit = _pick_int(
        "APP_AI_PDF_QUICK_ADJACENT_EXCERPT_CHAR_LIMIT",
        payload,
        "pdf_quick_adjacent_excerpt_char_limit",
        160,
    )
    temperature = _pick_float(
        "APP_AI_TEMPERATURE",
        payload,
        "temperature",
        0.25,
    )

    problems: list[str] = []
    if provider not in SUPPORTED_PROVIDERS:
        problems.append(f"当前仅支持 {', '.join(sorted(SUPPORTED_PROVIDERS))} 提供方，收到 provider={provider!r}。")
    if not api_key and provider != "mimo":
        problems.append("未配置主通道 API Key，AI 对话功能已禁用。")
    if provider == "mimo" and not mimo_api_key:
        problems.append("未配置 MIMO_API_KEY，MiMo 通道已禁用。")
    if zhipu_search_engine not in ZHIPU_SEARCH_ENGINES:
        problems.append(
            f"智谱联网引擎仅支持 {', '.join(ZHIPU_SEARCH_ENGINES)}，"
            f"收到 {zhipu_search_engine!r}，已回退为 {ZHIPU_SEARCH_ENGINES[0]}。"
        )
        zhipu_search_engine = ZHIPU_SEARCH_ENGINES[0]
    zhipu_search_count = max(1, min(20, zhipu_search_count))
    zhipu_daily_token_limit = max(0, zhipu_daily_token_limit)
    if request_timeout_seconds < 5:
        problems.append("request_timeout_seconds 过小，已回退为 120 秒。")
        request_timeout_seconds = 120
    if max_history_turns < 1:
        problems.append("max_history_turns 必须大于等于 1，已回退为 12。")
        max_history_turns = 12
    search_history_turns = max(1, search_history_turns)
    pdf_history_turns = max(1, pdf_history_turns)
    search_message_char_limit = max(500, search_message_char_limit)
    pdf_message_char_limit = max(500, pdf_message_char_limit)
    # 旧 ai.override.yaml 里仍可能保存 1800/900。把“完整输出优先”设为代码级下限，
    # 避免历史后台配置在重启后悄悄把 MiMo/DeepSeek 的 completion 再压回截断值。
    search_answer_max_tokens = max(32768, search_answer_max_tokens)
    pdf_answer_max_tokens = max(32768, pdf_answer_max_tokens)
    pdf_quick_answer_max_tokens = max(16384, pdf_quick_answer_max_tokens)
    pdf_selected_text_char_limit = max(200, pdf_selected_text_char_limit)
    pdf_current_text_char_limit = max(600, pdf_current_text_char_limit)
    pdf_adjacent_excerpt_char_limit = max(60, pdf_adjacent_excerpt_char_limit)
    pdf_quick_selected_text_char_limit = max(120, pdf_quick_selected_text_char_limit)
    pdf_quick_current_text_char_limit = max(300, pdf_quick_current_text_char_limit)
    pdf_quick_adjacent_excerpt_char_limit = max(60, pdf_quick_adjacent_excerpt_char_limit)
    temperature = min(1.5, max(0.0, temperature))

    enabled = provider in SUPPORTED_PROVIDERS and bool(mimo_api_key if provider == "mimo" else api_key)
    mimo_enabled = bool(mimo_api_key)
    zhipu_enabled = bool(zhipu_api_key)

    return AIConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        mimo_api_key=mimo_api_key,
        mimo_base_url=mimo_base_url,
        mimo_model=mimo_model,
        mimo_enabled=mimo_enabled,
        zhipu_api_key=zhipu_api_key,
        zhipu_model=zhipu_model,
        zhipu_base_url=zhipu_base_url,
        zhipu_search_engine=zhipu_search_engine,
        zhipu_search_count=zhipu_search_count,
        zhipu_daily_token_limit=zhipu_daily_token_limit,
        zhipu_enabled=zhipu_enabled,
        request_timeout_seconds=request_timeout_seconds,
        max_history_turns=max_history_turns,
        search_history_turns=search_history_turns,
        pdf_history_turns=pdf_history_turns,
        search_message_char_limit=search_message_char_limit,
        pdf_message_char_limit=pdf_message_char_limit,
        search_answer_max_tokens=search_answer_max_tokens,
        pdf_answer_max_tokens=pdf_answer_max_tokens,
        pdf_quick_answer_max_tokens=pdf_quick_answer_max_tokens,
        pdf_selected_text_char_limit=pdf_selected_text_char_limit,
        pdf_current_text_char_limit=pdf_current_text_char_limit,
        pdf_adjacent_excerpt_char_limit=pdf_adjacent_excerpt_char_limit,
        pdf_quick_selected_text_char_limit=pdf_quick_selected_text_char_limit,
        pdf_quick_current_text_char_limit=pdf_quick_current_text_char_limit,
        pdf_quick_adjacent_excerpt_char_limit=pdf_quick_adjacent_excerpt_char_limit,
        temperature=temperature,
        enabled=enabled,
        problems=tuple(problems),
    )


def save_ai_overrides(values: dict[str, Any], path: Path | None = None) -> None:
    target = path or AI_OVERRIDE_PATH
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {key: values[key] for key in AI_EDITABLE_KEYS if key in values}
    target.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def reset_ai_overrides(path: Path | None = None) -> None:
    target = path or AI_OVERRIDE_PATH
    if target.exists():
        target.unlink()


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# 接地作答（随心问开启引文检索）天然更长：含逐字引文 + 准确出处 + 区分性分析。通用对话的
# search_answer_max_tokens 可能被管理员/override 压低以控成本，会把这类较长回答截在半句。
# 故接地路径用一个独立下限：既保证完整收尾，又不至于失控。
# 注入原文已从 4 条增至 10 条，下限从 1300 一路上调：2400 仍有较长的接地回答撞顶被截在半句，
# 2026-07-03 上调到 4000；2026-07-07 提示词从「择 3-6 条、简明扼要」改为「充分运用引文、逐条
# 展开阐释（800～1500 字基准）」，回答随之变长，下限同步上调到 6000。
# **2026-07-31 再上调到 10000**：关键新认识——deepseek-v4-flash/pro 都是推理模型，max_tokens 是
# 「思考 + 正文」的合计上限（usage.completion_tokens_details.reasoning_tokens 计在内）。语料涨到
# 19.6 万页后，10 条接地原文能让 flash 的思考独吞 5000+ token（实测 mt=6000 → finish_reason=length、
# reasoning_tokens=5260），正文被挤成空——正是「思维链上屏」事故的直接诱因。10000 给思考留出余量
# 后仍能写完整篇正文。上游已实测接受 mt=10000（flash/pro 均 finish_reason=stop）。
# 万一某通道拒绝，answer_search_chat 内有 token 阶梯逐级回退（10000 → 6000 → 4000，均为历史
# 验证过的生产值），绝不因上调而让接地问答整体失败。注意这是「单次回答输出上限的下限」，
# 与「各档用户每日/每周 token 额度」是两套机制：本下限对所有用户一致，放宽它不会改动
# _require_ai_quota_or_raise 的分档额度——每次回答的 token 仍照常计入该用户的周池，额度限制原样保留。
# 也不等于每次都花 10000：它只是天花板，实际用量由模型自己收笔决定（正常接地回答 1000～1600）。
GROUNDED_ANSWER_MIN_TOKENS = 65536
GROUNDED_ANSWER_MID_TOKENS = 32768
GROUNDED_ANSWER_FALLBACK_TOKENS = 16000

# 研究型检索的「综述」是长文：需独立的高输出上限(不受被压低的 search_answer_max_tokens 限制)。
# 第一要务是让模型自然写完完整文章；续写和重写也给足余量，而不是后端硬凑小结。
RESEARCH_REVIEW_MAX_TOKENS = 98304
RESEARCH_REVIEW_CONTINUATION_MAX_TOKENS = 32768
RESEARCH_REVIEW_REWRITE_MAX_TOKENS = 98304
RESEARCH_REVIEW_CONTINUATION_ATTEMPTS = 3
# “5000 字以上”同时落实为生成提示与确定性完成门槛。目标略高于门槛，避免模型刚好卡在边缘；
# 低于 5000 个中文汉字即进入实质性续写，不以重复观点或拉长引文凑数。
RESEARCH_REVIEW_TARGET_CJK_CHARS = 5600
RESEARCH_REVIEW_MIN_CJK_CHARS = 5000

# —— 研究综述「整篇挂钟预算」(兜底防呆，非 CF 超时约束) ——
# 研究综述用**非流式**生成(逐 token 流式版曾翻车回退，见记忆)，单篇可达上万 token、最多 ~5 次顺序调用。
# 它经 **SSE 心跳保活**端点对外返回(app._sse_run_with_heartbeat)：响应先吐字节、其间每隔几秒发心跳，
# 喂住 Cloudflare ~100s「源站首字节」计时器，故**不再受 CF 100s 限**——生成可安心写完整全长综述。
# 这里的总预算只作**兜底防呆**(防某次模型调用卡死把线程长期拖住)：每次发起修复/续写/重写前校验剩余预算，
# 不足就带已成文返回；单次调用再按剩余预算压 http_timeout。默认 300s(容纳约 5000 字单轮 + 一次续写)，
# 可经环境变量 RESEARCH_REVIEW_BUDGET_SECONDS 调整。
try:
    _RR_BUDGET_RAW = int((os.environ.get("RESEARCH_REVIEW_BUDGET_SECONDS") or "600").strip() or "600")
except ValueError:
    _RR_BUDGET_RAW = 600
RESEARCH_REVIEW_TOTAL_BUDGET_SECONDS = max(20, _RR_BUDGET_RAW)
# 追加一轮(修复/续写/重写)前要求的最小剩余预算：不足则不再发起，避免最后一轮把总时长拖得过长。
RESEARCH_REVIEW_FOLLOWUP_MIN_HEADROOM_SECONDS = 22
# 单次模型调用 HTTP 超时的下限：预算将尽时也别用过小的超时立刻判错(给最后一次机会留点余地)。
RESEARCH_REVIEW_MIN_CALL_TIMEOUT_SECONDS = 8
# 研究综述「生成调用」单次 HTTP 超时——刻意与全局 request_timeout_seconds(120s) 解耦。
# 综述生成跑在 SSE 心跳保活的后台线程里(app._sse_run_with_heartbeat)：CF 边缘看到的是主生成器每 12s
# 一条心跳、而非这条沉默的 DeepSeek 调用，故它可安全地远超 CF ~100s 首字节超时(5000 字单轮约需 ~150-200s)。
# 关键：只放宽**生成调用**；expand/rerank 等跑在心跳「之前」的同步 AI 调用仍用全局 120s 快速失败
# (它们慢就会顶在 CF 100s 之前，必须 fail-fast 兜底)，绝不可跟着放宽。
RESEARCH_REVIEW_CALL_TIMEOUT_SECONDS = 480
_RESEARCH_REVIEW_START_MARKERS = (
    "【综述正文开始】",
    "【正式综述开始】",
    "正式综述如下：",
    "正式综述:",
    "正式综述：",
    "综述正文如下：",
    "综述正文:",
    "综述正文：",
)
_RESEARCH_REVIEW_END_MARKERS = (
    "【综述正文结束】",
    "【正式综述结束】",
    "（综述完）",
    "(综述完)",
)
_RESEARCH_REVIEW_THINK_BLOCK_RE = re.compile(
    r"<(?:think|thinking|analysis)>.*?</(?:think|thinking|analysis)>",
    re.I | re.S,
)
_RESEARCH_REVIEW_LEAK_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"思考过程|推理过程|分析过程|内部思考|我的思考|我的分析|解题思路|写作思路|"
    r"首先[，,]?\s*我(?:需要|要)|我需要|我们需要|用户要求|题目要求|提示词要求|"
    r"好的|下面是|以下是|我将"
    r")[：:，,。\s]",
    re.I,
)
_RESEARCH_REVIEW_LEAK_MARKERS = (
    "思考过程",
    "推理过程",
    "分析过程",
    "内部思考",
    "我的思考",
    "我的分析",
    "解题思路",
    "写作思路",
    "首先，我需要",
    "首先我需要",
    "我需要先",
    "我们需要先",
    "用户要求",
    "题目要求",
    "提示词要求",
)
# 反伪造出处（接地问答与研究综述共用）：真实出处只走 [N] 编号（前端据结构化 citations 渲染真实命中）。
# 模型偶发在正文里自造脚注/尾注——「① 参见《马克思恩格斯文集》第1卷，第163页」，甚至自证
# 「（此段为学理补充，非本次检索原文）」。这类自拟书名＋卷次＋页码的出处一律不可信、须剔除。
# 合法的行内引证「（[1]《…》第X页）」以方括号编号起头、不受影响。
_FABRICATED_CITE_LINE_RES = (
    # 脚注/尾注定义行：以圈码 ①-⑳ 或「注N」起头，随后（可带「参见」）给出《书名》＋第N页/卷。
    re.compile(
        r"^\s*(?:[①-⑳]|[（(]?\s*注\s*\d*\s*[）)]?)\s*"
        r"(?:参见|参阅|详见|另见|见)?\s*《[^》]+》.{0,40}?第?\s*\d+\s*[页卷]"
    ),
    # 把「书名号出处」与「脱离本次检索」的自证并置的行（自认非检索来源却仍给出处）。
    re.compile(
        r"(?:非本次检索|非检索原文|未检索到|检索之外|学理补充|超出本次检索|检索原文之外)[^\n]*《[^》]+》"
        r"|《[^》]+》[^\n]*(?:非本次检索|非检索原文|未检索到|检索之外|学理补充|超出本次检索|检索原文之外)"
    ),
)

# 快速回答的直接引文兜底校验。只处理“带本站 [N] 编号、且能在对应接地原文中逐字归一匹配”的内容：
# 无法可靠匹配的一律原样保留，避免为了修格式而误伤模型的分析、联网资料或普通引号用法。
_GROUNDED_INLINE_QUOTE_RE = re.compile(
    r'(?P<open>[“「『"])(?P<quote>[^“”「」『』"\n]{8,}?)(?P<close>[”」』"])'
    r'(?P<refs>\s*(?:\[\d+\]\s*)+)'
)
_GROUNDING_REF_RE = re.compile(r"\[(\d+)\]")
_GROUNDING_REF_ONLY_RE = re.compile(r"^\s*(?:\[\d+\]\s*)+$")
_DIRECT_QUOTE_ENDERS = "。！？!?"
_DIRECT_QUOTE_CLOSERS = "”’」』）》】）)]"
_GENERIC_SOURCE_PHRASE_RE = re.compile(
    r"(?P<phrase>(?:有|相关|上述|这些|该)?文献(?:指出|显示|表明|认为|提到)|"
    r"(?:相关|上述|这些|所给|所提供的)?材料(?:指出|显示|表明|认为)|"
    r"(?:原文|文本)(?:指出|显示|表明|认为|强调|提出|提到)|检索结果(?:显示|表明)|根据(?:上述|所给|所提供的)?(?:材料|文献)|本次检索)"
    r"(?P<suffix>\s*[：:，,]?\s*)"
)
_ATTRIBUTION_NAMES = (
    "马克思恩格斯", "马克思和恩格斯", "恩格斯和马克思", "马克思与恩格斯", "恩格斯与马克思",
    "马克思、恩格斯", "恩格斯、马克思", "马克思", "恩格斯", "列宁", "斯大林", "毛泽东",
    "刘少奇", "周恩来", "邓小平", "江泽民", "胡锦涛", "习近平", "陈独秀", "李大钊",
)
_ATTRIBUTION_RE = re.compile(
    r"(?:正如)?"
    rf"(?=(?:{'|'.join(re.escape(name) for name in _ATTRIBUTION_NAMES)}|《))"
    rf"(?P<author>{'|'.join(re.escape(name) for name in _ATTRIBUTION_NAMES)})?"
    r"(?:在?《(?P<title>[^》\n]{2,100})》中?)?(?P<verb>指出|强调|认为|提出|写道|说过|说)"
    r"(?P<suffix>[ \t]*(?:的(?:那样|是)?|了)?[：:，,]?[ \t]*)"
)
_ANY_LONG_QUOTE_RE = re.compile(r'[“「『"](?P<quote>[^“”「」『』"\n]{8,}?)[”」』"]')
_INLINE_QUOTE_WITH_REFS_RE = re.compile(
    r'(?P<open>[“「『"])(?P<quote>[^“”「」『』"\n]{4,}?)(?P<close>[”」』"])'
    r'(?P<space>\s*)(?P<refs>(?:\[\d+\]\s*)*)'
)
_PAGE_CLAIM_RE = re.compile(r"(?:第\s*\d+\s*(?:卷|册).{0,20})?第\s*\d+\s*页")

_ACADEMIC_WRITING_RULES = (
    "以高水准学术论文写作为标准：围绕问题形成论点、证据、分析与推导，引用服务于论证。"
    "根据语境灵活平衡转述、改写和直接引述，不预设比例，不要求每段使用固定引用形式。"
    "转述和改写必须保留原意、条件与限定，并在所支持的论断句末标 [N]；"
    "转述和改写应按论证需要重组表达，不能仅去掉引号大段照录；采用原文关键措辞时应明确作为直接引述。"
    "概念解释、比较与推导应充分展开，明确区分原作者观点与解释性推论。"
    "学理框架用于组织问题；具体作者观点由所给材料支持，解释性推论从已提供的原文前提出发，说明推导关系与适用条件。"
    "措辞本身具有分析价值时直接引述：连续短语、分句可以嵌入自己的句子，较长引文按需要独立成块；"
    "不要为引用而搬运整段，也不要为避免直接引述而抹去关键措辞。"
    "同一篇目只在论证确有需要时完整介绍，后续自然承接，不反复以《某篇》指出开头。"
    "以所讨论的概念、关系或具体判断作为论述主体，能直接陈述的命题直接陈述。"
    "采用中文马克思主义学术论文的自然论述方式：先正面说明具体命题及其成立条件，"
    "再围绕概念内涵、历史联系、作用机制与理论意义展开有依据的分析，段落随问题自然推进。"
    "开篇、小节首句和结尾应直接承载实质判断，说明研究对象具有何种规定、关系怎样形成、"
    "变化经由哪些条件实现；以肯定命题及其解释作为论述主干，把主要篇幅用于展开这些内容。"
    "例如说明社会关系的历史性，可以直接论述它在特定生产条件下形成并随条件变化而发展，"
    "随后分析具体机制与限度；这种正面展开是写作原则，具体命题仍须依据本轮材料。"
    "转折和否定用于确有对象的观点辨析、条件限定或反驳；避免反复用‘不是……而是……’"
    "‘并非……恰恰……’起笔，或为引出肯定判断先虚设一个需要否定的观点。"
    "必要的辩证分析、对比和限定应充分保留；原著引文中的否定、转折及其措辞一律忠实保留。"
    "无需遵循固定句式或数量比例，不用同一种段落模板组织所有论点。"
    "成稿前在内部通读分析文字：若连续使用否定和转折只是为了突出肯定结论，就保持命题、"
    "依据和必要限定，直接写出该结论并充分解释；有真实辨析对象的对比及原著措辞完整保留。"
    "不反复使用文献指出、材料表明、文本指出、文本认为、文本对某问题的说明等材料介绍句式；"
    "不要把文献指出简单替换成文本指出。仅在辨析观点归属或比较不同论述时强调作者或篇目。"
    "不罗列来源清单，不逐段介绍材料说了什么。"
    "直接引文逐字保留同一编号原文中的连续片段及其标点，不用省略号拼接，不补写缺字；"
    "嵌入正文的直接原文，无论短语、分句或连续多句，都用完整的中文双引号包围全部所引文字；"
    "原文内部已有引号时，外层引用应包住完整句或语义完整的分句，内部引号原样保留；"
    "不要以内引号为切点，把以‘的’等连接成分起头的依附性尾部单独当作直接引文。"
    "独立长引可用完整的Markdown引用块。编号放在完整引文之后或其所属论断句末，"
    "原文后续句子仍被照录时也应完整标明引用边界与来源，不能把它当作自己的分析。"
    "转述与署名观点均可标注来源，不需要为了署名而强加一段直接引文。"
    "页眉页脚、页码残片、PDF水印、无关下载网址和联系邮箱不得进入正文或引文。"
)



@dataclass(frozen=True)
class _InlineQuotation:
    string: str
    begin: int
    close: int
    stop: int

    def start(self):
        return self.begin

    def end(self):
        return self.stop

    def group(self, name=0):
        return {
            0: self.string[self.begin:self.stop],
            "open": self.string[self.begin], "close": self.string[self.close],
            "quote": self.string[self.begin + 1:self.close],
            "refs": self.string[self.close + 1:self.stop], "suffix": "", "space": "",
        }[name]


def _inline_quotations(line: str):
    """Yield outer quotations once, including nested quotation marks."""
    pairs = {"“": "”", "「": "」", "『": "』", '"': '"'}
    cursor = 0
    code = [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`", line)]
    while cursor < len(line):
        if line[cursor] not in pairs or any(a <= cursor < b for a, b in code):
            cursor += 1
            continue
        start, stack = cursor, [pairs[line[cursor]]]
        cursor += 1
        while cursor < len(line) and stack:
            c = line[cursor]
            if c == stack[-1]:
                stack.pop()
            elif c in pairs:
                stack.append(pairs[c])
            cursor += 1
        if stack:
            # An orphan opening mark must not hide subsequent valid quotes.
            cursor = start + 1
            continue
        close = cursor - 1
        refs = re.match(r"[ \t]*(?:\[\d+\][ \t]*)+", line[cursor:])
        stop = cursor + len(refs.group()) if refs else cursor
        yield _InlineQuotation(line, start, close, stop)
        cursor = stop


def _citation_unit_refs(text: str, start: int) -> list[int]:
    """Refs for this sentence/clause, never for a later sentence or author."""
    tail = text[start:]
    quoted = [(m.start(), m.close + 1) for m in _inline_quotations(tail)]
    stop = len(tail)
    for m in re.finditer(r"[。！？!?；;\n]", tail):
        if any(a <= m.start() < b for a, b in quoted):
            continue
        stop = m.end()
        suffix = re.match(r'[”」』\"*_ \t]*(?:\[\d+\][ \t]*)*', tail[stop:])
        stop += len(suffix.group()) if suffix else 0
        break
    next_author = next((m for m in _ATTRIBUTION_RE.finditer(tail)
                        if not any(a <= m.start() < b for a, b in quoted)), None)
    if next_author:
        stop = min(stop, next_author.start())
    return [int(v) for v in _GROUNDING_REF_RE.findall(tail[:stop])]

# 「关思考」开关：推理模型（deepseek-v4-flash/pro、智谱 GLM）都接受该字段，服务端据此不产 reasoning。
# 用途有二：智谱通道一贯强制关闭；DeepSeek 通道仅在「首答只剩思维链」时作为一次性重试参数（见
# chat_complete / _stream_chat_once 的 disable_thinking）。日常仍开思考——它显著提升回答质量。
_THINKING_DISABLED = {"type": "disabled"}

# 正文里混进思维链的识别（所有对话链路共用，不只研究综述）。命中只触发一次「关思考重试」，
# 代价可控；重试后的正文即便再次疑似，也照常返回（宁可给内容也不给空错误）。
# 英文标记不可或缺：推理模型的思维链常是英文（线上事故现场即「we need answer in Chinese…」）。
_REASONING_LEAK_HEAD_MARKERS = (
    "用户要求我",
    "用户问的是",
    "用户希望我",
    "我需要先",
    "我们需要先",
    "我需要回答",
    "我们需要回答",
    "首先，用户",
    "首先我需要",
    "首先，我需要",
    "让我先",
    "我应该先",
    "需要用中文回答",
    "需要结构",
    "we need",
    "we should",
    "we must",
    "we can quote",
    "the user asks",
    "the user wants",
    "i need to",
    "let me",
    "need answer",
    "need to answer",
    "need structure",
    "first, the user",
    "okay, the user",
)


class ZAIClient:
    def __init__(self, config: AIConfig) -> None:
        self.config = config

    @staticmethod
    def _format_grounding_block(grounding: list[dict[str, Any]] | None) -> str:
        """把引文库接地命中（真实原文+准确出处）拼成编号清单，供注入提示词；空则返回空串。

        ``grounding`` 每项形如 ``{"index": 1, "citation": "《…》第X卷，…第N页。", "text": "原文…"}``，
        全部由 app 层经 ``corpus.locate_associative`` 的真实命中产生——引文不可伪造。
        """
        if not grounding:
            return ""
        lines: list[str] = []
        for item in grounding:
            text = clean_evidence((item or {}).get("text") or "", item).text
            if not text:
                continue
            idx = (item or {}).get("index")
            citation = " ".join(str((item or {}).get("citation") or "").split())
            work_title = " ".join(str((item or {}).get("work_title") or "").split())
            authors = [
                " ".join(str(author or "").split())
                for author in ((item or {}).get("work_authors") or [])
                if " ".join(str(author or "").split())
            ]
            provenance_verified = bool((item or {}).get("provenance_verified"))
            head = f"[{idx}]" if idx is not None else "-"
            metadata = [
                f"篇目：{f'《{work_title}》' if work_title and provenance_verified else '（篇目未核验，正文不得补写篇名）'}",
                f"责任者：{'、'.join(authors) if provenance_verified and authors else '（责任者未核验，正文不得补写人名）'}",
                f"版本卷页：{citation or '（出处缺失）'}",
            ]
            lines.append(f"{head} " + "\n".join(metadata) + f"\n原文：{text}")
        return "\n\n".join(lines)

    @staticmethod
    def _is_fast_tier(model_name: str) -> bool:
        """是不是「快档」模型（deepseek-v4-flash）？快档默认关思考。

        依据（2026-07-31 实测，均为 flash）：
        - 线索抽取（JSON，mt=1500/temp=0）：开思考 16.2s、reasoning 吃满 1500、finish=length、
          **JSON 完全解析不出**（这正是日志里长期刷屏的 "expand yielded no usable clues" 病根）；
          关思考 5.1s、finish=stop、JSON 干净可用。
        - 接地作答（mt=10000）：开思考 33.4s / 正文 1946 字；关思考 15.5s / 正文 1953 字，
          小标题与编号引用同样齐备——**速度翻倍而质量不降**。
        - 吉祥物（mt=500）：思考直接吃光预算，线上长期每天数次 "模型返回了空内容"。
        弱模型在难任务上反而反复权衡（比 pro 想得更久），思考对它是净负担；深思交给 pro 档。
        """
        normalized = str(model_name or "").lower()
        return "flash" in normalized or normalized == "mimo-v2.5"

    @staticmethod
    def _looks_like_reasoning_leak(text: str) -> bool:
        """开头像「思考过程」而非答案？（个别推理模型会把思维链直接写进 content。）

        只看开头 240 字：正式答复的开篇是论述或小标题，不会是「用户要求我…」「we need answer in
        Chinese…」这类自我分析。命中仅用于触发一次「关思考重试」，不会据此丢弃内容。
        """
        head = " ".join(str(text or "")[:240].split()).lower()
        if not head:
            return False
        return any(marker in head for marker in _REASONING_LEAK_HEAD_MARKERS)

    @staticmethod
    def _research_review_has_reasoning_leak(text: str) -> bool:
        sample = str(text or "")[:1500]
        return bool(_RESEARCH_REVIEW_LEAK_PREFIX_RE.search(sample)) or any(
            marker in sample for marker in _RESEARCH_REVIEW_LEAK_MARKERS
        )

    @staticmethod
    def _strip_fabricated_citation_lines(text: str) -> str:
        """整行剔除模型自造的伪出处脚注/尾注（接地问答与研究综述共用）。真实出处只走 [N] 编号；
        任何以圈码/「注N」起头且带「书名＋卷次/页码」的自拟脚注行、或把书名出处与「非检索/学理补充」
        自证并置的行，一律删除。合法的行内引证「（[1]《…》第X页）」以方括号编号起头，不受影响。"""
        s = str(text or "")
        if not s:
            return ""
        kept = [
            ln for ln in s.split("\n")
            if not any(rx.search(ln) for rx in _FABRICATED_CITE_LINE_RES)
        ]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    @staticmethod
    def _direct_quote_normalized_map(text: str) -> tuple[str, list[int]]:
        """引文比对用的 NFKC 字符串及原文位置映射：忽略标点/空白，但不容忍改字或换字。"""
        chars: list[str] = []
        positions: list[int] = []
        for pos, raw_ch in enumerate(str(text or "")):
            for ch in unicodedata.normalize("NFKC", raw_ch).lower():
                if not ch.isalnum():
                    continue
                chars.append(ch)
                positions.append(pos)
        return "".join(chars), positions

    @classmethod
    def _complete_direct_quote_from_source(cls, quote: str, source: str) -> str:
        """若 quote 是 source 的逐字片段，返回覆盖它的完整原句；不能确定则返回空串。

        匹配只忽略标点、空白与全半角差异，不做模糊改字。这样既能补回模型漏掉的句首/句末，
        又不会把转述或模型自写内容误判为原文。
        """
        visible = re.sub(r"\[\d+\]", "", str(quote or ""))
        visible = re.sub(r"[*_~`]", "", visible).strip().strip("“”「」『』\"")
        needle, _ = cls._direct_quote_normalized_map(visible)
        haystack, positions = cls._direct_quote_normalized_map(source)
        if len(needle) < 8 or not haystack or not positions:
            return ""
        found = haystack.find(needle)
        if found < 0:
            return ""
        start = positions[found]
        stop = positions[found + len(needle) - 1] + 1

        left = 0
        for mark in _DIRECT_QUOTE_ENDERS:
            left = max(left, str(source).rfind(mark, 0, start) + 1)
        right_candidates = [str(source).find(mark, stop) for mark in _DIRECT_QUOTE_ENDERS]
        right_candidates = [pos for pos in right_candidates if pos >= 0]
        if not right_candidates:
            # 对应接地段本身若没有后续句号，就不能证明这是一句完整原文；保持模型原样最安全。
            return ""
        right = min(right_candidates) + 1
        while right < len(source) and source[right] in _DIRECT_QUOTE_CLOSERS:
            right += 1
        completed = str(source)[left:right].strip()
        return completed if completed and completed.rstrip(_DIRECT_QUOTE_CLOSERS)[-1:] in _DIRECT_QUOTE_ENDERS else ""

    @classmethod
    def _grounded_quote_excerpt(cls, quote: str, item: dict) -> str:
        visible = re.sub(r"[*_~`]", "", str(quote or "")).strip()
        cleaned = item.get("_cleaned_evidence") or clean_evidence(item.get("text") or "", item)
        match = exact_quote(visible, cleaned.text)
        if not match:
            return ""
        # Respect both pre-window cleanup barriers and any damage discovered here.
        for segments in (item.get("quote_segments", [cleaned.text]), cleaned.quote_segments):
            if not any(exact_quote(visible, segment) for segment in segments):
                return ""
        return match

    @classmethod
    def _sanitize_grounded_direct_quotes(
        cls,
        text: str,
        grounding: list[dict[str, Any]] | None,
        *,
        promote_inline_blocks: bool = False,
    ) -> str:
        """保留可核验引文的选择范围，并去掉重复照录，保留分析正文。

        - Markdown 引用块：保留所选连续片段；同一片段再次出现时只移除重复引用块。
        - 行内引号：不扩大所选范围，保留短语在句子中的语法作用。
        - ``promote_inline_blocks`` 保留参数兼容性；不再强制提升行内引文。
        - 代码块、无 [N] 内容、无法在原文精确匹配的内容完全不动。
        """
        answer = str(text or "")
        if not answer or not grounding:
            return answer
        source_by_index: dict[int, str] = {}
        for item in grounding:
            try:
                idx = int((item or {}).get("index"))
            except (TypeError, ValueError):
                continue
            source = " ".join(str((item or {}).get("text") or "").split())
            if source:
                source_by_index[idx] = source
        if not source_by_index:
            return answer

        seen_quotes: set[str] = set()
        lines = answer.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        output: list[str] = []
        in_fence = False
        i = 0

        def _match_completed(raw_quote: str, refs_text: str) -> str:
            for raw_idx in _GROUNDING_REF_RE.findall(refs_text):
                source = source_by_index.get(int(raw_idx))
                if not source:
                    continue
                completed = cls._grounded_quote_excerpt(raw_quote, {"text": source})
                if completed:
                    return completed
            return ""

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if re.match(r"^(?:`{3,}|~{3,})", stripped):
                in_fence = not in_fence
                output.append(line)
                i += 1
                continue
            if in_fence:
                output.append(line)
                i += 1
                continue

            if line.lstrip().startswith(">"):
                block_lines: list[str] = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    block_lines.append(lines[i])
                    i += 1
                body = " ".join(part.lstrip()[1:].strip() for part in block_lines).strip()
                refs_text = "".join(f"[{idx}]" for idx in _GROUNDING_REF_RE.findall(body))
                consume_ref_line = False
                if not refs_text and i < len(lines) and _GROUNDING_REF_ONLY_RE.fullmatch(lines[i] or ""):
                    refs_text = "".join(f"[{idx}]" for idx in _GROUNDING_REF_RE.findall(lines[i]))
                    consume_ref_line = True
                raw_quote = _GROUNDING_REF_RE.sub("", body).strip()
                completed = _match_completed(raw_quote, refs_text)
                if completed:
                    key, _ = cls._direct_quote_normalized_map(completed)
                    if key in seen_quotes:
                        # 只删重复逐字引文本身；前后的解释段落均保留，回答的信息量不缩水。
                        if consume_ref_line:
                            i += 1
                        continue
                    seen_quotes.add(key)
                    output.append(f"> {completed}{refs_text}")
                    if consume_ref_line:
                        i += 1
                    continue
                output.extend(block_lines)
                continue

            def _replace_inline(match: re.Match) -> str:
                completed = _match_completed(match.group("quote"), match.group("refs"))
                if not completed:
                    return match.group(0)
                key, _ = cls._direct_quote_normalized_map(completed)
                refs = "".join(f"[{idx}]" for idx in _GROUNDING_REF_RE.findall(match.group("refs")))
                if key in seen_quotes:
                    return f"这一论述{refs}"
                seen_quotes.add(key)
                return f'{match.group("open")}{completed}{match.group("close")}{refs}'

            cursor, parts = 0, []
            for match in _inline_quotations(line):
                parts.extend((line[cursor:match.start()], _replace_inline(match) if match.group("refs") else match.group()))
                cursor = match.end()
            output.append("".join(parts) + line[cursor:])
            i += 1

        return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()

    @classmethod
    def _attribution_errors(cls, match: re.Match, item: dict[str, Any]) -> list[str]:
        claimed = match.group("author") or ""
        required = ("马克思", "恩格斯") if "马克思" in claimed and "恩格斯" in claimed else (claimed,) if claimed else ()
        authors = [str(a) for a in item.get("work_authors") or []]
        errors = []
        if not item.get("provenance_verified") or any(not any(name in a for a in authors) for name in required):
            errors.append("author_mismatch")

        def title_key(title: str) -> str:
            # The verified catalogue may append a composition date. This is a
            # bibliographic suffix, not a license to match arbitrary substrings.
            title = re.sub(r"[（(][〇零一二三四五六七八九十百0-9年月日\s]+[）)]\s*$", "", str(title or ""))
            return cls._direct_quote_normalized_map(title)[0]

        if match.group("title") and title_key(match.group("title")) != title_key(item.get("work_title") or ""):
            errors.append("title_mismatch")
        return errors

    @classmethod
    def validate_grounded_answer(cls, text: str, grounding: list[dict[str, Any]] | None) -> list[str]:
        """Validate selected excerpts and attribution separately, without rewriting."""
        answer = str(text or "")
        if not answer:
            return ["empty_answer"]
        evidence = {}
        for item in grounding or []:
            try:
                evidence[int(item.get("index"))] = item
            except (TypeError, ValueError, AttributeError):
                continue
        violations = []
        if _GENERIC_SOURCE_PHRASE_RE.search(answer):
            violations.append("generic_source_phrase")
        for index in map(int, _GROUNDING_REF_RE.findall(answer)):
            if index not in evidence:
                violations.append(f"unknown_reference:{index}")
        lines = answer.splitlines()
        fence = False
        row = 0
        while row < len(lines):
            line = lines[row]
            if re.match(r"^\s*(?:`{3,}|~{3,})", line):
                fence = not fence
                row += 1
                continue
            if fence:
                row += 1
                continue
            records = []
            if line.lstrip().startswith(">"):
                block = []
                while row < len(lines) and lines[row].lstrip().startswith(">"):
                    block.append(lines[row].lstrip()[1:].strip())
                    row += 1
                body = " ".join(block)
                if row < len(lines) and _GROUNDING_REF_ONLY_RE.fullmatch(lines[row]):
                    body += lines[row]
                    row += 1
                records.append((_GROUNDING_REF_RE.sub("", body).strip().strip('“”「」『』"'),
                                [int(v) for v in _GROUNDING_REF_RE.findall(body)]))
            else:
                for match in _inline_quotations(line):
                    quote = match.group("quote")
                    refs = _citation_unit_refs(line, match.close + 1)
                    if refs or len(cls._direct_quote_normalized_map(quote)[0]) >= 20 or quote[-1:] in _DIRECT_QUOTE_ENDERS:
                        records.append((quote, refs))
                for match in _ATTRIBUTION_RE.finditer(line):
                    if any(q.start() <= match.start() < q.end() for q in _inline_quotations(line)):
                        continue
                    refs = _citation_unit_refs(line, match.end())
                    next_row = row + 1
                    while next_row < len(lines) and not lines[next_row].strip():
                        next_row += 1
                    if not refs and not line[match.end():].strip() and next_row < len(lines) and lines[next_row].lstrip().startswith(">"):
                        refs = [int(v) for v in _GROUNDING_REF_RE.findall(lines[next_row])]
                    if not refs:
                        violations.append("uncited_named_attribution")
                    for index in refs:
                        item = evidence.get(index)
                        if item is None:
                            continue
                        violations.extend(f"{error}:{index}" for error in cls._attribution_errors(match, item))
                row += 1
            for quote, refs in records:
                if not refs:
                    violations.append("uncited_direct_quote")
                for index in refs:
                    if index in evidence and not cls._grounded_quote_excerpt(quote, evidence[index]):
                        violations.append(f"quote_mismatch:{index}")
        violations.extend(clean_evidence(answer, markdown=True).issues)
        return list(dict.fromkeys(violations))

    @classmethod
    def repair_grounded_answer(
        cls, text: str, grounding: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Repair citation units without discarding an otherwise useful answer.

        This is deliberately different from :meth:`validate_grounded_answer`.
        Formatting defects (a missing/remote ``[N]`` or a generic source lead)
        are repaired deterministically.  A quote that cannot be located in one
        injected source is de-quoted locally, and a false author/work lead is
        removed locally without inventing a replacement lead. The caller only needs a
        constrained same-source regeneration when no usable source reference
        survives; it must never replace the whole answer with ungrounded prose.
        """

        answer = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        evidence: dict[int, dict[str, Any]] = {}
        for item in grounding or []:
            try:
                index = int((item or {}).get("index"))
            except (TypeError, ValueError):
                continue
            if str((item or {}).get("text") or "").strip():
                evidence[index] = dict(item or {})
                evidence[index]["_cleaned_evidence"] = clean_evidence(item.get("text") or "", item)
        if not answer or not evidence:
            return {
                "answer_markdown": answer,
                "status": "insufficient",
                "issues": ["empty_answer" if not answer else "missing_evidence"],
                "used_indices": [],
                "verified_quote_count": 0,
            }

        issues: list[str] = []
        verified_quote_count = 0
        seen_verified_quotes: set[str] = set()
        # Recover source-backed inline fragments before older quote/attribution
        # checks can strip their delimiters. The same pass at the end is idempotent.
        import ai_citations
        if ai_citations.enabled():
            try:
                answer, restored_issues = ai_citations.repair_missing_references(answer, list(evidence.values()))
                issues.extend(restored_issues)
            except Exception:
                LOGGER.exception("Quote boundary recovery unavailable; retaining original answer for verification")
        answer, watermark_count = PDF_WATERMARK_RE.subn(" ", answer)
        if watermark_count:
            issues.append("pdf_watermark_removed")

        def _normalized(value: str) -> str:
            return cls._direct_quote_normalized_map(value)[0]

        def _quote_has_ocr_noise(value: str) -> bool:
            return bool(clean_evidence(value).issues)

        def _quote_matches(raw_quote: str, indices: list[int]) -> list[tuple[int, str]]:
            matches: list[tuple[int, str]] = []
            if _quote_has_ocr_noise(raw_quote):
                return matches
            needle = _normalized(raw_quote)
            # 党和国家文献中常见“两个务必”“实事求是”等短引。
            # 是否属于直接引文由外层的编号/引出语规则判定，这里只做逐字包含校验。
            if len(needle) < 2:
                return matches
            for index in indices:
                item = evidence.get(index)
                if not item:
                    continue
                excerpt = cls._grounded_quote_excerpt(raw_quote, item)
                if excerpt:
                    matches.append((index, excerpt))
            return matches

        def _recover_repeated_quote(raw_quote: str, indices: list[int]) -> list[tuple[int, str]]:
            """Recover only a source-backed quote damaged by duplicated fragments.

            Some models repeat an inner quoted clause when the evidence itself
            contains nested quotation marks.  The returned text is always one
            complete, contiguous sentence copied from the explicitly cited
            source; fuzzy text is never emitted as a quotation.
            """

            raw_norm = _normalized(raw_quote)
            if len(raw_norm) < 16:
                return []
            recovered: list[tuple[int, str]] = []
            for index in indices:
                item = evidence.get(index)
                if not item:
                    continue
                source = " ".join(str(item.get("text") or "").split())
                source_norm, positions = cls._direct_quote_normalized_map(source)
                if not source_norm or not positions:
                    continue
                matcher = difflib.SequenceMatcher(None, source_norm, raw_norm, autojunk=False)
                blocks = [block for block in matcher.get_matching_blocks() if block.size >= 6]
                if not blocks:
                    continue
                longest = max(blocks, key=lambda block: block.size)
                # A short common phrase must never be enough to manufacture a
                # quotation.  Recovery is reserved for a substantially matching
                # sentence whose mismatch is consistent with repeated text.
                if longest.size < 16:
                    continue
                probe_start = positions[longest.a]
                probe_end = positions[longest.a + longest.size - 1] + 1
                probe = source[probe_start:probe_end]
                completed = cls._complete_direct_quote_from_source(probe, source)
                completed_norm = _normalized(completed)
                if not completed_norm:
                    continue
                completed_start = source_norm.find(completed_norm)
                if completed_start < 0:
                    continue
                completed_stop = completed_start + len(completed_norm)
                covered: set[int] = set()
                for block in blocks:
                    start = max(block.a, completed_start)
                    stop = min(block.a + block.size, completed_stop)
                    if stop > start:
                        covered.update(range(start, stop))
                coverage = len(covered) / max(1, len(completed_norm))
                # The raw form must be longer than the canonical source sentence
                # (the tell-tale duplication), and most of that source sentence
                # must still be recoverable in source order.
                if len(raw_norm) <= len(completed_norm) or coverage < 0.72:
                    continue
                if cls._grounded_quote_excerpt(completed, item):
                    recovered.append((index, completed))
            return recovered

        def _same_attribution(match: re.Match, item: dict[str, Any]) -> bool:
            return not cls._attribution_errors(match, item)

        def _close_dangling_quote_lead(lines: list[str]) -> None:
            """Close an introduction whose following quote had to be de-quoted."""

            for pos in range(len(lines) - 1, -1, -1):
                if not lines[pos].strip():
                    continue
                if re.search(r"[：:]\s*$", lines[pos]):
                    lines[pos] = re.sub(r"[：:]\s*$", "。", lines[pos])
                return

        # Treat a contiguous Markdown quote block as one sealed citation unit.
        # In particular, do not send its nested inner quotation marks through
        # the later inline-quote pass: that was the cause of repeated clauses.
        block_lines: list[str] = []
        source_lines = answer.split("\n")
        in_fence = False
        line_index = 0
        while line_index < len(source_lines):
            line = source_lines[line_index]
            stripped = line.strip()
            if re.match(r"^(?:`{3,}|~{3,})", stripped):
                in_fence = not in_fence
                block_lines.append(line)
                line_index += 1
                continue
            if in_fence or not line.lstrip().startswith(">"):
                block_lines.append(line)
                line_index += 1
                continue

            markdown_quote_lines: list[str] = []
            while line_index < len(source_lines) and source_lines[line_index].lstrip().startswith(">"):
                markdown_quote_lines.append(source_lines[line_index])
                line_index += 1
            body = " ".join(part.lstrip()[1:].strip() for part in markdown_quote_lines).strip()
            raw_refs = [int(value) for value in _GROUNDING_REF_RE.findall(body)]
            consume_ref_line = False
            if not raw_refs and line_index < len(source_lines) and _GROUNDING_REF_ONLY_RE.fullmatch(
                source_lines[line_index] or ""
            ):
                raw_refs = [int(value) for value in _GROUNDING_REF_RE.findall(source_lines[line_index])]
                consume_ref_line = True
            quote = _GROUNDING_REF_RE.sub("", body)
            quote = re.sub(r"[*_~`]", "", quote).strip().strip("“”「」『』\"")
            noisy_quote = _quote_has_ocr_noise(quote)
            valid_refs = [idx for idx in raw_refs if idx in evidence]
            matches = _quote_matches(quote, valid_refs)
            if not matches and valid_refs:
                matches = _recover_repeated_quote(quote, valid_refs)
            if not matches:
                all_matches = _quote_matches(quote, list(evidence))
                matches = all_matches if len(all_matches) == 1 else []
            if matches:
                index, completed = matches[0]
                quote_key = _normalized(completed)
                if quote_key in seen_verified_quotes:
                    _close_dangling_quote_lead(block_lines)
                    issues.append("duplicate_quote_collapsed")
                else:
                    seen_verified_quotes.add(quote_key)
                    block_lines.append(f"> {completed}[{index}]")
                    verified_quote_count += 1
                    if raw_refs != [index] or _normalized(quote) != quote_key:
                        issues.append("quote_reference_repaired")
            else:
                # Never leave a visual hole.  Unverified wording loses quote
                # styling and its source number, but remains as ordinary prose;
                # the surrounding argument is preserved for the same-source
                # retry/final repair decision.
                _close_dangling_quote_lead(block_lines)
                plain = quote.strip()
                if plain and not noisy_quote:
                    block_lines.append(plain)
                    issues.append("quote_block_dequoted")
                elif noisy_quote:
                    issues.append("ocr_quote_block_removed")
                else:
                    issues.append("empty_quote_block_removed")
            if consume_ref_line:
                line_index += 1
        answer = "\n".join(block_lines)

        # Inline quotation marks also cover terminology.  Treat them as direct
        # quotations only when they carry a reference, form a full/long sentence,
        # or follow a named attribution.  This avoids the previous false positive
        # on harmless terms such as “赛伯格（Cyborg）”.
        def _repair_inline(match: re.Match) -> str:
            nonlocal verified_quote_count
            quote = match.group("quote")
            if _quote_has_ocr_noise(quote):
                issues.append("ocr_inline_quote_removed")
                return ""
            raw_refs = [int(value) for value in _GROUNDING_REF_RE.findall(match.group("refs") or "")]
            deferred_refs = _citation_unit_refs(match.string, match.close + 1) if not raw_refs else []
            raw_refs = raw_refs or deferred_refs
            prefix = match.string[max(0, match.start() - 100):match.start()]
            direct_candidate = bool(raw_refs) or len(_normalized(quote)) >= 20 or bool(
                quote.rstrip(_DIRECT_QUOTE_CLOSERS)[-1:] in _DIRECT_QUOTE_ENDERS
            ) or bool(_ATTRIBUTION_RE.search(prefix))
            if not direct_candidate:
                return match.group(0)
            matches = _quote_matches(quote, [idx for idx in raw_refs if idx in evidence])
            if not matches:
                all_matches = _quote_matches(quote, list(evidence))
                matches = all_matches if len(all_matches) == 1 else []
            if matches:
                index, completed = matches[0]
                quote_key = _normalized(completed)
                if quote_key in seen_verified_quotes and len(quote_key) >= 40 and quote.rstrip(_DIRECT_QUOTE_CLOSERS)[-1:] in _DIRECT_QUOTE_ENDERS:
                    issues.append("duplicate_quote_collapsed")
                    return f"这一论述[{index}]"
                seen_verified_quotes.add(quote_key)
                verified_quote_count += 1
                if raw_refs != [index] or _normalized(quote) != _normalized(completed):
                    issues.append("quote_reference_repaired")
                refs_text = "" if deferred_refs and index in deferred_refs else f"[{index}]"
                return f"{match.group('open')}{completed}{match.group('close')}{refs_text}"
            # Do not present unverified wording as a verbatim quotation.  Keeping
            # the words as ordinary prose preserves the local argument instead
            # of deleting the whole answer.
            issues.append("unverified_quote_dequoted")
            return quote

        repaired_lines: list[str] = []
        in_fence = False
        for line in answer.split("\n"):
            stripped = line.strip()
            if re.match(r"^(?:`{3,}|~{3,})", stripped):
                in_fence = not in_fence
                repaired_lines.append(line)
            elif in_fence or line.lstrip().startswith(">"):
                repaired_lines.append(line)
            else:
                cursor, parts = 0, []
                for match in _inline_quotations(line):
                    parts.extend((line[cursor:match.start()], _repair_inline(match)))
                    cursor = match.end()
                repaired_lines.append("".join(parts) + line[cursor:])
        answer = "\n".join(repaired_lines)

        # Unknown source numbers are formatting/content errors local to that
        # marker.  Removing the marker is safer and far less destructive than a
        # second ungrounded answer.
        def _remove_unknown_ref(match: re.Match) -> str:
            index = int(match.group(1))
            if index in evidence:
                return match.group(0)
            issues.append("unknown_reference_removed")
            return ""

        answer = _GROUNDING_REF_RE.sub(_remove_unknown_ref, answer)

        def _nearby_valid_refs(lines: list[str], row: int, end: int) -> list[int]:
            # Prefer the same line.  A prose lead ending in a colon may point to
            # the immediately following Markdown quote block, so inspect that
            # one block as the same citation unit and nothing farther away.
            refs = [idx for idx in _citation_unit_refs(lines[row], end) if idx in evidence]
            if refs:
                return refs
            if lines[row][end:].strip():
                return []
            next_row = row + 1
            while next_row < len(lines) and not lines[next_row].strip():
                next_row += 1
            if next_row < len(lines) and lines[next_row].lstrip().startswith(">"):
                return [
                    int(value) for value in _GROUNDING_REF_RE.findall(lines[next_row])
                    if int(value) in evidence
                ]
            return []

        # Named attribution is retained only when the same [N] verifies both the
        # responsibility and work title. Otherwise remove only the unsupported
        # lead, including a dependent 是 construction, without adding a template.
        attribution_lines = answer.split("\n")
        in_fence = False
        for row, line in enumerate(attribution_lines):
            stripped = line.strip()
            if re.match(r"^(?:`{3,}|~{3,})", stripped):
                in_fence = not in_fence
                continue
            if in_fence or line.lstrip().startswith(">"):
                continue

            def _repair_attribution(match: re.Match) -> str:
                if any(q.start() <= match.start() < q.end() for q in _inline_quotations(line)):
                    return match.group(0)
                refs = _nearby_valid_refs(attribution_lines, row, match.end())
                if any(_same_attribution(match, evidence[index]) for index in refs):
                    return match.group(0)
                issues.append("unverified_attribution_removed")
                return ""

            attribution_lines[row] = _ATTRIBUTION_RE.sub(_repair_attribution, line)
        answer = "\n".join(attribution_lines)

        # Remove independent retrieval-report leads without inserting a template.
        # Original quotations and embedded grammatical phrases remain sealed.
        generic_lines = answer.split("\n")
        in_fence = False
        for row, line in enumerate(generic_lines):
            stripped = line.strip()
            if re.match(r"^(?:`{3,}|~{3,})", stripped):
                in_fence = not in_fence
                continue
            if in_fence or line.lstrip().startswith(">"):
                continue

            def _repair_generic_lead(match: re.Match) -> str:
                if any(q.start() <= match.start() < q.end() for q in _inline_quotations(line)):
                    return match.group(0)
                prefix = line[:match.start()].rstrip()
                # Only remove a syntactically independent lead. Embedded phrases
                # and source quotations are not prose to rewrite with regexes.
                if prefix and prefix[-1] not in "。！？；，：:、":
                    return match.group(0)
                if line[match.end():].startswith("的"):
                    return match.group(0)
                issues.append("generic_source_lead_repaired")
                return ""

            generic_lines[row] = _GENERIC_SOURCE_PHRASE_RE.sub(_repair_generic_lead, line)
        answer = "\n".join(generic_lines)
        cleaned_answer = clean_evidence(answer, markdown=True)
        answer = cleaned_answer.text
        issues.extend(cleaned_answer.issues)
        # Collapse only immediately repeated identical markers.  Reusing the
        # same source later in a different sentence remains legitimate.
        answer = re.sub(r"\[(\d+)\](?:\s*\[\1\])+", r"[\1]", answer)
        answer = re.sub(r"[ \t]+\n", "\n", answer)
        answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
        used_indices = sorted({
            int(value) for value in _GROUNDING_REF_RE.findall(answer)
            if int(value) in evidence
        })
        citation_details = {}
        # Citation-only postprocessing: it never rewrites the generated argument
        # or adds a model call to the ordinary generation path.
        import ai_citations
        if ai_citations.enabled():
            try:
                restored, restore_issues = ai_citations.repair_missing_references(answer, list(evidence.values()))
                citation_details = ai_citations.ledger(restored, list(evidence.values()))
                answer = citation_details["answer_markdown"]
                used_indices = citation_details["used_indices"]
                issues.extend(restore_issues + citation_details.pop("issues", []))
            except Exception:
                LOGGER.exception("Citation ledger unavailable; keeping existing repaired answer")
        status = "verified" if not issues and used_indices else "repaired" if used_indices else "insufficient"
        return {
            **citation_details,
            "answer_markdown": answer,
            "status": status,
            "issues": list(dict.fromkeys(issues)),
            "used_indices": used_indices,
            "verified_quote_count": verified_quote_count,
        }

    @classmethod
    def sanitize_ungrounded_answer(cls, text: str) -> str:
        """Neutralize unverified source styling without deleting useful prose.

        Ungrounded answers must not present model memory as verified quotations,
        page references or author/work attributions.  Those claims are local to a
        marker or lead-in, however: deleting the whole Markdown line also deletes
        proposed wording, transitions and the surrounding analysis.  Preserve the
        text and remove only the unsupported presentation/claim fragments.
        """

        kept: list[str] = []
        for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            # A model often uses blockquotes to highlight a proposed sentence,
            # not to claim a verbatim source.  Flatten every quote level to plain
            # Markdown so the wording remains visible without quotation styling.
            line = re.sub(r"^(?P<indent>[ \t]*)(?:>[ \t]*)+", r"\g<indent>", raw_line)
            line = _GROUNDING_REF_RE.sub("", line)
            line = _PAGE_CLAIM_RE.sub("", line)
            line = re.sub(
                r"(?:参见|详见|载于|位于|来自|见)[ \t]*(?=[，,。；;])", "", line
            )
            line = _ATTRIBUTION_RE.sub("", line)
            line = _ANY_LONG_QUOTE_RE.sub(lambda match: match.group("quote"), line)

            # Removing a source lead can leave only its separator.  Clean that
            # punctuation locally while retaining Markdown prefixes and layout.
            line = re.sub(
                r"^(?P<prefix>[ \t]*(?:(?:#{1,6}|[-*+]|\d+[.)\u3001])[ \t]+)?)"
                r"[\uff1a:,\uff0c\uff1b;]+[ \t]*",
                r"\g<prefix>",
                line,
            )
            line = re.sub(r"[ \t]+([\uff0c。！？；：,.!?;:])", r"\1", line).rstrip()
            kept.append(line if line.strip() else "")
        return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    @staticmethod
    def _dedupe_research_review_closings(text: str) -> str:
        """只保留最后一个 Markdown 收束章节，移除较早的重复小结段。"""
        source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        closing_re = re.compile(
            r"(?im)^[ \t]{0,3}#{1,4}[ \t]*(?:小结|总结|结语|结束语|结论|余论)\b[^\n]*"
        )
        closings = list(closing_re.finditer(source))
        if len(closings) <= 1:
            return source.strip()
        headings = list(re.finditer(r"(?m)^[ \t]{0,3}#{1,4}[ \t]+\S[^\n]*", source))
        remove_ranges: list[tuple[int, int]] = []
        for closing in closings[:-1]:
            next_heading = next((heading for heading in headings if heading.start() > closing.start()), None)
            stop = next_heading.start() if next_heading is not None else closing.end()
            remove_ranges.append((closing.start(), stop))
        parts: list[str] = []
        cursor = 0
        for start, stop in remove_ranges:
            if start < cursor:
                continue
            parts.append(source[cursor:start])
            cursor = stop
        parts.append(source[cursor:])
        return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()

    @staticmethod
    def _strip_mimo_trailing_reference_appendix(text: str) -> str:
        """移除 MiMo 在接地回答末尾重复生成、且带 [N] 的引用原文附录。"""
        source = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        marker_re = re.compile(
            r"(?im)^[ \t]*(?:#{1,4}[ \t]*)?(?:\*\*)?"
            r"(?:引用原文|参考原文|参考文献|引用来源)(?:\*\*)?[ \t]*[：:]?[ \t]*$"
        )
        matches = list(marker_re.finditer(source))
        if not matches:
            return source.strip()
        marker = matches[-1]
        appendix = source[marker.end():]
        if marker.start() < len(source) // 3 or not _GROUNDING_REF_RE.search(appendix):
            return source.strip()
        return source[:marker.start()].rstrip()

    @classmethod
    def _sanitize_research_review_output(cls, text: str, *, allow_fragment: bool = False) -> str:
        """Keep only the formal review text and drop leaked reasoning/prompt-analysis material."""
        s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not s:
            return ""
        s = _RESEARCH_REVIEW_THINK_BLOCK_RE.sub("", s).strip()

        for marker in _RESEARCH_REVIEW_START_MARKERS:
            pos = s.find(marker)
            if pos >= 0:
                s = s[pos + len(marker):].strip()
                break

        for marker in _RESEARCH_REVIEW_END_MARKERS:
            pos = s.find(marker)
            if pos >= 0:
                s = s[:pos].strip()
                break

        # Some models ignore markers but label the final answer after a reasoning prelude.
        formal_match = re.search(
            r"(?:以下(?:是|为))?(?:正式)?(?:综述|文章)(?:正文)?(?:如下)?[：:]\s*",
            s[:2500],
        )
        if formal_match and cls._research_review_has_reasoning_leak(s[:formal_match.start()]):
            s = s[formal_match.end():].strip()

        if s.startswith("```"):
            s = re.sub(r"^```(?:markdown|md)?\s*", "", s, flags=re.I).strip()
            s = re.sub(r"\s*```$", "", s).strip()

        lines = s.split("\n")
        while lines and (not lines[0].strip() or _RESEARCH_REVIEW_LEAK_PREFIX_RE.search(lines[0])):
            lines.pop(0)
        s = "\n".join(lines).strip()

        heading_match = re.search(r"(?m)^#{1,3}\s*(?!.*(?:思考|分析|推理|思路)).+\S", s)
        if heading_match and heading_match.start() > 0 and cls._research_review_has_reasoning_leak(s[:heading_match.start()]):
            s = s[heading_match.start():].strip()

        for marker in (*_RESEARCH_REVIEW_START_MARKERS, *_RESEARCH_REVIEW_END_MARKERS):
            s = s.replace(marker, "")
        s = s.strip()

        # 剔除模型自造的伪出处脚注/尾注行（与接地问答共用同一套规则）。
        s = cls._strip_fabricated_citation_lines(s)

        if not s:
            return ""
        if cls._research_review_has_reasoning_leak(s[:700]):
            return ""
        if not allow_fragment and len(s) < 300:
            return ""
        return s

    @staticmethod
    def _research_review_complete(text: str) -> bool:
        """Heuristic guard for long reviews: require a visible conclusion and sentence-final punctuation."""
        compact = " ".join(str(text or "").split())
        if len(compact) < 600:
            return False
        if ZAIClient._research_review_has_reasoning_leak(compact[:1000]):
            return False
        tail = compact[-700:]
        has_closing_section = any(
            marker in tail
            for marker in ("小结", "总结", "结语", "结束语", "结论", "综上", "总之", "余论")
        )
        stripped = tail.rstrip()
        has_final_punctuation = stripped.endswith(("。", "！", "？", ".”", "！”", "？”", "）", "】")) or bool(
            re.search(r"[。！？][\]）】》」』”']*(?:\[\d+\])?$", stripped)
        )
        looks_cut = tail.rstrip().endswith(("，", "、", "；", "：", "（", "《", "“", "「", "[", "——", "-"))
        return has_closing_section and has_final_punctuation and not looks_cut

    @staticmethod
    def _research_review_cjk_chars(text: str) -> int:
        """Count Chinese ideographs, excluding Markdown, citation numbers and punctuation."""
        return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(text or "")))

    @staticmethod
    def _research_review_without_final_closing(text: str) -> tuple[str, bool]:
        """Remove the last conclusion section before appending substantive expansion.

        A short but formally complete first draft already ends in ``## 小结``.  Keeping that
        conclusion and appending more sections after it produces a malformed article with two
        conclusions, so continuation replaces the final conclusion instead.
        """
        source = str(text or "").rstrip()
        matches = list(
            re.finditer(
                r"(?im)^[ \t]{0,3}#{1,4}[ \t]*(?:小结|总结|结语|结束语|结论|余论)\b[^\n]*",
                source,
            )
        )
        if not matches:
            return source, False
        return source[:matches[-1].start()].rstrip(), True

    @staticmethod
    def _looks_like_token_limit_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "max_tokens",
                "max token",
                "maximum token",
                "tokens limit",
                "token limit",
                "context length",
                "too many tokens",
                "exceeds",
                "超过",
                "上限",
                "最大",
            )
        )

    def _chat_research_review(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        deadline: float | None = None,
        model: str | None = None,
        provider: str | None = None,
        disable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        token_ladder = [max_tokens]
        for fallback in (65536, 32768, 16000):
            if 0 < fallback < max_tokens and fallback not in token_ladder:
                token_ladder.append(fallback)
        last_error: AIServiceError | None = None
        for budget in token_ladder:
            # 生成跑在 SSE 心跳保活线程里，已与 CF ~100s 解耦：单次 HTTP 超时用研究专用的较宽上限
            # (RESEARCH_REVIEW_CALL_TIMEOUT_SECONDS，非全局 120s)，再被「剩余总预算」夹一下不超整篇预算。
            # 预算已不足一次最短调用则直接判超时，交上层兜底。
            http_timeout: float | None = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < RESEARCH_REVIEW_MIN_CALL_TIMEOUT_SECONDS:
                    raise last_error or AIServiceError("研究综述生成已超出时间预算。")
                http_timeout = min(float(RESEARCH_REVIEW_CALL_TIMEOUT_SECONDS), remaining)
            try:
                return self.chat_complete(
                    messages,
                    max_tokens=budget,
                    allow_reasoning_fallback=False,
                    http_timeout=http_timeout,
                    model=model,
                    provider=provider,
                    disable_thinking=disable_thinking,
                    reasoning_effort=reasoning_effort,
                )
            except AIServiceError as exc:
                if not self._looks_like_token_limit_error(exc):
                    raise
                last_error = exc
                LOGGER.warning("Research review token budget %s rejected, retrying lower budget: %s", budget, exc)
        raise last_error or AIServiceError("研究综述生成失败。")

    def answer_search_chat(
        self,
        messages: list[dict[str, Any]],
        question: str,
        provider: str | None = None,
        grounding: list[dict[str, Any]] | None = None,
        model: str | None = None,
        prefer_new_sources: bool = False,
        reasoning_effort: str | None = None,
        citation_retry: bool = False,
    ) -> AIAnswer:
        use_zhipu = provider == "zhipu"
        use_mimo = provider == "mimo" or str(model or "").lower().startswith("mimo-")
        self._ensure_enabled(provider)
        history = self._trim_messages(
            messages,
            max_turns=self.config.search_history_turns,
            char_limit=self.config.search_message_char_limit,
        )
        warnings: list[str] = []
        sources: list[dict[str, str]] = []

        grounding_block = self._format_grounding_block(grounding)
        source_refresh_line = (
            "本轮用户明确要求换一批或继续寻找资料：可以承接上文的论题和分析框架，但不得重复上文已经"
            "直接引用过的原句、具体引文页段或出处；直接引文只从本轮新提供的材料中选择，并优先展开新的"
            "文献支点。若新材料不足，宁可减少引文，也不要拿旧引文凑数。\n"
            if prefer_new_sources else ""
        )
        citation_retry_line = (
            "本轮是出处完整性修复重写：上一稿没有为多分论点论述保留下足够的可核验证据。必须继续使用下方"
            "同一批真实原文，让不同分论点分别采用与之最相关、最有把握的原句或转述；每个 [N] 都要紧跟其"
            "支持的句子。不要把整篇多节论述全部压在单一编号上，也不要输出上一稿、解释修复过程或使用弱相关来源。\n"
            if citation_retry else ""
        )

        if grounding_block:
            # 引文库接地（RAG）：调用模型前注入真实原文与准确出处，模型据此作答并准确标注引用。
            # 口径＝「增强作答」：以引文库原文为主要依据，库外内容可用模型自身知识补充但须明确区分。
            if use_zhipu:
                web_line = (
                    "2. 已同时为你启用联网检索：涉及实时信息或外部资料时可参考检索结果，引用网络资料时在正文注明来源标题；"
                    "但本站引文库的原文引证优先，其出处须按下述规则准确标注，不得与网络来源混淆。\n"
                )
            else:
                web_line = "2. 不要声称已经联网检索，也不要编造网络来源链接。\n"
            prompt = (
                "请回答用户的问题。下面是本站「马克思主义经典文献引文库」中与该问题相关的真实原文段落"
                "与准确出处（逐字摘自本站收录的中文版本，出处准确可信）：\n\n"
                f"{grounding_block}\n\n"
                "作答要求：\n"
                "1. 先据你自身的学理知识判断该问题是否有公认的分析框架或结构（例如异化劳动通常讲"
                "「与劳动产品、与劳动活动、与类本质、与他人」相异化这四重规定；再如某范畴的若干方面、"
                "某理论的发展阶段等），若有，就**先立起这一完整框架作为论述骨架、逐点展开**，"
                "避免因原文偏重某几点而把框架讲缺、遗漏公认的其他方面。"
                "在此骨架之上，以上述真实原文为各点的**主要依据并尽量充分地加以运用**："
                "从中选择最相关、最能支撑论点的原文加以引证和阐释；引用数量不设最低值，"
                "应按问题复杂度和论证需要决定，最多使用 12 个有效编号，不得为了数量使用弱相关条目；"
                "若问题需要多个分论点且下方有多条分别支撑不同层面的强相关原文，应为不同分论点选用各自证据，"
                "不得把整篇多节论述全部压在单一编号上；简单问题或确实只有一条相关材料时可以只用一条。"
                "解释性分析须从已给原文的明确前提出发，可以用「从概念上看」「由此可以理解为」"
                "等表述明确表明这是解释性推论，不得写成原文已明言的事实。"
                "**不得补充材料未提供的作者意图、后文内容、写作年代、历史背景、书名、卷次或页码**；"
                "绝不为分析性推论伪造引文或出处。\n"
                f"{web_line}"
                f"{source_refresh_line}"
                f"{citation_retry_line}"
                "3. 引用须在同一编号原文中逐字核验，可选择语义完整的连续短语、分句或完整句子。"
                "引文可以嵌入自己的句子，较长引文按需要独立成 Markdown 引用块；不要机械补长原句。"
                "转述、改写和直接引述均在其支持的论断之后标 [N]；一处可由多条来源共同支持。"
                "直接引述不改字、不以省略号拼接、不重复照录同一长句。版本卷页由引用卡片提供，不自行编写。\n"
                "4. 每处引证不要一引了之：要结合该引文所在著作的语境阐明其含义，说明它如何回应用户的"
                "问题；多条引文之间注意梳理相互关系（如思想发展的脉络、不同著作间的互证或侧重差异），"
                "使回答形成有层次的论述而非引文罗列。\n"
                "5. **把原文自然写进论证，不要在回答里讨论或复述材料清单与检索过程**：有对应原文就引用并按"
                "第 3 点在引文或相关转述之后标注 [编号]，推论须交代已有的原文前提。回答正文中不得出现「材料1」"
                "「材料[1]」「第1条材料」「上述材料」「所给材料」「所提供的材料」「所提供的原文」"
                "「所提供的文献」「根据材料」「材料没有直接提供」等检索报告式"
                "称呼，也不得写「材料[1]指出……」「从材料[2]可以看出……」。**[N] 只能作为句后引证标记，"
                "不能充当句子的主语或材料名称。正文也不得使用「文献指出」「有文献显示」「相关文献认为」"
                "「材料表明」「原文指出」「检索结果显示」等泛化引出。"
                "转述时写成 `劳动产品反过来成为支配劳动者的力量。[1]`。无需说明某处「检索到／未检索到／"
                "属于补充」，也不要出现「检索到的原文」「本次检索」这类字眼，更不要给没有原文的内容补脚注"
                "或页码；开头直接进入问题，结尾直接总结论证，让回答读起来是一篇自然、连贯的研究性论述，"
                "而不是对输入材料逐条作答的检索报告。开头和结尾尤其不要评价「这些材料／原文提供、覆盖或支撑了"
                "哪些方面」，直接陈述理论框架、论证关系和结论。\n"
                "6. 使用中文回答，紧扣问题、结构清晰，并**充分展开论述**：一般写到 800～1500 字"
                "（确属简单的事实性问题可酌情从简）；务必把话说完整、在句末标点收尾，"
                "不堆砌与问题无关的内容。\n"
                "7. 排版用 Markdown、清晰易读：每个分论点用 `### 小标题` 起头；较长的逐字引文用 "
                "`> 引用块` 单独、完整呈现（引用块之后仍按第 3 点只标注 [编号]，不写书名卷次页码）；"
                "关键术语、核心论断用 `**加粗**` 突出。**引用块内照录原文、不要加粗**；正文中的 "
                "`**加粗**` 务必成对闭合，不要残留单个 `**`。不要用一级/二级大标题。\n\n"
                f"用户问题：{question}"
            )
            system_content = (
                "你是一位严谨的中文学术研究者；快速问答同样遵循学术论文的论证与引证标准。"
                "篇目与责任者只能使用已核验的结构化字段，绝不猜测归属、编造引文和版本卷页。"
                "没有材料支持的作者意图、后文、年代和历史背景不得补写；不输出检索过程。"
                + _ACADEMIC_WRITING_RULES
            )
        elif use_zhipu:
            prompt = (
                "请回答用户的问题。\n"
                "要求：\n"
                "1. 使用中文回答，尽量准确、完整、结构清晰。\n"
                "2. 已为你启用联网检索：涉及实时信息或外部资料时，优先依据检索结果作答，"
                "并在正文中注明所依据来源的标题；检索结果不足时如实说明，绝不编造来源或链接。\n"
                f"{source_refresh_line}"
                "3. 本次未启用本站引文库，因此网络结果不得用于认定经典文献逐字引文或人物—篇目归属；"
                "不得输出经典原文的逐字引文、具体页码、本站式 [N]，也不得写未经本站语料核验的"
                "“某人在《某篇》中指出/强调”式具体归属。\n"
                "4. 排版用 Markdown、清晰易读：分论点用 `### 小标题` 起头，关键术语与核心论断用 "
                "`**加粗**` 突出；不要使用 Markdown `> 引用块`。需要给出示例、改写稿、"
                "过渡句或建议文本时，用普通段落或列表完整呈现；不要用一级/二级大标题。\n\n"
                f"用户问题：{question}"
            )
            system_content = "你是一位严谨、清楚、重视来源标注的中文研究助手。"
        else:
            prompt = (
                "请回答用户的问题。\n"
                "要求：\n"
                "1. 使用中文回答，尽量准确、完整、结构清晰。\n"
                "2. 不要声称已经联网检索，也不要编造具体来源链接。\n"
                "3. 本次未启用本站引文库：可以使用模型自身知识作一般性分析，但不得输出逐字引文、具体页码、"
                "方括号来源编号，也不得写未经本站证据核验的“某人在《某篇》中指出/强调”式具体归属。"
                "如果需要实时资料或外部来源核验，要明确提示用户当前未启用联网检索。\n"
                f"{source_refresh_line}"
                "4. 排版用 Markdown、清晰易读：分论点用 `### 小标题` 起头，关键术语与核心论断用 "
                "`**加粗**` 突出；不要使用 Markdown `> 引用块`。需要给出示例、改写稿、"
                "过渡句或建议文本时，用普通段落或列表完整呈现；不要用一级/二级大标题。\n\n"
                f"用户问题：{question}"
            )
            system_content = "你是一位严谨、清楚、重视来源标注的中文研究助手。"

        # 接地作答更长：给独立下限避免被压低的通用上限截断；普通对话仍沿用配置值。
        # 若个别通道拒绝放宽后的上限（报 token 上限类错误），按阶梯回退到历史验证值重试，
        # 与研究综述 _chat_research_review 的阶梯同构——绝不让「放宽上限」本身弄垮接地问答。
        answer_max_tokens = self.config.search_answer_max_tokens
        if grounding_block:
            answer_max_tokens = max(answer_max_tokens, GROUNDED_ANSWER_MIN_TOKENS)
        chat_messages = [
            {"role": "system", "content": system_content},
            *history,
            {"role": "user", "content": prompt},
        ]
        web_query = self.zhipu_search_query(question) if use_zhipu else None
        token_ladder = [answer_max_tokens]
        if grounding_block:
            # 逐级回退（10000 → 6000 → 4000）：中间挡是上一版生产值，避免一被拒就摔回最低挡。
            for rung in (GROUNDED_ANSWER_MID_TOKENS, GROUNDED_ANSWER_FALLBACK_TOKENS):
                if rung < answer_max_tokens and rung not in token_ladder:
                    token_ladder.append(rung)
        answer = ""
        for step, budget in enumerate(token_ladder):
            try:
                answer = self.chat_complete(
                    chat_messages,
                    max_tokens=budget,
                    provider=provider,
                    sources_out=sources,
                    web_search_query=web_query,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
                break
            except AIServiceError as exc:
                if step == len(token_ladder) - 1 or not self._looks_like_token_limit_error(exc):
                    raise
                LOGGER.warning(
                    "Search-chat token budget %s rejected, retrying lower budget: %s", budget, exc
                )
        # 反伪造出处兜底：剔除模型万一自造的伪脚注/尾注行（真实出处只走 [编号]，前端另渲染 citations）。
        if grounding_block and answer:
            answer = self._strip_fabricated_citation_lines(answer)
            if use_mimo:
                answer = self._strip_mimo_trailing_reference_appendix(answer)
        elif answer:
            answer = self.sanitize_ungrounded_answer(answer)
            if not answer:
                answer = "可以从概念和理论结构上继续分析，但本次未启用本站引文库，因此不提供未经核验的引文或具体出处归属。"
        if use_zhipu and not sources:
            # 联网失效必须对用户可见：否则模型可能按提示词“演”出参考来源说明，造成已联网的假象。
            warnings.append("本次未获取到联网检索来源（检索服务暂不可用或已降级），回答基于模型自身知识。")
        return AIAnswer(
            answer_markdown=answer,
            sources=sources,
            used_web=bool(sources),
            warnings=warnings,
        )

    def generate_research_review(self, topic: str, passages: list[dict[str, Any]], *, should_cancel=None, model: str | None = None, context_messages: list[dict[str, Any]] | None = None, provider: str | None = None, reasoning_effort: str | None = None, integrity_retry: bool = False) -> str:
        """研究综述对外入口：先占用独立的「研究并发闸」（与交互式 AI 名额隔离，避免几篇并发综述把
        吉祥物/问答判忙），再委托实现。``should_cancel`` 为可选取消回调（客户端断开时由 SSE 层置位），
        在每个模型调用边界检查，命中则带着已成文提前收尾、尽快释放名额。``model`` 为可选模型档位覆盖
        （前端「模型选择」flash/pro，白名单已在调用方校验；None 则用服务端默认模型）。``context_messages``
        为可选的此前对话（[{role,content}]），仅作背景让综述承接对话语境，不改变接地检索。"""
        if not _RESEARCH_REVIEW_SEMAPHORE.acquire(timeout=_AI_HTTP_ACQUIRE_TIMEOUT):
            raise AIServiceError("AI 当前访问量较大，请稍后重试。")
        try:
            with research_ai_http_context():
                return self._generate_research_review_impl(topic, passages, should_cancel=should_cancel, model=model, context_messages=context_messages, provider=provider, reasoning_effort=reasoning_effort, integrity_retry=integrity_retry)
        finally:
            _RESEARCH_REVIEW_SEMAPHORE.release()

    @staticmethod
    def _format_review_context(context_messages: list[dict[str, Any]] | None) -> str:
        """把此前对话压成一小段背景（供综述承接语境）。限最近 6 条、每条限长；空则返回空串。"""
        if not context_messages:
            return ""
        lines: list[str] = []
        # 研究综述也需要承接多轮论证；保留最近 5 轮，但按角色限长并设总额，
        # 防止长综述挤占本轮新检索原文的 token 空间。
        total_chars = 0
        for m in context_messages[-10:]:
            if not isinstance(m, dict):
                continue
            per_message_limit = 1400 if m.get("role") == "assistant" else 1000
            txt = " ".join(str(m.get("content") or "").split())[:per_message_limit]
            if txt:
                remaining = 8000 - total_chars
                if remaining <= 0:
                    break
                txt = txt[:remaining]
                role = "助手" if m.get("role") == "assistant" else "用户"
                lines.append(f"{role}：{txt}")
                total_chars += len(txt)
        if not lines:
            return ""
        return (
            "【此前对话背景（仅供理解语境、承接上文；综述的论断与引文仍只依据下方检索到的真实原文，"
            "不得据此背景杜撰原文或出处）】：\n" + "\n".join(lines) + "\n\n"
        )

    def _generate_research_review_impl(self, topic: str, passages: list[dict[str, Any]], should_cancel=None, model: str | None = None, context_messages: list[dict[str, Any]] | None = None, provider: str | None = None, reasoning_effort: str | None = None, integrity_retry: bool = False) -> str:
        """研究型检索综述：依据检索到的**真实原文**写一篇接地综述，文中用 [N] 标注来源。

        ``passages`` 为已编号的真实命中 ``{"index","citation","text"}``（全部来自 corpus 真实命中）。
        严格接地：只依据给定原文，每处论断标注来源编号，绝不编造原文/观点/出处——引文不可伪造。
        """
        self._ensure_enabled(provider)
        use_mimo = provider == "mimo" or str(model or "").lower().startswith("mimo-")
        block = self._format_grounding_block(passages)
        if not block:
            raise AIServiceError("没有可用于综述的检索原文。")
        topic = " ".join(str(topic or "").split())[:600]
        context_block = self._format_review_context(context_messages)
        integrity_retry_line = (
            "本轮是出处完整性修复重写：上一稿未能为多节长文保留下足够的可核验证据。请继续且只能使用下方"
            "同一批原文，让不同小节分别采用与其论点最相关的证据，不得把整篇长文全部压在单一编号上；"
            "每个保留的直接引文和人物—篇目引出都必须能由同一 [N] 精确验证，不得使用弱相关来源。"
            "只输出重新写成的正式综述，不解释修复过程。\n"
            if integrity_retry else ""
        )
        review_target_cjk_chars = RESEARCH_REVIEW_TARGET_CJK_CHARS
        review_min_cjk_chars = RESEARCH_REVIEW_MIN_CJK_CHARS
        mimo_closing_rule = (
            "8. MiMo 结构硬规则：全文只能出现一次收束章节，且只能在全部正文完成后的最后使用 `## 小结`；"
            "正文中途不得输出“小结”“总结”“结语”“结论”等收束标题，也不得先小结后继续展开。\n"
            if use_mimo else ""
        )
        prompt = (
            "请围绕用户的研究论题"
            + ("（如附有此前对话背景，请自然承接其语境、可在开篇点明承接关系）" if context_block else "")
            + "，写一篇较充分的学术综述。下面是本站「马克思主义经典文献库」中与该论题相关的真实原文段落"
            "与准确出处（逐字摘自本站收录文本，版本卷页以每条已给字段为准）：\n\n"
            f"{block}\n\n"
            f"{context_block}"
            f"{integrity_retry_line}"
            "写作要求：\n"
            "1. 紧扣研究论题：先据你自身的学理知识判断该论题有无公认的分析框架/结构（如异化劳动的四重规定、"
            "某理论的几个方面或发展阶段等），若有则据以搭起完整的小节骨架、不遗漏公认方面，"
            "无则按问题内在层次自行分节；全篇分 4-6 个有标题的小节，有逻辑地综合上述原文所反映的思想，"
            "形成一篇连贯、详实、自然写完的综述。正式正文必须达到 5000 个中文汉字以上，"
            f"建议以约 {review_target_cjk_chars} 个中文汉字为目标；即使限定单篇目也不得降低这一篇幅门槛。"
            "应以增加有材料支撑的分析层次达到篇幅，不得重复铺陈、拉长引文或添加无来源事实。"
            "动笔前先在内部规划各节篇幅（不要输出规划过程），优先在本轮一次完整写完。\n"
            "**框架仅为骨架，一切论断与展开须以上述真实原文为准加以修正、充实**："
            "原文有所侧重、差异或深化处，一律以原文为准，不生搬硬套教科书式框架。\n"
            "2. 围绕每个小节的论证需要择要使用材料。引用数量不设最低值、也不要求凑到固定数量，"
            "应按输入内容和论述需要决定，全文最多使用 30 个高相关编号。用户限定单一篇目时，"
            "只从该篇目内选择真正需要的段落；宁可少引，也不得为了凑数堆砌弱相关材料。若多个小节有不同的"
            "直接论据且下方提供了相应强相关原文，应将这些证据分别用于对应小节，不得让一条编号承担整篇长文。\n"
            "2a. 页码、年代页眉、整本汇编或丛书标题、卷册标题、PDF生成器版权水印、下载网址、联系邮箱"
            "和页眉页脚都不是正文原句；即使材料中因排版抽取而残留，也绝不能把它们放进双引号或 Markdown"
            "引用块，不能用它们连接跨页句子。\n"
            "3. 文中每一处依据原文的论断，须在句末用方括号标注来源编号，如 [1]、[2][4]；一处可引多条。\n"
            "4. 所有直接引述都须在同一编号的原文字段中逐字找到，保留所选连续片段的措辞和标点，"
            "不能确认逐字一致时应转述且不加引号。原文内部的嵌套引号须原样保留，不分别扩写成重复段落。"
            "引文末尾或其所支撑的句末标 [N]，引用块编号紧跟块内原文末尾；转述和改写也应标明来源。"
            "责任者和篇目已核验时可据论证需要署名引述或转述，不要求每次完整介绍篇名。\n"
            "5. 综述的**框架结构**可参酌公认学理，但**具体论断、引文与出处只依据上述检索到的真实原文**，"
            "不得编造原文、观点或出处；给定原文段落的出处以所附卷次、页码为准、直接采信，"
            "篇目与责任者只能采用每条材料中明确给出的已核验字段；标为未核验时绝不补写或猜测，也不要用设问句"
            "质疑其来源（不要写「这段话是否出自……？」之类）。\n"
            "5a. 学理框架只能用来组织问题和作出解释性推论，不得借此添加材料未提供的作者意图、"
            "后文内容、著作年代、历史背景、书名、版本或页码。「由此可以理解为」之类的推论必须与原文事实明确区分，"
            "不能把模型记忆写成本轮文献已经证明的内容。\n"
            "5b. **出处只能用一种形式：指向上述真实原文的方括号编号 [N]（如 [1]、[3][4]）。**"
            "严禁自造任何别的引证或注释体系：不得添加脚注或尾注（①②③、¹²、注1 之类），"
            "不得自行写出「参见《……》第 X 卷第 Y 页」这类由你给出的书名＋卷次＋页码，"
            "也不得给编号原文以外的任何句子附上具体页码、卷次或版本号。\n"
            "5c. 框架中缺少原文依据的方面，不补写具体作者观点；充分展开已有证据支持的命题，"
            "交代其概念内涵、推导关系与适用条件。证据覆盖提示由页面单独显示，正文不叙述检索流程，"
            "也不把尚缺依据的结论写成已获证明的事实。\n"
            "6. 用规范的学术中文，严谨、有条理；开篇点出论题，中段充分展开，结尾自然小结，"
            "必须把完整文章写完，不要在小节中途、句子中途或论证尚未完成时停止。\n"
            "7. 输出格式硬规则：第一行写【综述正文开始】，最后一行写【综述正文结束】；"
            "两者之间只能放正式综述正文。不要输出任何思考过程、分析过程、写作计划、提示词复述、"
            "自我说明或“我需要先……”之类内容。正式正文以“## 研究综述”开头，并以“## 小结”收束。\n\n"
            f"{mimo_closing_rule}"
            f"研究论题：{topic}"
        )
        system_message = {
            "role": "system",
            "content": "你是一位严谨的马克思主义经典文献研究者，擅长依据真实原文撰写有据可查的"
                       "学术综述：凡采用材料中的事实或观点都准确标注来源编号，灵活平衡转述、改写和直接引述，"
                       "绝不编造引文、观点或出处。"
                       "直接引文须是单个 [N] 原文里逐字一致的连续短语、分句或完整句子，不得用省略号拼接。"
                       "不得补充材料未提供的作者意图、后文、年代或历史背景；分析性推论必须与原文事实区分。"
                       "来源一律只用指向检索原文的 [N] 方括号编号，绝不自造脚注（①②）或"
                       "「参见《…》第 X 页」式的书名页码出处，宁可不给出处也不杜撰。"
                       "正文引用要融入正常论文论证，不得机械重复篇名或固定引出语，也不得用“文献指出”"
                       "“有文献显示”“材料表明”“原文指出”等泛称引出引文；"
                       "只有编号材料明确给出已核验责任者和篇目时，才能写人物—篇目归属。"
                       "只输出最终综述正文，绝不输出思考过程、推理过程、分析草稿或提示词说明。" + _ACADEMIC_WRITING_RULES,
        }
        # 整篇生成的总挂钟预算：每次发起模型调用前校验剩余预算，确保在 Cloudflare 边缘超时前回 JSON。
        # 首轮给足预算一次写完；后续修复/续写/重写只有在剩余预算充足时才追加，否则带着已成文返回。
        deadline = time.monotonic() + RESEARCH_REVIEW_TOTAL_BUDGET_SECONDS

        def _budget_left() -> float:
            return deadline - time.monotonic()

        def _cancelled() -> bool:
            # 客户端断开后，后续修复/续写/重写都不再发起：带着已成文收尾，尽快释放 AI 名额、不做废功。
            return should_cancel is not None and bool(should_cancel())

        raw_answer = self._chat_research_review(
            [system_message, {"role": "user", "content": prompt}],
            max_tokens=RESEARCH_REVIEW_MAX_TOKENS,
            deadline=deadline,
            model=model,
            provider=provider,
            reasoning_effort=reasoning_effort,
        )
        answer = self._sanitize_research_review_output(raw_answer)
        if use_mimo and answer:
            answer = self._dedupe_research_review_closings(answer)
        if not answer and not _cancelled() and _budget_left() >= RESEARCH_REVIEW_FOLLOWUP_MIN_HEADROOM_SECONDS:
            repair_prompt = (
                "上一轮输出没有得到合格的正式综述正文。请重新生成一篇完整的学术综述，"
                "不要输出思考过程、分析过程、写作计划或自我说明；只输出【综述正文开始】与"
                "【综述正文结束】之间的正式文章。\n\n"
                f"{prompt}"
            )
            raw_answer = self._chat_research_review(
                [system_message, {"role": "user", "content": repair_prompt}],
                max_tokens=RESEARCH_REVIEW_MAX_TOKENS,
                deadline=deadline,
                model=model,
                provider=provider,
                reasoning_effort=reasoning_effort,
            )
            answer = self._sanitize_research_review_output(raw_answer)
            if use_mimo and answer:
                answer = self._dedupe_research_review_closings(answer)
        if not answer:
            raise AIServiceError("模型未返回可用的正式综述正文。")
        for _ in range(RESEARCH_REVIEW_CONTINUATION_ATTEMPTS):
            is_complete = self._research_review_complete(answer)
            cjk_chars = self._research_review_cjk_chars(answer)
            if is_complete and cjk_chars >= review_min_cjk_chars:
                break
            # 预算不足以再安全跑一轮续写、或客户端已断开，就带着当前已成文返回，绝不冒险顶过 CF 边缘超时/做废功。
            if _cancelled() or _budget_left() < RESEARCH_REVIEW_FOLLOWUP_MIN_HEADROOM_SECONDS:
                break
            continuation_base = answer
            closing_removed = False
            if cjk_chars < review_min_cjk_chars and (is_complete or use_mimo):
                continuation_base, closing_removed = self._research_review_without_final_closing(answer)
            length_instruction = (
                f"当前正文约有 {cjk_chars} 个中文汉字，低于约 {review_target_cjk_chars} 字的目标。"
                f"请在已有论证基础上新增有材料支撑的分析层次，使合并后的全文至少达到 "
                f"{review_min_cjk_chars} 个中文汉字；不要靠重复观点、拉长引文或空话凑字数。"
                if cjk_chars < review_min_cjk_chars
                else ""
            )
            closing_instruction = (
                "原稿末尾的小结已移除；请先补写一至两个实质性小节或充分扩展尚薄弱的小节，最后重新写出唯一的“## 小结”。"
                if closing_removed
                else "请完成尚未展开充分的部分，并在全文最后写出唯一的“## 小结”。"
            )
            mimo_continuation_rule = (
                "续写期间不得复述已有开篇或任何既有小节；只在本轮新增正文全部写完后输出一次 `## 小结`，"
                "不得生成多个小结、总结、结语或结论章节。\n"
                if use_mimo else ""
            )
            continuation_prompt = (
                "下面这篇研究综述还没有自然完成。请从已有正文的末尾继续写下去，不要重写全文，不要重复已经写过的段落；"
                "仍然只能依据同一批真实原文，并继续使用已有的 [N] 来源编号。请继续完成尚未展开充分的部分、"
                "补足必要的小节，并在论证自然完成后写出完整小结。不要为了尽快收束而只写几句模板结尾，"
                "也不要仓促结束；应把文章剩余部分自然写完。只输出续写正文，不要输出任何思考过程、分析过程、"
                f"写作计划或自我说明。\n{length_instruction}\n{closing_instruction}\n{mimo_continuation_rule}\n"
                f"研究论题：{topic}\n\n"
                f"真实原文与出处：\n{block}\n\n"
                f"已生成正文：\n{continuation_base[-5000:]}"
            )
            raw_continuation = self._chat_research_review(
                [system_message, {"role": "user", "content": continuation_prompt}],
                max_tokens=RESEARCH_REVIEW_CONTINUATION_MAX_TOKENS,
                deadline=deadline,
                model=model,
                provider=provider,
                # 首轮已用 Pro 完成整体推理；补写只沿既有结构填足薄弱部分。关闭补写轮思考可把
                # token 和时间集中给正文，又不牺牲首轮的研究判断与材料组织质量。
                disable_thinking=True,
                reasoning_effort="off",
            )
            continuation = self._sanitize_research_review_output(raw_continuation, allow_fragment=True).strip()
            if not continuation:
                break
            answer = f"{continuation_base.rstrip()}\n\n{continuation}"
            if use_mimo:
                answer = self._dedupe_research_review_closings(answer)
        if not self._research_review_complete(answer) and not _cancelled() and _budget_left() >= RESEARCH_REVIEW_FOLLOWUP_MIN_HEADROOM_SECONDS:
            rewrite_prompt = (
                "前面的版本仍未自然写完。请重新写一篇完整的学术综述，保持严谨但不要输出思考过程。"
                "这次请控制整体结构，确保文章一次性完整收束：有开篇、有 3-5 个自然展开的小节、有充分论证、"
                "最后有自然的小结。不要复制未完成版本，不要只补一个结尾；请重新组织成完整文章。\n\n"
                f"{prompt}"
            )
            raw_rewrite = self._chat_research_review(
                [system_message, {"role": "user", "content": rewrite_prompt}],
                max_tokens=RESEARCH_REVIEW_REWRITE_MAX_TOKENS,
                deadline=deadline,
                model=model,
                provider=provider,
                reasoning_effort=reasoning_effort,
            )
            rewrite = self._sanitize_research_review_output(raw_rewrite)
            if rewrite:
                answer = self._dedupe_research_review_closings(rewrite) if use_mimo else rewrite
        if use_mimo:
            answer = self._dedupe_research_review_closings(answer)
        LOGGER.info(
            "Research review completed (cjk_chars=%s, structurally_complete=%s)",
            self._research_review_cjk_chars(answer),
            self._research_review_complete(answer),
        )
        if self._research_review_cjk_chars(answer) < review_min_cjk_chars:
            raise AIServiceError("研究综述未达到 5000 个中文汉字的篇幅要求。")
        return answer

    def expand_associative_query(self, gist: str, *, deep: bool = False) -> dict:
        """联想检索第一步：把用户的“大意/关键词”扩展为可在语料中检索的线索。

        返回 ``{"quotes": [...1-3...], "keywords": [...4-10...]}``。这些只作为检索输入，
        其内容绝不直接作为结果展示——最终引文一律由真实命中生成。

        ``deep=True`` 只改变缓存/重试档位，不改变模型：所有联想检索内部步骤都固定
        使用 DeepSeek Flash 非思考，且由网站承担费用。
        """
        self._ensure_enabled()
        gist = " ".join(str(gist or "").split())[:600]
        if not gist:
            return {}
        # 缓存键必须带档位：两档抽出的线索不同，混用会让研究档悄悄吃到快档的结果（反之亦然）。
        cache_key = ("deep|" if deep else "fast|") + gist
        cached = _ASSOC_EXPAND_CACHE.get(cache_key)
        if cached is not None:
            _ASSOC_EXPAND_CACHE.move_to_end(cache_key)
            return dict(cached)
        prompt = (
            "本检索库收录了以下经典作家与党和国家重要文献的中文著作："
            "马克思、恩格斯（《文集》《全集》《选集》）、列宁、斯大林，李大钊、陈独秀，"
            "毛泽东、周恩来、陈云，邓小平、江泽民、胡锦涛的文集、选集、全集或年谱，"
            "习近平（《谈治国理政》《经济文选》，及新时代中国特色社会主义思想、经济思想、法治思想、"
            "生态文明思想、文化思想、党的建设等专题《学习纲要》《概论》）、"
            "西方马克思主义专题（卢卡奇、科尔施、葛兰西、布洛赫、霍克海默尔与阿多诺、阿尔都塞、列斐伏尔），"
            "以及党代会报告、历届全会公报、十八大/十九大以来重要文献选编、五年规划纲要等。\n"
            "用户想在其中找到与下面这段输入最匹配的原文。输入可能是“大意描述”，也可能是“记得的只言片语/残句”。"
            "请先在心里推理：它与**哪些作者/文献群**直接相关、其中谁是主文库，以及最可能涉及哪段论述、哪一主题与篇章，"
            "再据此输出便于在中文原著中逐字定位的检索线索。**切勿**把当代中国政治话语（如“中华民族伟大复兴”“中国式现代化”）"
            "硬套成 19 世纪马恩术语——该用谁的话就用谁的话。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown 代码块，格式：\n"
            '{"corpus": ["最相关的作者或文献群"], "intent": "locate 或 research", "quotes": ["最可能的原文整句"],'
            ' "fragments": ["逐字短语1", "逐字短语2"], "keywords": ["正文实词1", "正文实词2"],'
            ' "chapter_keywords": ["篇章/标题词1", "篇章/标题词2"],'
            ' "facets": [{"aspect": "侧面名", "keywords": ["该侧面实词1", "该侧面实词2"]}]}\n'
            "要求：\n"
            "1. corpus 最先判断：按主次给 1-6 个与输入直接相关的作者或文献群，取值从这些里选——"
            "“马克思恩格斯 / 列宁 / 斯大林 / 西方马克思主义 / 李大钊 / 陈独秀 / 毛泽东 / 周恩来 / 陈云 / "
            "邓小平 / 江泽民 / 胡锦涛 / 习近平 / 党和国家文献”。不要为了凑数扩到无关文库。"
            "比较或思想史问题必须同时列出涉及的各方，例如“马恩国家观与列宁国家观的异同”→"
            "[“马克思恩格斯”,“列宁”]；“中国式现代化”→以“习近平”为首，同时列“党和国家文献”，"
            "研究其历史脉络时再列“邓小平/毛泽东/江泽民/胡锦涛”。"
            "单一出处问题仍只列最可能的一群，例如“剩余价值/异化/资本论”→“马克思恩格斯”，"
            "“帝国主义是资本主义最高阶段”→“列宁”，“改革开放/一国两制”→“邓小平”。"
            "**真的拿不准且没有明确作者/时代信号时才留空数组**。\n"
            "2. quotes 给 1-3 句，尽量逐字还原**对应著作**的书面语措辞（马恩列用人民出版社 19 世纪译文风格的政治经济学/"
            "哲学术语；毛及以后中国领导人、党和国家文献用其时代的现代汉语政治表述），而非口语转述；记不准就给最可能的措辞。\n"
            "3. fragments 最重要：给 5-12 个你认为会**一字不差**出现在**对应著作**正文中的特征短语（4-12 字）——"
            "马恩如“社会关系的总和”“全世界无产者，联合起来”；习近平如“中华民族伟大复兴”“中国式现代化”“人类命运共同体”。"
            "这是定位成败的关键，宁可多给几个不同位置、不同表述的短语。\n"
            "4. keywords 给 6-12 个正文里区分度高的实词：既要从大意**推理**出原著可能用到的术语，"
            "也要从用户给的只言片语里**直接截取**关键实词；涵盖近义/不同译法（如“异化/外化”），"
            "避免“的/是/社会/发展”这类高频泛词。\n"
            "5. chapter_keywords 给 3-8 个可能出现在**篇章或标题**中的词（著作名、章节主题、概念名），"
            "如“费尔巴哈”“帝国主义”“家庭、私有制和国家”“新发展理念”“全面从严治党”，用于定位所属篇章；"
            "著作名可给简称/全称两种写法（如“共宣”与“共产党宣言”）。\n"
            "6. intent 判断用户意图：若是【找一段他大概记得、想定位出处的特定原文】（给了残句，或明确著作+主题），"
            '填 "locate"；若给的是【一个研究性的想法、论题或大意，想找一批相关引文来佐证或展开研究】，'
            '填 "research"。拿不准填 "research"。\n'
            "7. facets 总是给（无论 intent 取何值）：把输入拆成 2-4 个不同侧面/角度，每个侧面给 aspect（侧面名）"
            "和 3-6 个该侧面的检索实词（可含近义/不同译法），用于按侧面广召回——便于用户切到「研究辅助」时铺开线索。\n"
            f"\n用户输入：{gist}"
        )
        # DeepSeek 即便 temperature=0 也偶尔返回空/截断的 JSON，导致“同一输入有时搜不到”。
        # 故重试至多 3 次，命中可用线索即止；成功结果入缓存，使同一输入后续稳定可复现。
        messages = [
            {"role": "system", "content": "你是精通马克思主义经典作家著作、中国化马克思主义重要文献、西方马克思主义专题著作及党和国家重要文献措辞与篇目结构的中文检索专家。"},
            {"role": "user", "content": prompt},
        ]
        plan: dict = {}
        # 研究档同样固定 Flash：内部检索不消耗用户钱包，也不因会员选择 MiMo/Pro 而变档。
        fast_model = ASSOC_EXPAND_MODEL
        deep_model = ASSOC_EXPAND_DEEP_MODEL
        attempts = [deep_model, fast_model, fast_model] if deep else [fast_model] * 3
        for model_name in attempts:
            # 1500（原 900）：JSON 现含 intent + facets 多侧面，900 会把 keywords/chapter_keywords
            # 截断在数组中途，导致整段解析失败、plan 退空，拖累整条联想检索。给足余量避免截断。
            # disable_thinking=True 是**结构化抽取任务的硬要求**：开思考时 flash 会把 1500 全烧在
            # 思考上（finish=length，实测 6/6 全废）、一个字 JSON 都不吐；关掉后稳定产出干净 JSON。
            # 这里不靠 _is_fast_tier 自动判定，是因为该步换成 pro（研究档）同样必须关。
            try:
                answer = self.chat_complete(
                    messages, max_tokens=2048, temperature=0.0, model=model_name,
                    disable_thinking=True, reasoning_effort="off",
                )
            except AIServiceError as exc:
                # 深档模型抽风不该拖垮整条检索：记一笔继续走下一次尝试（末次仍是 flash）。
                LOGGER.warning("Associative expand attempt failed (model=%s): %s", model_name, exc)
                continue
            parsed = _extract_json_object(self._coerce_message_content(answer))
            if isinstance(parsed, dict) and any(
                parsed.get(k) for k in ("quotes", "fragments", "keywords", "chapter_keywords")
            ):
                plan = parsed
                break
        if plan:
            _ASSOC_EXPAND_CACHE[cache_key] = plan
            _ASSOC_EXPAND_CACHE.move_to_end(cache_key)
            while len(_ASSOC_EXPAND_CACHE) > _ASSOC_EXPAND_CACHE_MAX:
                _ASSOC_EXPAND_CACHE.popitem(last=False)
        return plan

    def rank_associative_candidates(
        self, gist: str, candidates: list[dict], intent: str | None = None, *, detailed: bool = False
    ) -> list[dict]:
        """联想检索第二步：在已定位的真实候选段落中，按与大意的匹配度排序并给出理由。

        ``candidates`` 为已编号的真实命中（含真实引文/上下文）。模型只能从给定候选中选择，
        返回 ``[{"index": N, "confidence": 0-100, "reason": "..."}]``，不得编造或新增条目。
        ``intent=="research"`` 时改用研究口径：额外标注 relation(support/tension/extend) 并鼓励覆盖不同侧面。
        ``detailed=True`` 保留更完整的命中上下文和篇章名，用于首页真正的语义排序。
        """
        self._ensure_enabled()
        gist = " ".join(str(gist or "").split())[:600]
        if not candidates:
            return []
        lines: list[str] = []
        for i, cand in enumerate(candidates):
            citation = str(cand.get("citation") or "").strip()
            context = str(cand.get("context") or "")
            context = context.replace("[[H]]", "").replace("[[/H]]", "")
            context = " ".join(context.split())[:600 if detailed else 160]
            section = str(cand.get("work_title") or cand.get("section_title") or "")[:100]
            chapter_clue = f" | 所属篇章：{section}" if detailed and section else ""
            lines.append(f"[{i}] {citation}{chapter_clue} | 上下文：{context}")
        if intent == "research":
            prompt = (
                "用户给出的是一个研究性论题/想法（见下）。请从给定候选原文段落中，挑出能服务于该研究的，"
                "按对研究的价值从高到低排序，并标注每条与论题的关系。\n"
                "只输出一个 JSON 数组，不要解释、不要 Markdown 代码块，格式：\n"
                '[{"index": 候选编号, "confidence": 0-100, "relation": "support 或 tension 或 extend",'
                ' "reason": "一句话说明这条如何服务于该研究"}]\n'
                "relation 取值：support=直接支撑论题；tension=构成张力/反例/需辨析；extend=延伸论题或提供背景。\n"
                "要求：只能从给定候选编号中选择；不得编造页码或新增条目；优先覆盖论题的不同侧面，"
                "避免清一色同一出处；若没有任何候选可用，返回空数组 []。\n\n"
                f"研究论题：{gist}\n\n候选：\n" + "\n".join(lines)
            )
        else:
            prompt = (
                "用户的大意描述如下，请从给定候选原文段落中，挑出语义上真正匹配的，"
                "按匹配度从高到低排序。\n"
                "只输出一个 JSON 数组，不要解释、不要 Markdown 代码块，格式：\n"
                '[{"index": 候选编号, "confidence": 0-100, "reason": "一句话说明为何匹配"}]\n'
                "要求：只能从给定候选编号中选择；不得编造页码或新增条目；"
                "若没有任何候选匹配，返回空数组 []。\n\n"
                f"大意：{gist}\n\n候选：\n" + "\n".join(lines)
            )
        answer = self.chat_complete(
            [
                {"role": "system", "content": "你是严谨的中文文献核对助手，只在给定候选中判断，绝不编造。"},
                {"role": "user", "content": prompt},
            ],
            # 结构化 JSON 全有或全无：给足余量避免截断。研究意图重排池更大(20 条)且每条多一个
            # relation 字段，1100 会截断在数组中途→整段解析失败→无标注；故抬到 2000。
            max_tokens=3072,
            temperature=0.0,  # 重排也走确定性，保证同一输入结果稳定
            # 重排只在已定位的真实候选中判断匹配度，刻意走更轻量的 flash 档省成本；
            # 召回线索的「扩展」步不传 model，仍走主通道强模型保质量。
            model=ASSOC_RERANK_MODEL,
            # 同「扩展」步：结构化 JSON 全有或全无，思考只会吃掉预算把数组截断，一律关掉。
            disable_thinking=True, reasoning_effort="off",
        )
        parsed = _extract_json_object(self._coerce_message_content(answer))
        if isinstance(parsed, dict):
            # 容忍模型把数组包在 {"results": [...]} / {"ranking": [...]} 里
            for key in ("results", "ranking", "items", "data"):
                if isinstance(parsed.get(key), list):
                    parsed = parsed[key]
                    break
            else:
                if detailed:
                    raise AIServiceError("语义排序返回格式无效")
                parsed = []
        if detailed and not isinstance(parsed, list):
            raise AIServiceError("语义排序返回格式无效")
        return parsed if isinstance(parsed, list) else []

    def _pdf_chat_instructions(self, quick_mode: bool, use_zhipu: bool) -> str:
        style_instructions = (
            "控制在约 300-500 字，但仍须使用下述四段结构，不要挤成一个长段落。"
            if quick_mode
            else "可适度展开，但每个判断都要能回到给定原文，不要写成脱离文本的泛泛评论。"
        )
        web_instruction = (
            "3. 已为你启用联网检索：可结合检索结果补充背景或最新研究，引用网络资料时在正文注明来源标题；"
            "检索结果与原文无关时以本地上下文为准，绝不编造来源或链接。\n"
            if use_zhipu
            else "3. 可以解释必要的通用概念，但不得补充给定上下文未提供的作者意图、后文内容、"
                 "历史背景、书名、年代或页码；不要声称已经联网检索，也不要编造具体来源链接。\n"
        )
        return (
            "请对用户当前阅读的 PDF 页面做可核验的学术导读。\n"
            "硬性要求：\n"
            "1. 优先解释用户选中的内容；未选中时解释当前页。只能把“当前页全文、前后页摘录”"
            "视为书中原文，不能把模型知识冒充原文。\n"
            "2. 先辨认本页作者实际在讨论什么，再说明论证步骤；不得补写原文没有的结论，不得把"
            "相邻页内容误说成当前页内容。\n"
            f"{web_instruction}"
            "4. “原文依据”必须从给定上下文逐字短引 1-3 处；若文字层残缺或语句明显异常，要明确"
            "提示“文字层可能识别有误”，不要擅自润色后再加引号。\n"
            "4a. 每处原文引用必须是「当前页全文、前后页摘录或用户选中文字」里的一段连续子串，"
            "禁止用省略号拼接不连续文字；找不到逐字一致的连续片段时，只能转述并明确说明无法核验直引。\n"
            # 第 5 条针对推理模型：思维链既费 token 又挤占回答篇幅（服务端已不下发思考过程，
            # 这里再从源头要求模型把思考压到最短、直接产出讲解正文）。
            "5. 直接输出讲解正文：不要复述任务要求，不要输出「用户要求我…」之类的自我分析或思考过程；"
            "如需思考请尽量简短，把篇幅留给讲解本身。\n"
            "6. 严格使用以下 Markdown 结构和标题，不要省略标题：\n"
            "### 本页主旨\n用 2-3 句概括。\n\n"
            "### 论证脉络\n用 2-4 个有序条目说明推理关系。\n\n"
            "### 关键概念\n用项目符号解释概念在本页中的具体含义。\n\n"
            "### 原文依据\n使用 Markdown 引用块逐字短引，并说明每段引文支持上文哪一判断。\n"
            f"7. {style_instructions}\n\n"
        )

    def answer_pdf_chat(
        self,
        messages: list[dict[str, Any]],
        question: str,
        source_file: str,
        page: int,
        selected_text: str,
        web_enabled: bool,
        quick_mode: bool = False,
        page_context: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AIAnswer:
        use_zhipu = provider == "zhipu"
        self._ensure_enabled(provider)
        history_turns = (
            max(2, min(self.config.pdf_history_turns, 4))
            if quick_mode
            else self.config.pdf_history_turns
        )
        history = self._trim_messages(
            messages,
            max_turns=history_turns,
            char_limit=self.config.pdf_message_char_limit,
        )
        warnings: list[str] = []
        if not page_context:
            raise AIServiceError("缺少 PDF 页面上下文，无法进行讲解。")

        sources: list[dict[str, str]] = []
        local_context = self._format_pdf_context(
            source_file,
            page,
            selected_text,
            page_context,
            quick_mode=quick_mode,
        )
        prompt = (
            self._pdf_chat_instructions(quick_mode, use_zhipu)
            + f"本地 PDF 上下文：\n{local_context}\n\n"
            + f"用户问题：{question}"
        )
        answer = self.chat_complete(
            [
                {
                    "role": "system",
                    "content": "你是严谨的中文学术阅读助手。你必须适应该页实际书目与学科，忠于给定文本，区分原文事实与解释性推论。直接引文必须是给定上下文中连续、逐字一致的文字，不得用省略号拼接；不得补充材料未提供的后文、年代、背景、书名或页码，联网模式下由实际检索结果明确提供并在正文标注来源的内容除外。",
                },
                *history,
                {"role": "user", "content": prompt},
            ],
            max_tokens=(
                self.config.pdf_quick_answer_max_tokens
                if quick_mode
                else self.config.pdf_answer_max_tokens
            ),
            provider=provider,
            sources_out=sources,
            web_search_query=self.zhipu_search_query(question, page_context) if use_zhipu else None,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if use_zhipu and not sources:
            warnings.append("本次未获取到联网检索来源（检索服务暂不可用或已降级），讲解基于本页文本与模型自身知识。")
        return AIAnswer(
            answer_markdown=answer,
            sources=sources,
            used_web=bool(sources),
            warnings=warnings,
        )

    def prepare_pdf_chat(
        self,
        messages: list[dict[str, Any]],
        question: str,
        source_file: str,
        page: int,
        selected_text: str,
        web_enabled: bool,
        quick_mode: bool = False,
        page_context: dict[str, Any] | None = None,
        provider: str | None = None,
    ) -> tuple[list[dict[str, str]], int, list[dict[str, str]], list[str]]:
        use_zhipu = provider == "zhipu"
        self._ensure_enabled(provider)
        history_turns = (
            max(2, min(self.config.pdf_history_turns, 4))
            if quick_mode
            else self.config.pdf_history_turns
        )
        history = self._trim_messages(
            messages,
            max_turns=history_turns,
            char_limit=self.config.pdf_message_char_limit,
        )
        if not page_context:
            raise AIServiceError("缺少 PDF 页面上下文，无法进行讲解。")

        warnings: list[str] = []
        sources: list[dict[str, str]] = []
        local_context = self._format_pdf_context(
            source_file,
            page,
            selected_text,
            page_context,
            quick_mode=quick_mode,
        )
        prompt = (
            self._pdf_chat_instructions(quick_mode, use_zhipu)
            + f"本地 PDF 上下文：\n{local_context}\n\n"
            + f"用户问题：{question}"
        )
        max_tokens = (
            self.config.pdf_quick_answer_max_tokens
            if quick_mode
            else self.config.pdf_answer_max_tokens
        )
        return (
            [
                {
                    "role": "system",
                    "content": "你是严谨的中文学术阅读助手。你必须适应该页实际书目与学科，忠于给定文本，区分原文事实与解释性推论。直接引文必须是给定上下文中连续、逐字一致的文字，不得用省略号拼接；不得补充材料未提供的后文、年代、背景、书名或页码，联网模式下由实际检索结果明确提供并在正文标注来源的内容除外。",
                },
                *history,
                {"role": "user", "content": prompt},
            ],
            max_tokens,
            sources,
            warnings,
        )

    def _route(self, provider: str | None) -> dict[str, str]:
        """按调用方选择的通道返回 (base_url, api_key, model, label)。None/其他＝默认主通道。"""
        if provider == "zhipu":
            return {
                "base_url": self.config.zhipu_base_url,
                "api_key": self.config.zhipu_api_key,
                "model": self.config.zhipu_model,
                "label": "智谱AI",
                "provider": "zhipu",
                "auth_header": "Authorization",
            }
        if provider == "mimo":
            return {
                "base_url": self.config.mimo_base_url,
                "api_key": self.config.mimo_api_key,
                "model": self.config.mimo_model,
                "label": "MiMo",
                "provider": "mimo",
                "auth_header": "api-key",
            }
        return {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key,
            "model": self.config.model,
            "label": self.config.provider,
            "provider": "deepseek" if self.config.provider == "deepseek" else self.config.provider,
            "auth_header": "Authorization",
        }

    @staticmethod
    def _distill_query_terms(text: str) -> str:
        """剥掉口语问句的句首/句尾“求解外壳”，让检索词更接近关键词；剥过头则回退原句。"""
        s = " ".join(str(text or "").split())
        if not s:
            return ""
        stripped = _QUERY_TAIL_NOISE.sub("", _QUERY_HEAD_NOISE.sub("", s)).strip(" \t，,、。.？?！!；;：:~～")
        # 整句就是口语壳（剥成空/过短）时保留原句，避免把语义剥没。
        return stripped if len(stripped) >= 2 else s

    def zhipu_search_query(self, question: str, page_context: dict[str, Any] | None = None) -> str:
        """为强制联网生成简短、聚焦的检索词。

        两点调适，避免口语问句被医疗/养生等内容农场的长尾 SEO 霸屏：
        1. 剥掉口语“求解外壳”（句首“为什么/请问…”、句尾“…怎么回事/是什么原因/呢吗”），
           让检索词更接近关键词；阅读场景再带上篇章/书名。
        2. 领域锚定：问题或场景已含马列主题词即原样保留；否则补“马克思主义”，把搜索引擎
           拽回本站语料域（与出口过滤配合，跑题口语词基本无法再召回垃圾）。
        """
        raw = str(question or "").strip()
        core = self._distill_query_terms(raw)
        parts = [core or raw]
        anchored = any(anchor in raw for anchor in _MARX_CORE_ANCHORS)
        if page_context:
            ctx = (
                str(page_context.get("section_title") or "").strip()
                or str(page_context.get("display_title") or "").strip()
            )
            if ctx:
                parts.append(ctx)
                anchored = True  # 篇章/书名本身即强领域锚点
        if not anchored:
            parts.insert(0, "马克思主义")
        query = " ".join(part for part in parts if part)
        return " ".join(query.split())[:70]

    def _zhipu_web_search_tools(
        self,
        search_query: str | None = None,
        *,
        forced: bool = True,
    ) -> list[dict[str, Any]]:
        web_search: dict[str, Any] = {
            "enable": True,
            "search_engine": self.config.zhipu_search_engine,
            "search_result": True,
            "count": self.config.zhipu_search_count,
        }
        if forced:
            # 强制每次执行联网检索，不依赖模型自判断（实测自判断经常选择不搜）。
            # forced_search 为平台扩展参数、search_query 为经典“按指定词必搜”参数；
            # 两者都带上，任一生效即达成强制；都不被支持时由调用方降级到基础联网档。
            web_search["forced_search"] = True
            if search_query:
                web_search["search_query"] = search_query
        return [{"type": "web_search", "web_search": web_search}]

    @staticmethod
    def _zhipu_source_from_item(item: dict[str, Any]) -> dict[str, str]:
        """智谱检索结果单条 → 站内统一来源结构（对话内 web_search 与独立端点 search_result 同构）。"""
        link = str(item.get("link") or item.get("url") or "").strip()
        # 纵深防御：来源链接仅保留 http(s)，丢弃 javascript:/data: 等危险 scheme（前端 safeMarkdownUrl 亦兜底）。
        if link and not link.lower().startswith(("http://", "https://")):
            link = ""
        return {
            "title": str(item.get("title") or "").strip(),
            "link": link,
            "site": str(item.get("media") or "").strip(),
            "date": str(item.get("publish_date") or "").strip(),
            "snippet": str(item.get("content") or "").strip(),
        }

    def _zhipu_sources_from_payload(self, data: dict[str, Any]) -> list[dict[str, str]]:
        """从智谱响应提取联网来源并过滤内容农场/无关垃圾（search_result 在顶层 web_search 数组）。"""
        sources: list[dict[str, str]] = []
        for item in data.get("web_search") or []:
            if not isinstance(item, dict):
                continue
            source = self._zhipu_source_from_item(item)
            if source["title"] or source["link"]:
                sources.append(source)
        return self._filter_web_sources(sources)

    @staticmethod
    def _looks_like_web_junk(source: dict[str, str]) -> bool:
        """单条联网来源是否应判为内容农场/与本站主题无关的垃圾。"""
        site = source.get("site") or ""
        link = (source.get("link") or "").lower()
        if any(bad in site for bad in _WEB_SOURCE_DENY_SITES):
            return True
        if any(bad in link for bad in _WEB_SOURCE_DENY_DOMAINS):
            return True
        text = f"{source.get('title', '')} {source.get('snippet', '')}"
        if any(term in text for term in _WEB_SOURCE_JUNK_TERMS):
            # 有医疗/两性强信号、却通篇不含任何马列主题锚点 → 与本站主题无关。
            if not any(anchor in text for anchor in _MARX_SOFT_ANCHORS):
                return True
        return False

    def _filter_web_sources(
        self, sources: list[dict[str, str]], query: str = ""
    ) -> list[dict[str, str]]:
        """剔除内容农场/与主题明显无关的联网来源，返回过滤后的列表。"""
        kept = [s for s in (sources or []) if not self._looks_like_web_junk(s)]
        dropped = len(sources or []) - len(kept)
        if dropped:
            LOGGER.info(
                "web source filter: dropped %d/%d junk source(s); query=%r",
                dropped,
                len(sources or []),
                (query or "")[:40],
            )
        return kept

    def _zhipu_standalone_search(self, query: str) -> tuple[list[dict[str, str]], str]:
        """服务端直调智谱独立检索端点 /web_search。返回 (来源列表, 错误信息)。

        对话内 web_search 工具的检索费走平台「Coding套餐 > 资源包 > 余额」的抵扣优先级：
        账户挂着不含联网功能的 token 资源包时，检索会被平台静默跳过（HTTP 200、无来源、
        不报错）。独立端点计费独立、失败有明确错误码（如 1113 需充值），因此联网主链路
        改为先在这里检索、把结果注入对话，对话内检索只作兜底。
        """
        cleaned = " ".join(str(query or "").split())[:70]
        if not cleaned:
            return [], "检索词为空"
        try:
            data = self._post_json(
                "/web_search",
                {
                    "search_engine": self.config.zhipu_search_engine,
                    "search_query": cleaned,
                    "search_intent": False,
                    "count": max(1, min(20, self.config.zhipu_search_count)),
                },
                base_url=self.config.zhipu_base_url,
                api_key=self.config.zhipu_api_key,
                service_label="智谱AI",
            )
        except AIServiceError as exc:
            return [], str(exc)
        sources: list[dict[str, str]] = []
        for item in data.get("search_result") or []:
            if not isinstance(item, dict):
                continue
            source = self._zhipu_source_from_item(item)
            if source["title"] or source["link"]:
                sources.append(source)
        if not sources:
            return [], "检索调用成功但没有返回任何结果"
        return sources, ""

    def _zhipu_grounded_messages(
        self,
        messages: list[dict[str, str]],
        sources: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """把服务端检索结果注入对话（紧跟首条 system 指令），模型据此回答并标注来源。"""
        lines = [
            "以下是针对用户最新问题、刚刚由服务端联网检索到的网页结果。请优先依据这些结果回答；"
            "引用其中信息时标注来源序号（如 [2]）或网站名；结果未覆盖的部分再用自身知识补充并说明。"
        ]
        for index, source in enumerate(sources, 1):
            head = f"[{index}] {source['title'] or source['link']}"
            meta = "，".join(bit for bit in (source["site"], source["date"]) if bit)
            if meta:
                head += f"（{meta}）"
            block = [head]
            snippet = source["snippet"][:600]
            if snippet:
                block.append(snippet)
            if source["link"]:
                block.append(f"链接：{source['link']}")
            lines.append("\n".join(block))
        grounding = {"role": "system", "content": "\n\n".join(lines)}
        out = list(messages or [])
        insert_at = 1 if out and str(out[0].get("role") or "") == "system" else 0
        out.insert(insert_at, grounding)
        return out

    def _zhipu_grounding_or_stages(
        self,
        messages: list[dict[str, str]],
        web_search_query: str | None,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[list[dict[str, Any]] | None]]:
        """智谱联网主链路：先服务端直查，再过滤内容农场/无关结果。
        有可用结果→注入对话且不再带检索工具；端点失败→退回对话内检索三级降级；
        检索成功但结果全被判为垃圾→纯对话作答（不注入 grounding、不显示来源），
        不再对话内重搜（只会取回同样的垃圾）。返回 (messages, 来源, tool_stages)。"""
        if web_search_query:
            raw_sources, search_error = self._zhipu_standalone_search(web_search_query)
            if search_error:
                # 端点真失败（网络/计费/无结果）→ 退回对话内检索兜底档。
                LOGGER.warning(
                    "zhipu standalone search failed, falling back to in-chat web_search: %s",
                    search_error,
                )
                return messages, [], self._zhipu_tool_stages(web_search_query)
            sources = self._filter_web_sources(raw_sources, web_search_query)
            if sources:
                return self._zhipu_grounded_messages(messages, sources), sources, [None]
            LOGGER.info(
                "zhipu web search returned %d source(s), all dropped as junk; answering without web grounding",
                len(raw_sources),
            )
            return messages, [], [None]
        return messages, [], self._zhipu_tool_stages(web_search_query)

    def _zhipu_tool_stages(self, web_search_query: str | None) -> list[list[dict[str, Any]] | None]:
        """智谱联网的三级降级序列：强制联网 → 基础联网（模型自判断）→ 纯对话。

        强制档带 forced_search/search_query 扩展参数；若账号/模型不接受导致请求失败，
        退基础档仍保留联网能力；基础档也失败才退纯对话，保证任何情况下问答可用。
        """
        return [
            self._zhipu_web_search_tools(web_search_query, forced=True),
            self._zhipu_web_search_tools(forced=False),
            None,
        ]

    def zhipu_web_selftest(self) -> dict[str, Any]:
        """智谱联网通道一键自检（管理后台用）：三项硬指标定位“为什么没联网”。

        联网搜索是智谱平台侧执行的独立计费产品：账户无搜索资源时，对话内
        web_search 工具会被平台静默跳过（HTTP 200、不报错、forced_search 也救不了），
        只有独立 /web_search 端点会给出真实错误码（如 1113「您账户无可用资源包，请充值」）。
        prompt_tokens 是第二重指纹：约 11 为检索未注入，>1000 为已注入。
        """
        result: dict[str, Any] = {
            "enabled": self.config.zhipu_enabled,
            "model": self.config.zhipu_model,
            "base_url": self.config.zhipu_base_url,
            "search_engine": self.config.zhipu_search_engine,
            "endpoint_ok": False,
            "endpoint_error": "",
            "endpoint_results": 0,
            "chat_ok": False,
            "chat_error": "",
            "chat_sources": 0,
            "chat_prompt_tokens": None,
            "verdict": "",
        }
        if not self.config.zhipu_enabled:
            result["verdict"] = "未配置智谱 API Key，通道整体关闭。"
            return result
        try:
            data = self._post_json(
                "/web_search",
                {
                    "search_engine": self.config.zhipu_search_engine,
                    "search_query": "马克思 生平",
                    "search_intent": False,
                    "count": 1,
                },
                base_url=self.config.zhipu_base_url,
                api_key=self.config.zhipu_api_key,
                service_label="智谱AI",
            )
            result["endpoint_ok"] = True
            result["endpoint_results"] = len(data.get("search_result") or [])
        except AIServiceError as exc:
            result["endpoint_error"] = str(exc)
        try:
            data = self._post_json(
                "/chat/completions",
                {
                    "model": self.config.zhipu_model,
                    "messages": [{"role": "user", "content": "用一句话说明马克思的出生年份与出生地。"}],
                    "stream": False,
                    "max_tokens": 96,
                    "thinking": {"type": "disabled"},
                    "tools": self._zhipu_web_search_tools("马克思 出生年份 出生地", forced=True),
                },
                base_url=self.config.zhipu_base_url,
                api_key=self.config.zhipu_api_key,
                service_label="智谱AI",
            )
            result["chat_ok"] = True
            result["chat_sources"] = len(self._zhipu_sources_from_payload(data))
            usage = data.get("usage") or {}
            if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
                result["chat_prompt_tokens"] = int(usage["prompt_tokens"])
        except AIServiceError as exc:
            result["chat_error"] = str(exc)
        result["verdict"] = self._zhipu_selftest_verdict(result)
        return result

    @staticmethod
    def _zhipu_selftest_verdict(result: dict[str, Any]) -> str:
        if not result["endpoint_ok"]:
            error = str(result["endpoint_error"])
            if "1113" in error or "资源包" in error or "充值" in error:
                return (
                    "联网主链路不可用：智谱账户没有可用的联网搜索资源（注意平台抵扣顺序为"
                    "「Coding套餐 > 资源包 > 余额」，挂着不含联网的资源包也可能触发此错）。"
                    "去智谱开放平台核对余额与资源包后再测，代码与本页配置无需改动。"
                )
            if "401" in error or "令牌" in error or "apikey" in error.lower() or "鉴权" in error:
                return "智谱 API Key 无效或已过期：请核对上方 Key 是否为充值账户的 Key，重新粘贴后保存再测。"
            return f"联网主链路（服务端直查 /web_search）失败：{error}"
        if not result["chat_ok"]:
            return f"联网主链路正常（服务端直查可用），但对话调用失败：{result['chat_error']}"
        if result["chat_sources"] > 0:
            return (
                "联网检索一切正常：服务端直查与对话内检索（兜底档）都可用。"
                "若用户仍反馈无来源，请核对其所用功能与权限。"
            )
        prompt_tokens = result["chat_prompt_tokens"]
        if prompt_tokens is not None and int(prompt_tokens) < 200:
            return (
                "联网功能正常：主链路（服务端直查+注入对话）可用，用户不受影响。"
                f"仅对话内检索兜底档被平台静默跳过（prompt_tokens={prompt_tokens}，正常注入应 >1000），"
                "常见于「资源包优先抵扣」把检索费路由到不含联网的资源包；主链路已绕开该问题。"
            )
        return "服务端直查可用；对话内兜底档已注入但未提取到来源列表，可能是响应字段变化，不影响主链路。"

    def chat_complete(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None = None,
        provider: str | None = None,
        sources_out: list[dict[str, str]] | None = None,
        web_search_query: str | None = None,
        allow_reasoning_fallback: bool = False,
        model: str | None = None,
        http_timeout: float | None = None,
        disable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """一次非流式对话。

        ``allow_reasoning_fallback`` 已废弃（保留仅为兼容既有调用）：思维链在任何情况下都不再当作
        正文返回。历史上「正文为空就回退 reasoning_content」是为了「宁可给思考过程也不给空错误」，
        但推理模型把 max_tokens 全烧在思考上时（finish_reason=length、reasoning_tokens≈全额），
        正文恰恰是空的——于是读者页面上直接出现「we need answer in Chinese…」的自我分析。
        现改为：正文为空（或开头明显是思维链）时，用 thinking=disabled 原样重试一次，把整份
        token 预算让给正文；仍拿不到正文才如实报错。

        ``disable_thinking`` 默认 None＝按模型档位自动（快档 flash 关思考，见 _is_fast_tier）；
        传 True/False 可强制。
        """
        resolved_provider = provider
        if str(model or "").startswith("mimo-"):
            resolved_provider = "mimo"
        elif str(model or "").startswith("deepseek-"):
            resolved_provider = "deepseek"
        self._ensure_enabled(resolved_provider)
        use_zhipu = resolved_provider == "zhipu"
        route = self._route(resolved_provider)
        # 允许按调用方指定模型覆盖该通道默认模型（如马克思形象固定走更轻量的 deepseek-v4-flash）。
        model_name = (model or "").strip() or route["model"]
        normalized_effort = str(reasoning_effort or "").strip().lower()
        if normalized_effort == "medium":
            normalized_effort = "high"
        if disable_thinking is None:
            disable_thinking = normalized_effort in {"off", "disabled", "none"} or (
                not normalized_effort and self._is_fast_tier(model_name)
            )
        grounded_sources: list[dict[str, str]] = []
        if use_zhipu:
            messages, grounded_sources, tool_stages = self._zhipu_grounding_or_stages(messages, web_search_query)
        else:
            tool_stages = [None]

        def _request(disable_thinking: bool) -> dict[str, Any]:
            data: dict[str, Any] = {}
            for stage_index, tools in enumerate(tool_stages):
                payload: dict[str, Any] = {
                    "model": model_name,
                    "messages": messages,
                    "stream": False,
                    "temperature": self.config.temperature if temperature is None else temperature,
                }
                if route["provider"] == "mimo":
                    payload["max_completion_tokens"] = max_tokens
                else:
                    payload["max_tokens"] = max_tokens
                if tools:
                    payload["tools"] = tools
                if use_zhipu or disable_thinking:
                    # 关闭深度思考保证响应速度与输出干净（不混入 reasoning）。
                    payload["thinking"] = dict(_THINKING_DISABLED)
                elif route["provider"] in {"mimo", "deepseek"}:
                    payload["thinking"] = {"type": "enabled"}
                    if route["provider"] == "deepseek" and normalized_effort in {"low", "high", "max"}:
                        payload["reasoning_effort"] = normalized_effort
                try:
                    return self._post_json(
                        "/chat/completions",
                        payload,
                        base_url=route["base_url"],
                        api_key=route["api_key"],
                        service_label=route["label"],
                        provider_name=route["provider"],
                        auth_header=route["auth_header"],
                        reasoning_effort=normalized_effort or ("off" if disable_thinking else "on"),
                        http_timeout=http_timeout,
                    )
                except AIServiceError as exc:
                    if stage_index == len(tool_stages) - 1:
                        raise
                    # 降级必须可观测：否则“联网悄悄失效”无从排查（journalctl 可查到这行）。
                    LOGGER.warning("zhipu chat stage %d failed, degrading: %s", stage_index, exc)
            return data

        def _answer_text(data: dict[str, Any]) -> str:
            """只取正文：content → choices[].text。**绝不取 reasoning_content**。"""
            choices = data.get("choices") or []
            if not choices:
                raise AIServiceError("模型未返回任何内容。")
            message = choices[0].get("message") or {}
            text = self._coerce_message_content(message.get("content")).strip()
            if not text:
                text = self._coerce_message_content(choices[0].get("text")).strip()
            return text

        data = _request(disable_thinking)
        text = _answer_text(data)
        if not text or (not disable_thinking and self._looks_like_reasoning_leak(text)):
            # 思维链吃光了输出预算（或被模型写进了正文）→ 关掉思考重来一次，把预算全留给正文。
            # 重试只发生在这种异常形态上，正常回答零额外开销。（已关思考仍判空才重试一次，
            # 此时重试参数与首次相同，等价于一次纯重试；疑似泄漏的判定则跳过——关思考时不可能是思维链。）
            LOGGER.warning(
                "chat_complete got no usable answer (model=%s, empty=%s), retrying with thinking disabled",
                model_name,
                not text,
            )
            try:
                retry_data = _request(True)
                retry_text = _answer_text(retry_data)
            except AIServiceError as exc:
                LOGGER.warning("chat_complete no-thinking retry failed: %s", exc)
                retry_text = ""
                retry_data = {}
            if retry_text:
                data, text = retry_data, retry_text
        if use_zhipu and sources_out is not None:
            sources_out.extend(grounded_sources or self._zhipu_sources_from_payload(data))
        if not text:
            raise AIServiceError("模型本次只产生了思考过程、未给出答案，请重试。")
        return text

    def chat_complete_stream(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        provider: str | None = None,
        meta_out: dict[str, Any] | None = None,
        web_search_query: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[str]:
        use_zhipu = provider == "zhipu"
        if use_zhipu:
            messages, grounded_sources, tool_stages = self._zhipu_grounding_or_stages(messages, web_search_query)
            if grounded_sources and meta_out is not None:
                meta_out["sources"] = grounded_sources
        else:
            tool_stages = [None]
        yielded = [False]
        for stage_index, tools in enumerate(tool_stages):
            try:
                yield from self._stream_chat_once(
                    messages, max_tokens, provider=provider, tools=tools, meta_out=meta_out, yielded=yielded,
                    model=model, reasoning_effort=reasoning_effort,
                )
                return
            except _ReasoningOnlyResponse:
                # 全程只有思维链、没有正文（思考烧光了 max_tokens）。此时一个字都还没下发，
                # 重来一次是安全的：关掉思考，把整份预算让给正文——绝不把自我分析当答案吐给读者。
                LOGGER.warning("stream produced reasoning only, retrying with thinking disabled")
                try:
                    yield from self._stream_chat_once(
                        messages, max_tokens, provider=provider, tools=tools,
                        meta_out=meta_out, yielded=yielded, disable_thinking=True,
                        model=model, reasoning_effort="off",
                    )
                except _ReasoningOnlyResponse:
                    raise AIServiceError("模型本次只产生了思考过程、未给出答案，请重试。") from None
                return
            except AIServiceError as exc:
                # 首包即失败（强制参数/联网工具不被支持等）且未输出任何内容时，逐级降级重试；
                # 已经吐过增量就不能换档重来（会输出重复内容），原样抛出由路由层兜底。
                if yielded[0] or stage_index == len(tool_stages) - 1:
                    raise
                LOGGER.warning("zhipu stream stage %d failed, degrading: %s", stage_index, exc)

    def _stream_chat_once(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        provider: str | None,
        tools: list[dict[str, Any]] | None,
        meta_out: dict[str, Any] | None,
        yielded: list[bool],
        disable_thinking: bool = False,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[str]:
        resolved_provider = provider
        if str(model or "").startswith("mimo-"):
            resolved_provider = "mimo"
        elif str(model or "").startswith("deepseek-"):
            resolved_provider = "deepseek"
        self._ensure_enabled(resolved_provider)
        use_zhipu = resolved_provider == "zhipu"
        route = self._route(resolved_provider)
        model_name = str(model or "").strip() or route["model"]
        normalized_effort = str(reasoning_effort or "").strip().lower()
        if normalized_effort == "medium":
            normalized_effort = "high"
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": True,
            "temperature": self.config.temperature,
            "stream_options": {"include_usage": True},
        }
        if route["provider"] == "mimo":
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        # 快档（flash）默认关思考：它的思考对质量无增益、却常把 max_tokens 吃光（见 _is_fast_tier
        # 的实测），流式场景还会让读者干等首字。深思仍留给 pro 档。
        if use_zhipu or disable_thinking or normalized_effort == "off" or (
            not normalized_effort and self._is_fast_tier(model_name)
        ):
            payload["thinking"] = dict(_THINKING_DISABLED)
        elif route["provider"] in {"mimo", "deepseek"}:
            payload["thinking"] = {"type": "enabled"}
            if route["provider"] == "deepseek" and normalized_effort in {"low", "high", "max"}:
                payload["reasoning_effort"] = normalized_effort
        url = f"{route['base_url']}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=body,
            headers={
                route["auth_header"]: (
                    route["api_key"] if route["auth_header"].lower() == "api-key" else f"Bearer {route['api_key']}"
                ),
                "Content-Type": "application/json",
                "Accept-Language": "zh-CN,zh",
            },
            method="POST",
        )
        request_id = secrets.token_urlsafe(18)
        # Keep billing/telemetry timing independent from the request retry clock.
        # Several retry-path tests (and some callers) deliberately stub
        # ``time.monotonic``; consuming an extra value here would alter behavior.
        started_at = _call_perf_counter()
        effective_effort = normalized_effort or ("off" if payload.get("thinking") == _THINKING_DISABLED else "on")
        preflight = _emit_ai_call_event({
            "phase": "before", "request_id": request_id, "provider": route["provider"],
            "model": model_name, "reasoning_effort": effective_effort,
            "estimated_prompt_tokens": _estimate_message_tokens(messages),
            "max_completion_tokens": max_tokens,
        })
        observed_usage = {"prompt_tokens": 0, "cached_prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
        http_success = False
        # 思维链只保活、不下发：推理模型（如线上主通道 deepseek-v4-pro）会先流出大段
        # reasoning_content 再给正文。旧实现把它当正文吐给前端 → 阅读器满屏「用户要求我…」的
        # 自我分析，还被存进会话历史、按 completion 计入用户 token 额度，下一轮又作为历史重发。
        # 现改为：思考阶段每隔几秒 yield 一个空串（调用方译成 SSE 注释，喂住 Cloudflare 的空闲
        # 计时器），正文增量照常下发；全程没有正文时抛 _ReasoningOnlyResponse，由 chat_complete_stream
        # 关掉思考重来一遍（此时一个字都未下发，重来安全）——思维链任何情况下都不当答案下发。
        reasoning_seen = False
        last_tick = time.monotonic()
        try:
            with _ai_http_slot(), urllib_request.urlopen(
                req, timeout=self.config.request_timeout_seconds
            ) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("usage"):
                        observed_usage = _usage_from_payload(payload)
                    if use_zhipu and meta_out is not None and payload.get("web_search"):
                        meta_out["sources"] = self._zhipu_sources_from_payload(payload)
                    for choice in payload.get("choices") or []:
                        delta = choice.get("delta") or choice.get("message") or {}
                        text = (
                            self._coerce_message_content(delta.get("content"))
                            or self._coerce_message_content(choice.get("text"))
                        )
                        if text:
                            yielded[0] = True
                            yield text
                            continue
                        reasoning = self._coerce_message_content(delta.get("reasoning_content"))
                        if reasoning:
                            reasoning_seen = True
                            now = time.monotonic()
                            if now - last_tick >= 8.0:
                                last_tick = now
                                yield ""
            http_success = True
            if not yielded[0] and reasoning_seen:
                raise _ReasoningOnlyResponse()
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(detail)
            except json.JSONDecodeError:
                payload = {"message": detail}
            message = (
                payload.get("message")
                or payload.get("error", {}).get("message")
                or f"HTTP {exc.code}"
            )
            raise AIServiceError(f"{route['label']} 请求失败：{message}") from exc
        except urllib_error.URLError as exc:
            raise AIServiceError(f"无法连接 {route['label']} 服务：{exc.reason}") from exc
        except TimeoutError as exc:
            raise AIServiceError(f"{route['label']} 请求超时，请稍后重试。") from exc
        finally:
            try:
                _emit_ai_call_event({
                    "phase": "after", "request_id": request_id, "provider": route["provider"],
                    "model": model_name, "reasoning_effort": effective_effort,
                    **observed_usage, "success": http_success,
                    "latency_ms": int((_call_perf_counter() - started_at) * 1000),
                    "preflight": preflight,
                })
            except Exception:  # billing telemetry must not corrupt an already-produced stream
                LOGGER.exception("AI provider-call finalization failed")

    def _ensure_enabled(self, provider: str | None = None) -> None:
        if provider == "zhipu":
            if not self.config.zhipu_enabled:
                raise AIServiceError("智谱 AI 通道尚未配置，请先在控制台「智能服务」中填写智谱 API Key。")
            return
        if provider == "mimo":
            if not self.config.mimo_enabled:
                raise AIServiceError("MiMo 通道尚未配置 MIMO_API_KEY。")
            return
        if not self.config.enabled:
            raise AIServiceError("AI 对话功能尚未配置，请先在 config/ai.yaml、APPDATA 覆盖配置或环境变量中设置 Z.AI API Key。")

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        service_label: str | None = None,
        http_timeout: float | None = None,
        provider_name: str | None = None,
        auth_header: str = "Authorization",
        reasoning_effort: str = "off",
    ) -> dict[str, Any]:
        label = service_label or self.config.provider
        # 调用方(如研究综述)可按「剩余总预算」压低单次超时；默认沿用全局 request_timeout_seconds。
        timeout = http_timeout if (http_timeout and http_timeout > 0) else self.config.request_timeout_seconds
        url = f"{base_url or self.config.base_url}{path}"
        request_payload = payload
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        key_value = api_key or self.config.api_key
        req = urllib_request.Request(
            url,
            data=body,
            headers={
                auth_header: key_value if auth_header.lower() == "api-key" else f"Bearer {key_value}",
                "Content-Type": "application/json",
                "Accept-Language": "zh-CN,zh",
            },
            method="POST",
        )
        resolved_provider = str(provider_name or ("zhipu" if "智谱" in label else self.config.provider)).lower()
        resolved_model = str(request_payload.get("model") or "")
        request_id = secrets.token_urlsafe(18)
        started_at = _call_perf_counter()
        preflight = _emit_ai_call_event({
            "phase": "before", "request_id": request_id, "provider": resolved_provider,
            "model": resolved_model, "reasoning_effort": reasoning_effort,
            "estimated_prompt_tokens": _estimate_message_tokens(request_payload.get("messages")),
            "max_completion_tokens": int(request_payload.get("max_completion_tokens") or request_payload.get("max_tokens") or 0),
        })
        response_payload: dict[str, Any] = {}
        call_error = ""
        success = False
        try:
            with _ai_http_slot(), urllib_request.urlopen(req, timeout=timeout) as resp:
                response_payload = json.loads(resp.read().decode("utf-8"))
                success = True
                return response_payload
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                response_payload = json.loads(detail)
            except json.JSONDecodeError:
                response_payload = {"message": detail}
            message = (
                response_payload.get("message")
                or response_payload.get("error", {}).get("message")
                or f"HTTP {exc.code}"
            )
            call_error = str(message)
            raise AIServiceError(f"{label} 请求失败：{message}") from exc
        except urllib_error.URLError as exc:
            call_error = str(exc.reason)
            raise AIServiceError(f"无法连接 {label} 服务：{exc.reason}") from exc
        except TimeoutError as exc:
            call_error = "timeout"
            raise AIServiceError(f"{label} 请求超时，请稍后重试。") from exc
        finally:
            try:
                _emit_ai_call_event({
                    "phase": "after", "request_id": request_id, "provider": resolved_provider,
                    "model": resolved_model, "reasoning_effort": reasoning_effort,
                    **_usage_from_payload(response_payload), "success": success, "error": call_error,
                    "latency_ms": int((_call_perf_counter() - started_at) * 1000), "preflight": preflight,
                })
            except Exception:
                LOGGER.exception("AI provider-call finalization failed")

    def _trim_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        max_turns: int,
        char_limit: int,
    ) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = self._coerce_message_content(item.get("content")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            cleaned.append({"role": role, "content": self._limit_text(content, char_limit)})
        max_messages = max(2, max_turns * 2)
        return cleaned[-max_messages:]

    def _coerce_message_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = (
                        item.get("text")
                        or item.get("content")
                        or item.get("output_text")
                        or item.get("value")
                        or ""
                    )
                    if text:
                        parts.append(str(text))
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            return str(
                content.get("text")
                or content.get("content")
                or content.get("output_text")
                or content.get("value")
                or ""
            )
        return ""

    def _format_sources_for_prompt(self, sources: list[dict[str, str]]) -> str:
        if not sources:
            return "（无）"
        chunks: list[str] = []
        for idx, source in enumerate(sources, start=1):
            line = [
                f"[{idx}] 标题：{source.get('title') or '未命名来源'}",
                f"站点：{source.get('site') or '未知'}",
                f"链接：{source.get('link') or '无'}",
            ]
            if source.get("date"):
                line.append(f"日期：{source['date']}")
            if source.get("snippet"):
                line.append(f"摘要：{source['snippet']}")
            chunks.append("\n".join(line))
        return "\n\n".join(chunks)

    def _build_pdf_search_query(
        self,
        question: str,
        selected_text: str,
        page_context: dict[str, Any],
    ) -> str:
        parts = [question.strip()]
        if selected_text.strip():
            parts.append(self._limit_text(selected_text.strip(), 240))
        section_title = str(page_context.get("section_title") or "").strip()
        if section_title:
            parts.append(section_title)
        display_title = str(page_context.get("display_title") or "").strip()
        if display_title:
            parts.append(display_title)
        return " ".join(part for part in parts if part)

    def _format_pdf_context(
        self,
        source_file: str,
        page: int,
        selected_text: str,
        page_context: dict[str, Any],
        quick_mode: bool = False,
    ) -> str:
        selected_limit = (
            self.config.pdf_quick_selected_text_char_limit
            if quick_mode
            else self.config.pdf_selected_text_char_limit
        )
        current_limit = (
            self.config.pdf_quick_current_text_char_limit
            if quick_mode
            else self.config.pdf_current_text_char_limit
        )
        adjacent_limit = (
            self.config.pdf_quick_adjacent_excerpt_char_limit
            if quick_mode
            else self.config.pdf_adjacent_excerpt_char_limit
        )
        fields = [
            f"文件：{source_file}",
            f"PDF 页码：{page}",
            f"书名：{page_context.get('display_title') or ''}",
            f"卷册：{page_context.get('book') or ''} 第 {page_context.get('volume') or ''} 卷",
            f"篇章：{page_context.get('section_title') or '未识别'}",
            f"当前页标号：{page_context.get('page_label') or ''}",
            f"引文信息：{page_context.get('citation') or ''}",
        ]
        if selected_text.strip():
            fields.append(f"用户选中的内容：{self._limit_text(selected_text.strip(), selected_limit)}")
        fields.extend(
            [
                f"当前页全文：{self._limit_text(page_context.get('current_text') or '', current_limit)}",
                f"前页摘录：{self._limit_text(page_context.get('previous_excerpt') or '', adjacent_limit)}",
                f"后页摘录：{self._limit_text(page_context.get('next_excerpt') or '', adjacent_limit)}",
            ]
        )
        return "\n".join(fields)

    def _limit_text(self, text: str, limit: int) -> str:
        compact = " ".join((text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "..."
