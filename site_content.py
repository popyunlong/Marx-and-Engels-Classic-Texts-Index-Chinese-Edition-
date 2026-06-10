from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2.ext import Extension

from runtime_env import APP_NAME, APPDATA_DIR


SITE_TEXT_OVERRIDES_PATH = APPDATA_DIR / "site_text_overrides.json"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_SITE_TEXT_CALL_RE = re.compile(r"site_text\(\s*['\"]([a-zA-Z0-9_.:-]+)['\"]")
_CHINESE_TEXT_RE = re.compile(r"[\u4e00-\u9fff][^<>{}\n\r]{1,120}")
_JINJA_RE = re.compile(r"({[{%#].*?[}%]})")


@dataclass(frozen=True)
class SiteTextDefinition:
    key: str
    group: str
    label: str
    default: str
    multiline: bool = True


SITE_TEXT_DEFINITIONS = (
    SiteTextDefinition("index.hero_title", "首页", "首页大标题（程序名）", APP_NAME, multiline=False),
    SiteTextDefinition("index.topbar_title", "首页", "顶部状态标题", "站内账号与会员系统已启用", multiline=False),
    SiteTextDefinition(
        "index.topbar_logged_in",
        "首页",
        "顶部已登录说明",
        "当前登录：{display_name}（{email}）",
        multiline=False,
    ),
    SiteTextDefinition(
        "index.topbar_logged_out",
        "首页",
        "顶部未登录说明",
        "当前未登录。现在开始使用站内账号，而不是服务器统一口令。",
    ),
    SiteTextDefinition(
        "index.hero_intro",
        "首页",
        "首页主说明",
        "欢迎来到经典文献检索程序。请随意在下方检索窗口中输入你想核查、定位的原著句子或词语，程序将提供引文位置。若要查看 PDF 原文及使用 AI 导学则需要开通会员。",
    ),
    SiteTextDefinition("index.stat_app_version_label", "首页", "首页模块：程序版本标签", "程序版本", multiline=False),
    SiteTextDefinition("index.stat_app_version_value", "首页", "首页模块：程序版本数字", "", multiline=False),
    SiteTextDefinition("index.stat_data_version_label", "首页", "首页模块：资料版本标签", "资料版本", multiline=False),
    SiteTextDefinition("index.stat_data_version_value", "首页", "首页模块：资料版本数字", "", multiline=False),
    SiteTextDefinition("index.stat_wenji_label", "首页", "首页模块：《文集》标签", "《文集》", multiline=False),
    SiteTextDefinition("index.stat_wenji_value", "首页", "首页模块：《文集》卷数数字", "", multiline=False),
    SiteTextDefinition("index.stat_quanji_label", "首页", "首页模块：《全集》标签", "《全集》", multiline=False),
    SiteTextDefinition("index.stat_quanji_value", "首页", "首页模块：《全集》卷数数字", "", multiline=False),
    SiteTextDefinition(
        "index.member_active",
        "首页",
        "会员提示：已开通",
        "你当前已经是会员，PDF 全文、页内图像和 AI 讲解都已解锁。",
    ),
    SiteTextDefinition(
        "index.member_logged_in",
        "首页",
        "会员提示：已登录未开通",
        "你已经登录，但还不是会员。开通会员后，全文阅读、PDF 页面和 AI 导学会自动开放。",
    ),
    SiteTextDefinition(
        "index.member_guest",
        "首页",
        "会员提示：访客",
        "普通访客仍可检索，但 PDF 全文、页内图像和 AI 讲解需要登录并开通会员。",
    ),
    SiteTextDefinition("index.chapter_search_title", "首页", "篇章直达标题", "篇章直达", multiline=False),
    SiteTextDefinition(
        "index.chapter_search_placeholder",
        "首页",
        "篇章直达输入框提示",
        "搜索篇目，如 共产党宣言、资本论、反杜林论…",
        multiline=False,
    ),
    SiteTextDefinition(
        "index.chapter_search_hint",
        "首页",
        "篇章直达说明",
        "选中后将直接进入「{label}」对应篇目阅读；不同书库会分别标注以便区分。",
    ),
    SiteTextDefinition(
        "index.chapter_search_disabled_placeholder",
        "首页",
        "篇章直达未开放输入框提示",
        "搜索篇目…",
        multiline=False,
    ),
    SiteTextDefinition("index.search_title", "首页", "检索面板标题", "引文检索", multiline=False),
    SiteTextDefinition("index.search_placeholder", "首页", "检索输入框提示", "把需要核查的原文粘贴到这里。"),
    SiteTextDefinition(
        "index.search_shortcut",
        "首页",
        "检索快捷键提示",
        "快捷键：Ctrl / Command + Enter",
        multiline=False,
    ),
    SiteTextDefinition("index.runtime_title", "首页", "运行状态标题", "运行状态", multiline=False),
    SiteTextDefinition("index.runtime_mode_label", "首页", "运行状态：模式标签", "当前模式", multiline=False),
    SiteTextDefinition("index.runtime_db_label", "首页", "运行状态：数据库标签", "数据库校验", multiline=False),
    SiteTextDefinition("index.runtime_ai_label", "首页", "运行状态：AI 标签", "AI 对话", multiline=False),
    SiteTextDefinition("index.runtime_root_label", "首页", "运行状态：资料目录标签", "资料目录", multiline=False),
    SiteTextDefinition("index.feature_kicker", "首页", "全文阅读功能栏小标题", "会员独立功能", multiline=False),
    SiteTextDefinition("index.feature_title", "首页", "全文阅读功能栏标题", "经典文献完整目录阅读", multiline=False),
    SiteTextDefinition(
        "index.feature_description",
        "首页",
        "全文阅读功能栏说明",
        "按卷浏览完整目录，直接进入全文阅读；翻页更顺滑，并带有本页优先的 AI 辅助讲解。",
    ),
    SiteTextDefinition("index.feature_action", "首页", "全文阅读功能栏按钮", "进入全文阅读", multiline=False),
    SiteTextDefinition("index.feature_locked", "首页", "全文阅读功能栏未开放按钮", "会员开放", multiline=False),
    SiteTextDefinition("index.reader_full_kicker", "首页", "全文阅读器入口小标题", "阅读器入口", multiline=False),
    SiteTextDefinition("index.reader_full_title", "首页", "全文阅读器入口标题", "全文阅读器", multiline=False),
    SiteTextDefinition(
        "index.reader_full_description",
        "首页",
        "全文阅读器入口说明",
        "按卷浏览各套文献目录与正文，适合连续阅读和定位原文页码。",
    ),
    SiteTextDefinition("index.reader_full_action", "首页", "全文阅读器入口按钮", "进入全文阅读", multiline=False),
    SiteTextDefinition("index.reader_full_available", "首页", "全文阅读器可用提示", "已可用", multiline=False),
    SiteTextDefinition("index.reader_full_login_required", "首页", "全文阅读器登录提示", "登录即可使用", multiline=False),
    SiteTextDefinition("index.reader_full_subscribe_required", "首页", "全文阅读器开通提示", "开通会员后使用", multiline=False),
    SiteTextDefinition("index.reader_full_unavailable", "首页", "全文阅读器未开放提示", "暂未开放", multiline=False),
    SiteTextDefinition("index.dictionary_kicker", "首页", "大辞典入口小标题", "辞典入口", multiline=False),
    SiteTextDefinition("index.dictionary_title", "首页", "大辞典入口标题", "马克思主义大辞典", multiline=False),
    SiteTextDefinition(
        "index.dictionary_description",
        "首页",
        "大辞典入口说明",
        "按词目拼音索引浏览概念、人物、著作、事件与理论条目，支持词语直达和规范页码引文。",
    ),
    SiteTextDefinition("index.dictionary_action", "首页", "大辞典入口按钮", "进入大辞典", multiline=False),
    SiteTextDefinition("index.dictionary_available", "首页", "大辞典可用提示", "已可用", multiline=False),
    SiteTextDefinition("index.dictionary_login_required", "首页", "大辞典登录提示", "登录即可使用", multiline=False),
    SiteTextDefinition("index.dictionary_subscribe_required", "首页", "大辞典开通提示", "开通会员后使用", multiline=False),
    SiteTextDefinition("index.dictionary_unavailable", "首页", "大辞典未开放提示", "暂未开放", multiline=False),
    SiteTextDefinition("index.reader_ai_kicker", "首页", "AI 导学阅读器入口小标题", "阅读器入口", multiline=False),
    SiteTextDefinition("index.reader_ai_title", "首页", "AI 导学阅读器入口标题", "AI 导学阅读器", multiline=False),
    SiteTextDefinition(
        "index.reader_ai_description",
        "首页",
        "AI 导学阅读器入口说明",
        "进入带 AI 辅助的阅读器，围绕当前页和相邻页文本解释概念、梳理论证脉络。",
    ),
    SiteTextDefinition("index.reader_ai_action", "首页", "AI 导学阅读器入口按钮", "进入 AI 导学", multiline=False),
    SiteTextDefinition("index.reader_ai_available", "首页", "AI 导学阅读器可用提示", "已可用", multiline=False),
    SiteTextDefinition("index.reader_ai_login_required", "首页", "AI 导学阅读器登录提示", "登录即可使用", multiline=False),
    SiteTextDefinition("index.reader_ai_subscribe_required", "首页", "AI 导学阅读器开通提示", "开通会员后使用", multiline=False),
    SiteTextDefinition("index.reader_ai_unavailable", "首页", "AI 导学阅读器未开放提示", "暂未开放", multiline=False),
    SiteTextDefinition("index.reader_ai_maintenance", "首页", "AI 导学阅读器维护提示", "AI 导学暂时维护中", multiline=False),
    SiteTextDefinition("index.journal_kicker", "首页", "期刊提醒功能栏小标题", "会员独立功能", multiline=False),
    SiteTextDefinition("index.journal_title", "首页", "期刊提醒功能栏标题", "期刊新文每日摘要", multiline=False),
    SiteTextDefinition(
        "index.journal_description",
        "首页",
        "期刊提醒功能栏说明",
        "订阅相关期刊后，系统每天 09:00（北京时间）汇总发送新公开文章的题目、摘要、作者和 GB/T 7714-2015 引文；英文文章附中文题名与摘要。",
    ),
    SiteTextDefinition("index.journal_action", "首页", "期刊提醒功能栏按钮", "管理期刊提醒", multiline=False),
    SiteTextDefinition("index.journal_locked", "首页", "期刊提醒功能栏未开放按钮", "会员订阅", multiline=False),
    SiteTextDefinition("index.notice_title", "首页", "首页公告栏标题", "网站公告", multiline=False),
    SiteTextDefinition(
        "index.notice_body",
        "首页",
        "首页公告栏内容",
        "这里显示面向访客和用户的公告。你可以在管理后台的内容运营中随时修改这段文字。",
    ),
    SiteTextDefinition("index.feedback_title", "首页", "首页留言栏标题", "留言反馈", multiline=False),
    SiteTextDefinition(
        "index.feedback_guest",
        "首页",
        "首页留言栏未登录提示",
        "登录后可以在这里提交意见和反馈；管理员回复后，会通过邮件通知你，并同步显示在本栏。",
    ),
    SiteTextDefinition(
        "index.feedback_intro",
        "首页",
        "首页留言栏已登录提示",
        "欢迎留下使用意见、问题或建议。管理员回复后会通过邮件通知你。",
    ),
    SiteTextDefinition("index.feedback_placeholder", "首页", "首页留言输入框提示", "请输入你的意见或反馈。"),
    SiteTextDefinition("index.feedback_submit", "首页", "首页留言提交按钮", "提交留言", multiline=False),
    SiteTextDefinition("index.feedback_history", "首页", "首页留言历史标题", "历史会话", multiline=False),
    SiteTextDefinition("index.feedback_empty", "首页", "首页留言历史为空提示", "暂无历史留言。", multiline=False),
    SiteTextDefinition("index.feedback_reply_notice", "首页", "首页留言回复提醒", "管理员有新的回复", multiline=False),
    SiteTextDefinition("index.ai_title", "首页", "首页 AI 面板标题", "AI 导学问答", multiline=False),
    SiteTextDefinition(
        "index.ai_status_loading",
        "首页",
        "首页 AI 状态加载提示",
        "正在检查 AI 服务状态...",
        multiline=False,
    ),
    SiteTextDefinition(
        "index.ai_prompt_placeholder",
        "首页",
        "首页 AI 输入框提示",
        "例如：马克思在《资本论》第1卷中如何分析商品拜物教？请简要说明。",
    ),
    SiteTextDefinition(
        "index.ai_empty_state",
        "首页",
        "首页 AI 空状态说明",
        "这里可以直接向 AI 提问。当前已关闭联网检索，回答仅由已配置的模型生成。",
    ),
    SiteTextDefinition(
        "index.ai_member_locked",
        "首页",
        "首页 AI 非会员锁定提示",
        "AI 导学问答为会员功能；非会员当前不能使用。请登录并开通会员后再提问。",
    ),
    SiteTextDefinition(
        "index.ai_unavailable",
        "首页",
        "首页 AI 维护提示",
        "AI 导学暂时维护中，请稍后再试。",
    ),
    SiteTextDefinition("pricing.hero_intro", "会员套餐", "套餐页顶部说明", "开通会员后，可使用全文阅读、原始 PDF 访问、页内文本与 AI 辅助讲解等功能。请选择适合你的会员方案。"),
    SiteTextDefinition(
        "pricing.payment_enabled",
        "会员套餐",
        "套餐页支付状态：已启用",
        "在线支付已开通。提交订单后将跳转到收银台，支付完成后会员权益会自动生效。",
    ),
    SiteTextDefinition(
        "pricing.payment_disabled",
        "会员套餐",
        "套餐页支付状态：未启用",
        "在线支付暂时不可用，请稍后再试，或联系管理员协助开通。",
    ),
    SiteTextDefinition("pricing.feature_viewer", "会员套餐", "套餐权益 1", "按目录阅读各套文献全文", multiline=False),
    SiteTextDefinition("pricing.feature_pdf", "会员套餐", "套餐权益 2", "查看原始 PDF 页面与页内文字", multiline=False),
    SiteTextDefinition("pricing.feature_ai", "会员套餐", "套餐权益 3", "使用 AI 辅助理解当前页内容", multiline=False),
    SiteTextDefinition("pricing.feature_account", "会员套餐", "套餐权益 4", "在会员中心查看订单与有效期", multiline=False),
    SiteTextDefinition(
        "account.badge_active",
        "会员中心",
        "会员中心顶部：已开通标签",
        "会员有效：{plan_name}{expires_suffix}",
        multiline=False,
    ),
    SiteTextDefinition(
        "account.badge_free",
        "会员中心",
        "会员中心顶部：未开通标签",
        "当前为普通账号，尚未开通会员。",
        multiline=False,
    ),
    SiteTextDefinition(
        "account.membership_note",
        "会员中心",
        "会员状态说明",
        "这里显示你的会员方案和有效期。会员有效时，即可使用全文阅读、PDF 页面查看和 AI 辅助讲解。",
    ),
    SiteTextDefinition(
        "account.empty_orders",
        "会员中心",
        "空订单提示",
        "还没有订单。你可以到套餐页选择会员方案并开通。",
    ),
    SiteTextDefinition(
        "account.empty_subscriptions",
        "会员中心",
        "空订阅提示",
        "还没有有效订阅。开通会员后，这里会显示你的会员方案和有效期。",
    ),
    SiteTextDefinition(
        "account.payment_enabled",
        "会员中心",
        "支付状态说明：已启用",
        "在线支付已开通。支付完成后，系统会自动更新订单和会员状态。",
    ),
    SiteTextDefinition(
        "account.payment_disabled",
        "会员中心",
        "支付状态说明：未启用",
        "在线支付暂时不可用，请稍后再试，或联系管理员协助开通。",
    ),
    SiteTextDefinition(
        "login.intro",
        "登录注册",
        "登录页说明",
        "登录后即可进入会员中心、创建订单，并在开通会员后访问 PDF 全文与 AI 讲解。",
    ),
    SiteTextDefinition("login.links_register", "登录注册", "登录页：注册引导", "还没有账号？", multiline=False),
    SiteTextDefinition("login.links_pricing", "登录注册", "登录页：套餐引导", "想先看看会员方案？", multiline=False),
    SiteTextDefinition(
        "register.intro",
        "登录注册",
        "注册页说明",
        "创建账号后即可开通会员，并在会员中心查看订单、有效期和订阅记录。",
    ),
    SiteTextDefinition(
        "register.email_unavailable",
        "登录注册",
        "注册页：邮箱服务不可用提示",
        "邮件服务暂时不可用，暂时无法发送邮箱验证码。请稍后再试。",
    ),
    SiteTextDefinition("register.links_login", "登录注册", "注册页：登录引导", "已有账号？", multiline=False),
    SiteTextDefinition("register.links_pricing", "登录注册", "注册页：套餐引导", "想先了解套餐？", multiline=False),
    SiteTextDefinition("payment_result.status_paid", "支付结果", "支付结果：成功标签", "支付成功", multiline=False),
    SiteTextDefinition("payment_result.status_pending", "支付结果", "支付结果：处理中标签", "支付处理中 / 待支付", multiline=False),
    SiteTextDefinition(
        "payment_result.intro_paid",
        "支付结果",
        "支付结果：成功说明",
        "订单已完成支付。如果这是会员订单，会员权限已经开通；你现在可以返回会员中心，或直接回到首页继续使用。",
    ),
    SiteTextDefinition(
        "payment_result.intro_pending",
        "支付结果",
        "支付结果：待处理说明",
        "当前订单还未完成支付，支付状态可能仍在确认中。你可以稍后刷新会员中心，或者直接重新拉起支付。",
    ),
    SiteTextDefinition("library.chapter_search_label", "阅读页", "阅读器篇章直达标题", "篇章直达 · 搜索全部资料篇目", multiline=False),
    SiteTextDefinition(
        "library.chapter_search_placeholder",
        "阅读页",
        "阅读器篇章直达输入框提示",
        "输入篇名，如 共产党宣言、资本论、反杜林论…",
        multiline=False,
    ),
    SiteTextDefinition(
        "library.chapter_search_hint",
        "阅读页",
        "阅读器篇章直达说明",
        "输入篇名关键词，从下拉结果中选择即可直接进入对应卷的阅读位置；不同书库会分别标注以便区分。",
    ),
    SiteTextDefinition(
        "library.volume_hint",
        "阅读页",
        "阅读器按卷浏览提示",
        "下面按卷收起，点击任意一卷的标题即可展开该卷目录；也可以直接用上方的「篇章直达」搜索框定位篇目。",
    ),
    SiteTextDefinition("dictionary.search_label", "大辞典", "大辞典词条直达标题", "词语直达 · 搜索本页词条", multiline=False),
    SiteTextDefinition(
        "dictionary.search_placeholder",
        "大辞典",
        "大辞典词条直达输入框提示",
        "输入词语，如 资本、矛盾、共产党宣言…",
        multiline=False,
    ),
    SiteTextDefinition(
        "dictionary.search_hint",
        "大辞典",
        "大辞典词条直达说明",
        "输入词语关键词，从下拉结果中选择即可打开对应解释；也可以向下按 A-Z 拼音栏目浏览。",
    ),
    SiteTextDefinition("dictionary.heading", "大辞典", "大辞典目录页标题", "马克思主义大辞典", multiline=False),
    SiteTextDefinition(
        "dictionary.description",
        "大辞典",
        "大辞典目录页说明",
        "词条按书末词目拼音索引聚合；解释正文经过自动精修，并在下方保留对应书籍页码引文。",
    ),
    SiteTextDefinition("library.reader_heading", "阅读页", "全文阅读器目录页标题", "经典文献完整目录", multiline=False),
    SiteTextDefinition(
        "library.reader_description",
        "阅读页",
        "全文阅读器目录页说明",
        "按卷浏览目录，点击任一条目即可进入全文阅读器。",
    ),
    SiteTextDefinition("library.ai_heading", "阅读页", "AI 导学阅读器目录页标题", "经典文献 AI 导学阅读", multiline=False),
    SiteTextDefinition(
        "library.ai_description",
        "阅读页",
        "AI 导学阅读器目录页说明",
        "按卷浏览目录，点击任一条目即可进入带 AI 辅助的阅读器。目录优先使用已生成的精校数据，旧数据缺失时自动回退到 PDF 书签。",
    ),
    SiteTextDefinition(
        "library.ai_locked_description",
        "阅读页",
        "AI 导学阅读器无权限说明",
        "当前账号暂未开放 AI 导学。你仍可使用全文阅读器浏览目录与正文。",
    ),
    SiteTextDefinition(
        "journal.email_unavailable",
        "期刊提醒",
        "期刊订阅：邮箱服务不可用提示",
        "邮件服务暂时不可用，订阅确认邮件暂时无法发出。请稍后再试。",
    ),
    SiteTextDefinition("viewer.empty_toc", "阅读页", "目录为空提示", "这个 PDF 暂时没有可用目录，但仍可翻页浏览与讲解。"),
    SiteTextDefinition("viewer.page_text_title", "阅读页", "当前页文本标题", "当前页文本", multiline=False),
    SiteTextDefinition(
        "viewer.page_text_note",
        "阅读页",
        "当前页文本说明",
        "这里的文字可直接选中后交给右侧 AI 解释。若页面图像加载失败，文本讲解仍可正常使用。",
    ),
    SiteTextDefinition(
        "viewer.viewer_note",
        "阅读页",
        "阅读页底部说明",
        "页面图像来自后端稳定渲染；页下方文本来自本地语料，可直接选中并交给 AI 讲解。",
    ),
    SiteTextDefinition("viewer.ai_upsell_available_title", "阅读页", "纯阅读页 AI 导学提示：已开放标题", "你已可以使用 AI 导学", multiline=False),
    SiteTextDefinition(
        "viewer.ai_upsell_available_body",
        "阅读页",
        "纯阅读页 AI 导学提示：已开放说明",
        "当前账号已满足 AI 导学权限，可进入带 AI 讲解的阅读器，围绕本页和相邻页文本解释概念、梳理论证脉络。",
    ),
    SiteTextDefinition("viewer.ai_upsell_available_action", "阅读页", "纯阅读页 AI 导学提示：已开放按钮", "进入 AI 导学", multiline=False),
    SiteTextDefinition("viewer.ai_upsell_login_required_title", "阅读页", "纯阅读页 AI 导学提示：登录标题", "登录后可使用 AI 导学", multiline=False),
    SiteTextDefinition(
        "viewer.ai_upsell_login_required_body",
        "阅读页",
        "纯阅读页 AI 导学提示：登录说明",
        "登录后即可进入带 AI 讲解的阅读器，让 AI 根据当前页和相邻页文本辅助理解原著内容。",
    ),
    SiteTextDefinition("viewer.ai_upsell_login_required_action", "阅读页", "纯阅读页 AI 导学提示：登录按钮", "登录后使用", multiline=False),
    SiteTextDefinition("viewer.ai_upsell_subscribe_required_title", "阅读页", "纯阅读页 AI 导学提示：会员标题", "开通会员后可使用 AI 导学", multiline=False),
    SiteTextDefinition(
        "viewer.ai_upsell_subscribe_required_body",
        "阅读页",
        "纯阅读页 AI 导学提示：会员说明",
        "当前站点设置为会员开放 AI 导学。开通后可进入带 AI 讲解的阅读器，结合本页文本进行提问与解释。",
    ),
    SiteTextDefinition("viewer.ai_upsell_subscribe_required_action", "阅读页", "纯阅读页 AI 导学提示：会员按钮", "查看会员方案", multiline=False),
    SiteTextDefinition("viewer.ai_upsell_unavailable_title", "阅读页", "纯阅读页 AI 导学提示：未开放标题", "AI 导学暂未开放", multiline=False),
    SiteTextDefinition(
        "viewer.ai_upsell_unavailable_body",
        "阅读页",
        "纯阅读页 AI 导学提示：未开放说明",
        "当前站点暂未开放 AI 导学功能，你仍可继续使用全文阅读器浏览、定位和复制当前页文本。",
    ),
    SiteTextDefinition("viewer.ai_upsell_unavailable_action", "阅读页", "纯阅读页 AI 导学提示：未开放按钮", "了解开放状态", multiline=False),
    SiteTextDefinition("viewer.ai_title", "阅读页", "阅读页 AI 标题", "页面讲解", multiline=False),
    SiteTextDefinition(
        "viewer.ai_status_loading",
        "阅读页",
        "阅读页 AI 状态加载提示",
        "正在检查 AI 服务状态...",
        multiline=False,
    ),
    SiteTextDefinition("viewer.selected_title", "阅读页", "选中文本标题", "当前选中内容", multiline=False),
    SiteTextDefinition(
        "viewer.selected_empty",
        "阅读页",
        "未选中文本提示",
        "未选中任何文本。你可以先在“当前页文本”区域中划词，或直接点击“解释本页内容”。",
    ),
    SiteTextDefinition(
        "viewer.prompt_placeholder",
        "阅读页",
        "阅读页 AI 输入框提示",
        "例如：这段文字中的“联合起来”在这里具体指什么？请结合当前页说明。",
    ),
    SiteTextDefinition(
        "viewer.ai_empty_state",
        "阅读页",
        "阅读页 AI 空状态说明",
        "AI 会根据当前页和相邻页文本解释内容。当前已关闭联网检索。",
    ),
    SiteTextDefinition("viewer.page_context_loading", "阅读页", "页文本加载提示", "正在加载当前页文本...", multiline=False),
    SiteTextDefinition("viewer.page_context_unavailable", "阅读页", "页文本不可用提示", "当前页文本暂不可用。", multiline=False),
    SiteTextDefinition(
        "viewer.image_error",
        "阅读页",
        "图像加载失败提示",
        "当前页图像加载失败，但下方文本与右侧 AI 讲解仍可继续使用。",
    ),
    # 联想检索：页面 JS 内的用户可见文字，通过模板 {{ site_text(..) | tojson }} 注入到脚本里，可后台编辑。
    SiteTextDefinition(
        "search.assoc_placeholder",
        "联想检索",
        "联想检索输入框提示",
        "用你自己的话描述大意或关键词，例如：生产力决定生产关系那一段。AI 会在原著中帮你定位。",
    ),
    SiteTextDefinition(
        "search.assoc_loading",
        "联想检索",
        "联想检索加载提示",
        "AI 正在理解你的描述，并在原著中定位相关段落，请稍候...",
        multiline=False,
    ),
    SiteTextDefinition(
        "search.assoc_no_result",
        "联想检索",
        "联想检索无结果提示",
        "未找到匹配结果，请换一种说法或补充更具体的关键词。",
    ),
    SiteTextDefinition(
        "search.assoc_no_ai",
        "联想检索",
        "联想检索无 AI 权限提示",
        "联想检索需要 AI 权限，请先开通后再使用。",
    ),
    SiteTextDefinition(
        "search.assoc_tab_title",
        "联想检索",
        "联想检索标签禁用悬浮提示",
        "联想检索需要 AI 权限",
        multiline=False,
    ),
    SiteTextDefinition(
        "search.assoc_result_heading",
        "联想检索",
        "联想检索结果标题",
        "联想检索结果",
        multiline=False,
    ),
    SiteTextDefinition(
        "search.assoc_result_lede",
        "联想检索",
        "联想检索结果说明（{count}=定位段数，{volumes}=涉及卷数）",
        "AI 在原著中定位到 {count} 段（共 {volumes} 卷），按匹配权重优先排序；引文与页码均来自真实出处。",
    ),
    SiteTextDefinition("error.back_home", "错误页", "错误页返回首页按钮", "返回首页", multiline=False),
    SiteTextDefinition("error.back_search", "错误页", "错误页返回检索按钮", "返回检索", multiline=False),
)

