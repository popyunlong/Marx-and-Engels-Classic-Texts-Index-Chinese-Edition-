from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml

from runtime_env import APPDATA_DIR


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "ai.yaml"
AI_OVERRIDE_PATH = APPDATA_DIR / "ai.override.yaml"

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 智谱开放平台（bigmodel.cn）：OpenAI 兼容 /chat/completions，作为可选的「联网检索」通道，
# 与默认 DeepSeek 通道平行存在、互不影响；前台仅持有 ai_web 权限的用户可选。
ZHIPU_DEFAULT_MODEL = "glm-5.1"
ZHIPU_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_SEARCH_ENGINES = ("search_std", "search_pro", "search_pro_sogou", "search_pro_quark")

SUPPORTED_PROVIDERS = {"zai", "deepseek"}
ZAI_PROVIDER_ALIASES = {"zai", "z.ai", "智谱", "zhipu", "glm"}
DEEPSEEK_PROVIDER_ALIASES = {"deepseek", "深度求索"}

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


class AIServiceError(RuntimeError):
    pass


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
    base_url = (
        str(
            os.environ.get("ZAI_BASE_URL")
            or os.environ.get("APP_AI_BASE_URL")
            or payload.get("base_url")
            or (DEEPSEEK_BASE_URL if provider == "deepseek" else DEFAULT_BASE_URL)
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
        1800,
    )
    pdf_answer_max_tokens = _pick_int(
        "APP_AI_PDF_ANSWER_MAX_TOKENS",
        payload,
        "pdf_answer_max_tokens",
        1800,
    )
    pdf_quick_answer_max_tokens = _pick_int(
        "APP_AI_PDF_QUICK_ANSWER_MAX_TOKENS",
        payload,
        "pdf_quick_answer_max_tokens",
        900,
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
    if not api_key:
        problems.append("未配置 Z.AI API Key，AI 对话功能已禁用。")
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
    search_answer_max_tokens = max(300, search_answer_max_tokens)
    pdf_answer_max_tokens = max(300, pdf_answer_max_tokens)
    pdf_quick_answer_max_tokens = max(300, pdf_quick_answer_max_tokens)
    pdf_selected_text_char_limit = max(200, pdf_selected_text_char_limit)
    pdf_current_text_char_limit = max(600, pdf_current_text_char_limit)
    pdf_adjacent_excerpt_char_limit = max(60, pdf_adjacent_excerpt_char_limit)
    pdf_quick_selected_text_char_limit = max(120, pdf_quick_selected_text_char_limit)
    pdf_quick_current_text_char_limit = max(300, pdf_quick_current_text_char_limit)
    pdf_quick_adjacent_excerpt_char_limit = max(60, pdf_quick_adjacent_excerpt_char_limit)
    temperature = min(1.5, max(0.0, temperature))

    enabled = provider in SUPPORTED_PROVIDERS and bool(api_key)
    zhipu_enabled = bool(zhipu_api_key)

    return AIConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
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


class ZAIClient:
    def __init__(self, config: AIConfig) -> None:
        self.config = config

    def answer_search_chat(
        self,
        messages: list[dict[str, Any]],
        question: str,
        provider: str | None = None,
    ) -> AIAnswer:
        use_zhipu = provider == "zhipu"
        self._ensure_enabled(provider)
        history = self._trim_messages(
            messages,
            max_turns=self.config.search_history_turns,
            char_limit=self.config.search_message_char_limit,
        )
        warnings: list[str] = []
        sources: list[dict[str, str]] = []

        if use_zhipu:
            prompt = (
                "请回答用户的问题。\n"
                "要求：\n"
                "1. 使用中文回答，尽量准确、完整、结构清晰。\n"
                "2. 已为你启用联网检索：涉及实时信息或外部资料时，优先依据检索结果作答，"
                "并在正文中注明所依据来源的标题；检索结果不足时如实说明，绝不编造来源或链接。\n\n"
                f"用户问题：{question}"
            )
        else:
            prompt = (
                "请回答用户的问题。\n"
                "要求：\n"
                "1. 使用中文回答，尽量准确、完整、结构清晰。\n"
                "2. 不要声称已经联网检索，也不要编造具体来源链接。\n"
                "3. 如果需要实时资料或外部来源核验，要明确提示用户当前未启用联网检索。\n\n"
                f"用户问题：{question}"
            )
        answer = self.chat_complete(
            [
                {
                    "role": "system",
                    "content": "你是一位严谨、清楚、重视来源标注的中文研究助手。",
                },
                *history,
                {"role": "user", "content": prompt},
            ],
            max_tokens=self.config.search_answer_max_tokens,
            provider=provider,
            sources_out=sources,
            web_search_query=self.zhipu_search_query(question) if use_zhipu else None,
        )
        return AIAnswer(
            answer_markdown=answer,
            sources=sources,
            used_web=bool(sources),
            warnings=warnings,
        )

    def expand_associative_query(self, gist: str) -> dict:
        """联想检索第一步：把用户的“大意/关键词”扩展为可在语料中检索的线索。

        返回 ``{"quotes": [...1-3...], "keywords": [...4-10...]}``。这些只作为检索输入，
        其内容绝不直接作为结果展示——最终引文一律由真实命中生成。
        """
        self._ensure_enabled()
        gist = " ".join(str(gist or "").split())[:600]
        if not gist:
            return {}
        cached = _ASSOC_EXPAND_CACHE.get(gist)
        if cached is not None:
            _ASSOC_EXPAND_CACHE.move_to_end(gist)
            return dict(cached)
        prompt = (
            "用户想在马克思、恩格斯、列宁的著作中找到与下面这段输入最匹配的原文。输入可能是"
            "“大意描述”，也可能是“记得的只言片语/残句”。请先在心里推理它最可能出自哪段论述、"
            "属于哪一主题与篇章，再输出便于在中文原著中逐字定位的检索线索。\n"
            "只输出一个 JSON 对象，不要解释、不要 Markdown 代码块，格式：\n"
            '{"quotes": ["最可能的原文整句"], "fragments": ["逐字短语1", "逐字短语2"],'
            ' "keywords": ["正文实词1", "正文实词2"], "chapter_keywords": ["篇章/标题词1", "篇章/标题词2"]}\n'
            "要求：\n"
            "1. quotes 给 1-3 句，尽量逐字还原人民出版社中文译本的书面语措辞（19 世纪译文风格、"
            "政治经济学/哲学术语），而不是口语转述；记不准就给最可能的措辞。\n"
            "2. fragments 最重要：给 5-12 个你认为会**一字不差**出现在该译本正文中的特征短语（4-12 字），"
            "如“社会关系的总和”“全世界无产者，联合起来”“资本主义的最高阶段”。这是定位成败的关键，"
            "宁可多给几个不同位置、不同表述的短语。\n"
            "3. keywords 给 6-12 个正文里区分度高的实词：既要从大意**推理**出原著可能用到的术语，"
            "也要从用户给的只言片语里**直接截取**关键实词；涵盖近义/不同译法（如“异化/外化”），"
            "避免“的/是/社会/发展”这类高频泛词。\n"
            "4. chapter_keywords 给 3-8 个可能出现在**篇章或标题**中的词（著作名、章节主题、概念名），"
            "如“费尔巴哈”“资本的生产过程”“帝国主义”“家庭、私有制和国家”，用于定位所属篇章。\n"
            f"\n用户输入：{gist}"
        )
        # DeepSeek 即便 temperature=0 也偶尔返回空/截断的 JSON，导致“同一输入有时搜不到”。
        # 故重试至多 3 次，命中可用线索即止；成功结果入缓存，使同一输入后续稳定可复现。
        messages = [
            {"role": "system", "content": "你是精通马克思、恩格斯、列宁文献、熟知人民出版社中译本措辞与篇目结构的中文检索专家。"},
            {"role": "user", "content": prompt},
        ]
        plan: dict = {}
        for _attempt in range(3):
            answer = self.chat_complete(messages, max_tokens=900, temperature=0.0)
            parsed = _extract_json_object(self._coerce_message_content(answer))
            if isinstance(parsed, dict) and any(
                parsed.get(k) for k in ("quotes", "fragments", "keywords", "chapter_keywords")
            ):
                plan = parsed
                break
        if plan:
            _ASSOC_EXPAND_CACHE[gist] = plan
            _ASSOC_EXPAND_CACHE.move_to_end(gist)
            while len(_ASSOC_EXPAND_CACHE) > _ASSOC_EXPAND_CACHE_MAX:
                _ASSOC_EXPAND_CACHE.popitem(last=False)
        return plan

    def rank_associative_candidates(self, gist: str, candidates: list[dict]) -> list[dict]:
        """联想检索第二步：在已定位的真实候选段落中，按与大意的匹配度排序并给出理由。

        ``candidates`` 为已编号的真实命中（含真实引文/上下文）。模型只能从给定候选中选择，
        返回 ``[{"index": N, "confidence": 0-100, "reason": "..."}]``，不得编造或新增条目。
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
            context = " ".join(context.split())[:160]
            lines.append(f"[{i}] {citation} | 上下文：{context}")
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
            max_tokens=1100,  # 结构化 JSON 全有或全无：给足余量，避免 12 条带理由的输出被截断
            temperature=0.0,  # 重排也走确定性，保证同一输入结果稳定
        )
        parsed = _extract_json_object(self._coerce_message_content(answer))
        if isinstance(parsed, dict):
            # 容忍模型把数组包在 {"results": [...]} / {"ranking": [...]} 里
            for key in ("results", "ranking", "items", "data"):
                if isinstance(parsed.get(key), list):
                    parsed = parsed[key]
                    break
            else:
                parsed = []
        return parsed if isinstance(parsed, list) else []

    def _pdf_chat_instructions(self, quick_mode: bool, use_zhipu: bool) -> str:
        style_instructions = (
            "请直接给出 4-8 句的快速讲解，优先说明本页重点、关键概念与用户当前疑问。"
            if quick_mode
            else "请按要点分段讲解，先解释原文，再补充必要背景、概念关系和阅读提示。"
        )
        web_instruction = (
            "3. 已为你启用联网检索：可结合检索结果补充背景或最新研究，引用网络资料时在正文注明来源标题；"
            "检索结果与原文无关时以本地上下文为准，绝不编造来源或链接。\n"
            if use_zhipu
            else "3. 可以补充必要背景，但不要声称已经联网检索，也不要编造具体来源链接。\n"
        )
        return (
            "请围绕用户当前阅读的 PDF 页面进行讲解。\n"
            "要求：\n"
            "1. 优先解释用户选中的内容；未选中时解释当前页主旨。\n"
            "2. 回答必须先基于本地 PDF 上下文，不要脱离页面内容空谈。\n"
            f"{web_instruction}"
            "4. 使用中文回答，避免编造，必要时指出依据来自本页或相邻页。\n"
            f"6. {style_instructions}\n\n"
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
                    "content": "你是一位擅长马克思恩格斯文献解读的中文助手，回答要忠于文本、说明依据、适度补充背景。",
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
        )
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
                    "content": "你是一位擅长马克思恩格斯文献解读的中文助手，回答要忠于文本、说明依据、适度补充背景。",
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
            }
        return {
            "base_url": self.config.base_url,
            "api_key": self.config.api_key,
            "model": self.config.model,
            "label": self.config.provider,
        }

    def zhipu_search_query(self, question: str, page_context: dict[str, Any] | None = None) -> str:
        """为强制联网生成简短检索词：用户问题 +（阅读场景）篇章/书名，截到 70 字以内。"""
        parts = [str(question or "").strip()]
        if page_context:
            section_title = str(page_context.get("section_title") or "").strip()
            display_title = str(page_context.get("display_title") or "").strip()
            parts.append(section_title or display_title)
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

    def _zhipu_sources_from_payload(self, data: dict[str, Any]) -> list[dict[str, str]]:
        """从智谱响应中提取联网检索来源（开启 search_result 时返回在顶层 web_search 数组）。"""
        results = data.get("web_search") or []
        sources: list[dict[str, str]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            source = {
                "title": str(item.get("title") or "").strip(),
                "link": str(item.get("link") or item.get("url") or "").strip(),
                "site": str(item.get("media") or "").strip(),
                "date": str(item.get("publish_date") or "").strip(),
                "snippet": str(item.get("content") or "").strip(),
            }
            if source["title"] or source["link"]:
                sources.append(source)
        return sources

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

    def chat_complete(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None = None,
        provider: str | None = None,
        sources_out: list[dict[str, str]] | None = None,
        web_search_query: str | None = None,
    ) -> str:
        use_zhipu = provider == "zhipu"
        route = self._route(provider)
        tool_stages = self._zhipu_tool_stages(web_search_query) if use_zhipu else [None]
        data: dict[str, Any] = {}
        for stage_index, tools in enumerate(tool_stages):
            payload: dict[str, Any] = {
                "model": route["model"],
                "messages": messages,
                "stream": False,
                "temperature": self.config.temperature if temperature is None else temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                payload["tools"] = tools
            if use_zhipu:
                # 关闭深度思考保证响应速度与输出干净（不混入 reasoning）。
                payload["thinking"] = {"type": "disabled"}
            try:
                data = self._post_json(
                    "/chat/completions",
                    payload,
                    base_url=route["base_url"],
                    api_key=route["api_key"],
                    service_label=route["label"],
                )
                break
            except AIServiceError:
                if stage_index == len(tool_stages) - 1:
                    raise
        if use_zhipu and sources_out is not None:
            sources_out.extend(self._zhipu_sources_from_payload(data))
        choices = data.get("choices") or []
        if not choices:
            raise AIServiceError("模型未返回任何内容。")
        message = choices[0].get("message") or {}
        content = message.get("content")
        text = self._coerce_message_content(content).strip()
        if not text:
            text = self._coerce_message_content(message.get("reasoning_content")).strip()
        if not text:
            text = self._coerce_message_content(choices[0].get("text")).strip()
        if not text:
            raise AIServiceError("模型返回了空内容。")
        return text

    def chat_complete_stream(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        provider: str | None = None,
        meta_out: dict[str, Any] | None = None,
        web_search_query: str | None = None,
    ) -> Iterator[str]:
        use_zhipu = provider == "zhipu"
        tool_stages = self._zhipu_tool_stages(web_search_query) if use_zhipu else [None]
        yielded = [False]
        for stage_index, tools in enumerate(tool_stages):
            try:
                yield from self._stream_chat_once(
                    messages, max_tokens, provider=provider, tools=tools, meta_out=meta_out, yielded=yielded
                )
                return
            except AIServiceError:
                # 首包即失败（强制参数/联网工具不被支持等）且未输出任何内容时，逐级降级重试；
                # 已经吐过增量就不能换档重来（会输出重复内容），原样抛出由路由层兜底。
                if yielded[0] or stage_index == len(tool_stages) - 1:
                    raise

    def _stream_chat_once(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        *,
        provider: str | None,
        tools: list[dict[str, Any]] | None,
        meta_out: dict[str, Any] | None,
        yielded: list[bool],
    ) -> Iterator[str]:
        use_zhipu = provider == "zhipu"
        route = self._route(provider)
        payload: dict[str, Any] = {
            "model": route["model"],
            "messages": messages,
            "stream": True,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if use_zhipu:
            payload["thinking"] = {"type": "disabled"}
        url = f"{route['base_url']}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {route['api_key']}",
                "Content-Type": "application/json",
                "Accept-Language": "zh-CN,zh",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
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
                    if use_zhipu and meta_out is not None and payload.get("web_search"):
                        meta_out["sources"] = self._zhipu_sources_from_payload(payload)
                    for choice in payload.get("choices") or []:
                        delta = choice.get("delta") or choice.get("message") or {}
                        text = (
                            self._coerce_message_content(delta.get("content"))
                            or self._coerce_message_content(delta.get("reasoning_content"))
                            or self._coerce_message_content(choice.get("text"))
                        )
                        if text:
                            yielded[0] = True
                            yield text
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

    def _ensure_enabled(self, provider: str | None = None) -> None:
        if provider == "zhipu":
            if not self.config.zhipu_enabled:
                raise AIServiceError("智谱 AI 通道尚未配置，请先在控制台「智能服务」中填写智谱 API Key。")
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
    ) -> dict[str, Any]:
        label = service_label or self.config.provider
        url = f"{base_url or self.config.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key or self.config.api_key}",
                "Content-Type": "application/json",
                "Accept-Language": "zh-CN,zh",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
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
            raise AIServiceError(f"{label} 请求失败：{message}") from exc
        except urllib_error.URLError as exc:
            raise AIServiceError(f"无法连接 {label} 服务：{exc.reason}") from exc
        except TimeoutError as exc:
            raise AIServiceError(f"{label} 请求超时，请稍后重试。") from exc

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