_SITE_TEXT_INDEX = {item.key: item for item in SITE_TEXT_DEFINITIONS}

# 运行期追加的定义：例如 app.py 按真实书目动态注册的「首页卷数 pill」。这些 key 在
# 模板里以变量形式调用（site_text(item.text_key)），静态扫描发现不了，必须显式注册，
# 才能与静态定义同等地参与渲染、后台分组展示与覆盖保存。
_EXTRA_DEFINITIONS: list[SiteTextDefinition] = []


def register_site_text_definitions(definitions) -> None:
    """注册额外的站点文案定义（幂等：已存在的 key 跳过）。"""
    for definition in definitions:
        if definition.key in _SITE_TEXT_INDEX:
            continue
        _EXTRA_DEFINITIONS.append(definition)
        _SITE_TEXT_INDEX[definition.key] = definition


def _all_static_definitions() -> list[SiteTextDefinition]:
    return list(SITE_TEXT_DEFINITIONS) + _EXTRA_DEFINITIONS


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _load_overrides(path: Path | None = None) -> dict[str, str]:
    target = path or SITE_TEXT_OVERRIDES_PATH
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    values: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            values[key] = value
    return values


def _iter_template_paths(template_dir: Path | None = None) -> list[Path]:
    root = template_dir or TEMPLATE_DIR
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.html") if path.is_file())


def _dynamic_group_for_key(key: str) -> str:
    prefix = key.split(".", 1)[0]
    return {
        "index": "首页",
        "pricing": "会员套餐",
        "account": "会员中心",
        "login": "登录注册",
        "register": "登录注册",
        "payment_result": "支付结果",
        "library": "阅读页",
        "viewer": "阅读页",
        "dictionary": "大辞典",
        "journal": "期刊提醒",
        "error": "错误页",
    }.get(prefix, "自动发现")


def discover_template_site_text_keys(template_dir: Path | None = None) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    root = template_dir or TEMPLATE_DIR
    for path in _iter_template_paths(root):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for key in _SITE_TEXT_CALL_RE.findall(text):
            found.setdefault(key, set()).add(rel)
    return {key: sorted(paths) for key, paths in sorted(found.items())}


def _dynamic_definitions_from_templates(
    current: dict[str, str] | None = None,
    template_dir: Path | None = None,
) -> list[SiteTextDefinition]:
    used = discover_template_site_text_keys(template_dir)
    extra_keys = set(used) - set(_SITE_TEXT_INDEX)
    if current:
        extra_keys.update(key for key in current if key not in _SITE_TEXT_INDEX)
    # auto.* 由「自动接入文字」体系单独管理，不再混入「自动发现」动态 key，
    # 否则同一条文字会在两个分组里各出现一次。
    extra_keys = {key for key in extra_keys if not key.startswith(_AUTO_KEY_PREFIX)}
    return [
        SiteTextDefinition(
            key=key,
            group=_dynamic_group_for_key(key),
            label=f"自动发现：{key}",
            default=key,
            multiline=True,
        )
        for key in sorted(extra_keys)
    ]


def get_site_text_map(path: Path | None = None) -> dict[str, str]:
    overrides = _load_overrides(path)
    values = {
        definition.key: overrides.get(definition.key, definition.default)
        for definition in _all_static_definitions()
    }
    for definition in _dynamic_definitions_from_templates(overrides):
        values[definition.key] = overrides.get(definition.key, definition.default)
    return values


def render_site_text(key: str, /, **kwargs: object) -> str:
    definition = _SITE_TEXT_INDEX.get(key)
    base = definition.default if definition else key
    text = get_site_text_map().get(key, base)
    try:
        return text.format_map(_SafeFormatDict({k: "" if v is None else str(v) for k, v in kwargs.items()}))
    except Exception:
        return text


def list_site_text_groups() -> list[dict[str, object]]:
    return list_site_text_groups_from_map(get_site_text_map())


def list_site_text_groups_from_map(current: dict[str, str]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    auto_definitions = globals().get("auto_literal_definitions", lambda: [])
    definitions = (
        _all_static_definitions()
        + _dynamic_definitions_from_templates(current)
        + auto_definitions()
    )
    seen: set[str] = set()
    for definition in definitions:
        if definition.key in seen:
            continue
        seen.add(definition.key)
        groups.setdefault(definition.group, []).append(
            {
                "key": definition.key,
                "label": definition.label,
                "default": definition.default,
                "value": current.get(definition.key, definition.default),
                "multiline": definition.multiline,
            }
        )
    return [
        {"group": group, "entries": entries}
        for group, entries in groups.items()
    ]


def save_site_text_overrides(values: dict[str, str], path: Path | None = None) -> None:
    target = path or SITE_TEXT_OVERRIDES_PATH
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {}
    auto_definitions = globals().get("auto_literal_definitions", lambda: [])
    definitions = (
        _all_static_definitions()
        + _dynamic_definitions_from_templates(values)
        + auto_definitions()
    )
    seen: set[str] = set()
    for definition in definitions:
        if definition.key in seen:
            continue
        seen.add(definition.key)
        if definition.key not in values:
            continue
        value = str(values[definition.key])
        if value != definition.default:
            payload[definition.key] = value
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_site_text_overrides(values: dict[str, str], path: Path | None = None) -> None:
    """合并写入若干文案覆盖：只改动给定 key，其余覆盖原样保留。

    值等于框架默认值时移除该 key（即恢复默认）。用于「网站公告」等局部小表单，避免像
    整表保存那样用提交内容整体替换覆盖文件而误删其他文案。
    """
    target = path or SITE_TEXT_OVERRIDES_PATH
    current = _load_overrides(target)
    for key, raw in values.items():
        value = str(raw)
        definition = _SITE_TEXT_INDEX.get(key)
        default = definition.default if definition else ""
        if value == default:
            current.pop(key, None)
        else:
            current[key] = value
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def reset_site_text_overrides(path: Path | None = None) -> None:
    target = path or SITE_TEXT_OVERRIDES_PATH
    if target.exists():
        target.unlink()


def prune_stale_overrides(path: Path | None = None) -> list[str]:
    """从本地覆盖文件中删除失效（框架里已不存在）的文案 key，返回被删除的 key 列表。"""
    target = path or SITE_TEXT_OVERRIDES_PATH
    current = _load_overrides(target)
    if not current:
        return []
    stale = stale_override_keys(current)
    if not stale:
        return []
    remaining = {key: value for key, value in current.items() if key not in stale}
    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(remaining, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return stale


def _line_has_editable_site_text(line: str) -> bool:
    return "site_text(" in line or "site_text__" in line


def _clean_literal_candidate(text: str) -> str:
    text = _JINJA_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n：:，,。.;；、|/·")


# 后台控制台自身的模板不属于「站点文案」，不纳入硬编码候选扫描，避免淹没真正面向
# 用户的页面（首页、阅读页、会员页等）。
_UNMANAGED_SCAN_SKIP = {"control.html"}


def find_unmanaged_template_literals(
    template_dir: Path | None = None,
    *,
    limit: int = 400,
) -> list[dict[str, object]]:
    root = template_dir or TEMPLATE_DIR
    candidates: list[dict[str, object]] = []
    for path in _iter_template_paths(root):
        rel = path.relative_to(root).as_posix()
        if rel in _UNMANAGED_SCAN_SKIP:
            continue
        in_script = False
        in_style = False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            lowered = line.lower()
            if "<script" in lowered:
                in_script = True
            if "<style" in lowered:
                in_style = True
            skip_line = in_style or _line_has_editable_site_text(line)
            if not skip_line:
                for raw in _CHINESE_TEXT_RE.findall(line):
                    candidate = _clean_literal_candidate(raw)
                    if len(candidate) < 2:
                        continue
                    if any(mark in candidate for mark in ("class=", "href=", "url_for(", "csrf_token")):
                        continue
                    candidates.append(
                        {
                            "template": rel,
                            "line": line_no,
                            "text": candidate[:120],
                            "in_script": in_script,
                        }
                    )
                    if len(candidates) >= limit:
                        return candidates
            if "</script>" in lowered:
                in_script = False
            if "</style>" in lowered:
                in_style = False
    return candidates


def stale_override_keys(
    overrides: dict[str, str] | None = None,
    template_dir: Path | None = None,
) -> list[str]:
    """已保存、但在当前代码框架里既不是登记文案、也不再出现在任何模板里的 key。

    这些就是「旧的文字框架内容却还保留着」的部分：模板改版后遗留下来的失效文案缓存。
    传入 overrides 时基于该集合判断（用于服务器后台库设置）；否则读本地覆盖文件。
    """
    current = overrides if overrides is not None else _load_overrides()
    used_keys = set(discover_template_site_text_keys(template_dir))
    registered = {definition.key for definition in SITE_TEXT_DEFINITIONS}
    auto_keys = auto_literal_keys(template_dir)
    live = used_keys | registered | auto_keys
    return sorted(key for key in current if key not in live)


def site_text_coverage_report(
    template_dir: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    used = discover_template_site_text_keys(template_dir)
    registered = {definition.key for definition in SITE_TEXT_DEFINITIONS}
    used_keys = set(used)
    current_overrides = overrides if overrides is not None else _load_overrides()
    override_keys = set(current_overrides)
    auto_defs = auto_literal_definitions(template_dir)
    auto_keys = {definition.key for definition in auto_defs}
    # auto.* 既不算「自动发现 key」也不算「失效缓存」——它们是已自动接入、可编辑的正文文字。
    live_managed = used_keys | registered | auto_keys
    dynamic_keys = sorted((used_keys | override_keys) - registered - auto_keys)
    unused_registered = sorted(registered - used_keys)
    stale = sorted(key for key in override_keys if key not in live_managed)
    auto_edited = sorted(key for key in override_keys if key in auto_keys)
    return {
        "registered_count": len(registered),
        "template_key_count": len(used_keys),
        "dynamic_keys": [
            {"key": key, "templates": used.get(key, [])}
            for key in dynamic_keys
        ],
        "dynamic_count": len(dynamic_keys),
        "unused_registered_count": len(unused_registered),
        "unused_registered": unused_registered[:40],
        "auto_literal_count": len(auto_keys),
        "auto_edited_count": len(auto_edited),
        "stale_override_count": len(stale),
        "stale_overrides": stale,
    }


# ---------------------------------------------------------------------------
# 自动接入文字（全量动态）
#
# 目标：模板里没有手工写成 site_text(...) 的中文静态文字（按钮、标题、正文说明），
# 也能在后台直接编辑。做法是在 Jinja 编译前（preprocess）把「HTML 文本内容区」的
# 中文文字就地替换成 {{ site_text_auto('auto.<hash>', '<base64 原文>') }}：
#   - key 取自原文的 sha1，相同文字（如多处「进入全文阅读」）合并成同一个可编辑项；
#   - 原文以 base64 内联随调用携带，site_text_auto 在没有覆盖值时原样还原，
#     因此「未设置覆盖时，页面输出与改造前逐字节一致」；
#   - 只处理 HTML 文本内容，严格跳过标签内部（属性）、<script>/<style>、HTML 注释、
#     以及所有 Jinja 表达式/语句块，避免破坏结构；
#   - 注入器一旦抛错就回退原模板，绝不让线上渲染崩溃。
# ---------------------------------------------------------------------------

_AUTO_KEY_PREFIX = "auto."
_AUTO_GROUP = "自动接入文字（按钮 / 标题 / 正文）"
# 控制台自身模板不做自动接入：用户在后台编辑的是「除控制台之外」的页面文字。
_AUTO_INJECT_SKIP = {"control.html"}


def auto_key_for(core: str) -> str:
    digest = hashlib.sha1(core.encode("utf-8")).hexdigest()[:10]
    return _AUTO_KEY_PREFIX + digest


def _is_jinja_open(source: str, i: int) -> bool:
    return source[i] == "{" and i + 1 < len(source) and source[i + 1] in "{%#"


def _consume_jinja(source: str, i: int) -> int:
    """source[i] 处是 {{ / {% / {#，返回闭合标记之后的下标。"""
    close = {"{": "}}", "%": "%}", "#": "#}"}[source[i + 1]]
    j = source.find(close, i + 2)
    return len(source) if j == -1 else j + len(close)


def _consume_comment(source: str, i: int) -> int:
    j = source.find("-->", i + 4)
    return len(source) if j == -1 else j + 3


def _consume_tag(source: str, i: int) -> int:
    """source[i] == '<'，跳过引号串与标签内部 Jinja，返回闭合 '>' 之后的下标。"""
    n = len(source)
    j = i + 1
    while j < n:
        c = source[j]
        if c == ">":
            return j + 1
        if c in "\"'":
            k = source.find(c, j + 1)
            j = n if k == -1 else k + 1
            continue
        if _is_jinja_open(source, j):
            j = _consume_jinja(source, j)
            continue
        j += 1
    return n


def _find_raw_block_end(source: str, i: int, name: str) -> int:
    """从 i 起找 </script> 或 </style>（含），返回其后下标。"""
    match = re.compile(r"</\s*" + re.escape(name) + r"\s*>", re.IGNORECASE).search(source, i)
    return match.end() if match else len(source)


def _wrap_chinese_text(chunk: str, cores: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        core = raw.rstrip()                 # 仅剥掉尾部空白，正文一字不动
        trailing = raw[len(core):]
        if len(_clean_literal_candidate(core)) < 2:
            return raw
        cores.append(core)
        key = auto_key_for(core)
        b64 = base64.b64encode(core.encode("utf-8")).decode("ascii")
        return "{{ site_text_auto('%s', '%s') }}%s" % (key, b64, trailing)

    return _CHINESE_TEXT_RE.sub(repl, chunk)


def _process_template_source(source: str) -> tuple[str, list[str]]:
    """返回 (重写后的源码, 被接入的中文核心文本列表)。注入与发现共用同一套切分。"""
    n = len(source)
    out: list[str] = []
    cores: list[str] = []
    i = 0
    while i < n:
        if _is_jinja_open(source, i):
            j = _consume_jinja(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source.startswith("<!--", i):
            j = _consume_comment(source, i)
            out.append(source[i:j])
            i = j
            continue
        if source[i] == "<":
            j = _consume_tag(source, i)
            tag = source[i:j]
            out.append(tag)
            i = j
            name_match = re.match(r"<\s*([a-zA-Z][a-zA-Z0-9]*)", tag)
            name = name_match.group(1).lower() if name_match else ""
            if name in ("script", "style") and not tag.rstrip().endswith("/>"):
                end = _find_raw_block_end(source, i, name)
                out.append(source[i:end])
                i = end
            continue
        # HTML 文本内容：扫到下一个 '<' 或 Jinja 开头为止（独立的 '{' 算普通文字）。
        j = i
        while j < n and source[j] != "<" and not _is_jinja_open(source, j):
            j += 1
        out.append(_wrap_chinese_text(source[i:j], cores))
        i = j
    return "".join(out), cores


def _template_basename(name: str | None) -> str:
    if not name:
        return ""
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def inject_auto_site_text(source: str, name: str | None = None) -> str:
    """Jinja preprocess 钩子：把 HTML 文本内容里的中文就地接入文案系统。

    任何异常都回退原文，保证「最坏情况只是没接入，而不是渲染崩溃」。
    """
    if _template_basename(name) in _AUTO_INJECT_SKIP:
        return source
    try:
        rewritten, _ = _process_template_source(source)
        return rewritten
    except Exception:
        return source


class AutoSiteTextExtension(Extension):
    """在模板编译前接入自动文案；通过 app.jinja_env.add_extension 挂载。"""

    def preprocess(self, source: str, name: str | None, filename: str | None = None) -> str:
        return inject_auto_site_text(source, name)


def render_auto_site_text(key: str, b64default: str = "", overrides: dict[str, str] | None = None) -> str:
    """模板里 site_text_auto 的后端实现：有覆盖值用覆盖值，否则还原内联默认文字。"""
    if isinstance(overrides, dict):
        val = overrides.get(key)
        if isinstance(val, str) and val != "":
            return val
    try:
        return base64.b64decode(b64default.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


_AUTO_DISCOVERY_CACHE: dict[str, object] = {"sig": None, "data": None}


def _template_signature(root: Path) -> tuple:
    sig: list[tuple] = []
    for path in _iter_template_paths(root):
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def discover_auto_literals(template_dir: Path | None = None) -> dict[str, dict[str, object]]:
    """扫描所有（非控制台）模板，返回 {auto_key: {default, templates}}。

    结果按模板文件签名缓存，避免每次后台渲染都重新分词。
    """
    root = template_dir or TEMPLATE_DIR
    sig = _template_signature(root)
    if template_dir is None and _AUTO_DISCOVERY_CACHE["sig"] == sig and _AUTO_DISCOVERY_CACHE["data"] is not None:
        return _AUTO_DISCOVERY_CACHE["data"]  # type: ignore[return-value]
    found: dict[str, dict[str, object]] = {}
    for path in _iter_template_paths(root):
        if path.name in _AUTO_INJECT_SKIP:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            _, cores = _process_template_source(text)
        except Exception:
            continue
        for core in cores:
            key = auto_key_for(core)
            entry = found.get(key)
            if entry is None:
                found[key] = {"default": core, "templates": [rel]}
            elif rel not in entry["templates"]:  # type: ignore[operator]
                entry["templates"].append(rel)  # type: ignore[union-attr]
    result = dict(sorted(found.items(), key=lambda kv: str(kv[1]["default"])))
    if template_dir is None:
        _AUTO_DISCOVERY_CACHE["sig"] = sig
        _AUTO_DISCOVERY_CACHE["data"] = result
    return result


def auto_literal_definitions(template_dir: Path | None = None) -> list[SiteTextDefinition]:
    definitions: list[SiteTextDefinition] = []
    for key, info in discover_auto_literals(template_dir).items():
        default = str(info["default"])
        label = default if len(default) <= 16 else default[:16] + "…"
        definitions.append(
            SiteTextDefinition(
                key=key,
                group=_AUTO_GROUP,
                label=label,
                default=default,
                multiline=len(default) > 18,
            )
        )
    return definitions


def auto_literal_keys(template_dir: Path | None = None) -> set[str]:
    return set(discover_auto_literals(template_dir))
