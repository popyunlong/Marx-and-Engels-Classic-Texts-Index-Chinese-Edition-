from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlencode

import yaml


MAPI_TIMEOUT_SECONDS = 20


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "zpay.yaml"
DEFAULT_SUBMIT_URL = "https://zpayz.cn/submit.php"
DEFAULT_MAPI_URL = "https://zpayz.cn/mapi.php"
DEFAULT_API_URL = "https://zpayz.cn/api.php"
DEFAULT_NOTIFY_PATH = "/payments/zpay/notify"
DEFAULT_RETURN_PATH = "/payments/zpay/return"


@dataclass(frozen=True)
class ZPayConfig:
    pid: str
    key: str
    submit_url: str
    mapi_url: str
    api_url: str
    payment_type: str
    channel_id: str
    sign_type: str
    notify_url: str
    return_url: str
    subject_prefix: str
    device: str
    enabled: bool
    problems: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": "zpay",
            "gateway_url": self.submit_url,
            "submit_url": self.submit_url,
            "mapi_url": self.mapi_url,
            "api_url": self.api_url,
            "notify_url": self.notify_url,
            "return_url": self.return_url,
            "payment_type": self.payment_type,
            "channel_id": self.channel_id,
            "device": self.device,
            "sign_type": self.sign_type,
            "sandbox": False,
            "problems": list(self.problems),
        }


class ZPayConfigError(RuntimeError):
    pass


def load_zpay_config(public_base_url: str = "", config_path: Path | None = None) -> ZPayConfig:
    path = config_path or CONFIG_PATH
    payload: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            payload = raw

    pid = _pick("ZPAY_PID", payload, "pid")
    key = _pick("ZPAY_KEY", payload, "key")
    submit_url = _pick("ZPAY_SUBMIT_URL", payload, "submit_url", DEFAULT_SUBMIT_URL)
    mapi_url = _pick("ZPAY_MAPI_URL", payload, "mapi_url", DEFAULT_MAPI_URL)
    api_url = _pick("ZPAY_API_URL", payload, "api_url", DEFAULT_API_URL)
    payment_type = _pick("ZPAY_TYPE", payload, "type", "alipay")
    channel_id = _pick("ZPAY_CHANNEL_ID", payload, "channel_id")
    sign_type = _pick("ZPAY_SIGN_TYPE", payload, "sign_type", "MD5").upper()
    subject_prefix = _pick("ZPAY_SUBJECT_PREFIX", payload, "subject_prefix", "马恩文献检索会员")
    device = _pick("ZPAY_DEVICE", payload, "device", "pc")

    base_url = str(os.environ.get("PUBLIC_BASE_URL") or public_base_url or "").strip().rstrip("/")
    notify_url = _pick("ZPAY_NOTIFY_URL", payload, "notify_url")
    return_url = _pick("ZPAY_RETURN_URL", payload, "return_url")
    if not notify_url and base_url:
        notify_url = f"{base_url}{DEFAULT_NOTIFY_PATH}"
    if not return_url and base_url:
        return_url = f"{base_url}{DEFAULT_RETURN_PATH}"

    problems: list[str] = []
    if not pid:
        problems.append("未配置第四方支付商户 PID。")
    if not key:
        problems.append("未配置第四方支付商户密钥 KEY。")
    if not submit_url:
        problems.append("未配置第四方支付跳转网关 submit_url。")
    if sign_type != "MD5":
        problems.append(f"当前文档仅支持 MD5 签名，收到 sign_type={sign_type!r}。")
    if payment_type not in {"alipay", "wxpay"}:
        problems.append("支付方式 type 仅支持 alipay 或 wxpay。")
    if not notify_url:
        problems.append("未配置 notify_url；请设置 PUBLIC_BASE_URL 或显式填写 ZPAY_NOTIFY_URL。")
    if not return_url:
        problems.append("未配置 return_url；请设置 PUBLIC_BASE_URL 或显式填写 ZPAY_RETURN_URL。")

    return ZPayConfig(
        pid=pid,
        key=key,
        submit_url=submit_url,
        mapi_url=mapi_url,
        api_url=api_url,
        payment_type=payment_type,
        channel_id=channel_id,
        sign_type=sign_type,
        notify_url=notify_url,
        return_url=return_url,
        subject_prefix=subject_prefix,
        device=device,
        enabled=not problems,
        problems=tuple(problems),
    )


def _pick(env_name: str, payload: dict[str, Any], key: str, default: str = "") -> str:
    return str(os.environ.get(env_name) or payload.get(key) or default or "").strip()


def _amount_to_yuan(amount_cents: int) -> str:
    value = (Decimal(int(amount_cents)) / Decimal("100")).quantize(
        Decimal("0.00"),
        rounding=ROUND_HALF_UP,
    )
    return format(value, "f")


class ZPayClient:
    def __init__(self, config: ZPayConfig) -> None:
        self.config = config

    def _ensure_enabled(self) -> None:
        if not self.config.enabled:
            raise ZPayConfigError("第四方支付尚未完成配置。")

    def sign(self, params: dict[str, Any]) -> str:
        content = self._build_sign_content(params)
        return hashlib.md5(f"{content}{self.config.key}".encode("utf-8")).hexdigest()

    def verify_callback_params(self, params: dict[str, Any]) -> bool:
        self._ensure_enabled()
        signature = str(params.get("sign") or "").strip().lower()
        if not signature:
            return False
        expected = self.sign(params).lower()
        return hmac.compare_digest(signature, expected)

    def _build_sign_content(self, params: dict[str, Any]) -> str:
        items: list[tuple[str, str]] = []
        for key in sorted(params.keys()):
            if key in {"sign", "sign_type"}:
                continue
            value = params[key]
            if value is None or value == "":
                continue
            items.append((key, str(value)))
        return "&".join(f"{key}={value}" for key, value in items)

    def build_page_pay_url(
        self,
        *,
        order_no: str,
        subject: str,
        amount_cents: int,
        param: str = "",
    ) -> str:
        self._ensure_enabled()
        params: dict[str, Any] = {
            "pid": self.config.pid,
            "type": self.config.payment_type,
            "out_trade_no": order_no[:32],
            "notify_url": self.config.notify_url,
            "return_url": self.config.return_url,
            "name": subject[:100],
            "money": _amount_to_yuan(amount_cents),
        }
        if self.config.channel_id:
            params["cid"] = self.config.channel_id
        # device 指定支付产品：留空或 "pc" 时不下发，交由网关按浏览器 UA 自动判定（保持原行为）。
        # 显式设为 mobile/jump/wechat/qq 时下发，可绕开支付宝「当面付 PC 扫码」(precreate) 产品——
        # 该产品未签约会返回 ACQ.APPLY_PC_MERCHANT_CODE_ERROR 导致二维码无法生成。
        device = str(self.config.device or "").strip().lower()
        if device and device != "pc":
            params["device"] = device
        if param:
            params["param"] = param
        params["sign"] = self.sign(params)
        params["sign_type"] = self.config.sign_type
        query = "&".join(f"{key}={quote_plus(str(value))}" for key, value in params.items())
        return f"{self.config.submit_url}?{query}"

    def create_mapi_order(
        self,
        *,
        order_no: str,
        subject: str,
        amount_cents: int,
        client_ip: str = "",
        param: str = "",
    ) -> dict[str, Any]:
        """调用 mapi.php（API 接口）下单，返回二维码/支付链接，由本站自行渲染二维码。

        返回 dict：{ok, code, msg, payurl, qrcode, img, payurl2, trade_no, raw}。
        ok=True 表示网关返回 code==1（下单成功）。出错时 ok=False 且 msg 含错误信息，
        调用方据此回退到页面跳转或手动二维码，绝不抛出未捕获异常。
        """
        self._ensure_enabled()
        params: dict[str, Any] = {
            "pid": self.config.pid,
            "type": self.config.payment_type,
            "out_trade_no": order_no[:32],
            "notify_url": self.config.notify_url,
            "return_url": self.config.return_url,
            "name": subject[:100],
            "money": _amount_to_yuan(amount_cents),
            "clientip": (client_ip or "127.0.0.1").strip() or "127.0.0.1",
        }
        if self.config.channel_id:
            params["cid"] = self.config.channel_id
        device = str(self.config.device or "").strip().lower()
        if device and device != "pc":
            params["device"] = device
        if param:
            params["param"] = param
        params["sign"] = self.sign(params)
        params["sign_type"] = self.config.sign_type

        body = urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            self.config.mapi_url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "marx-search-zpay/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=MAPI_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "code": None, "msg": f"网关请求失败：{exc}", "raw": ""}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "code": None, "msg": "网关返回非 JSON。", "raw": raw[:500]}
        code = data.get("code")
        ok = str(code) == "1"
        return {
            "ok": ok,
            "code": code,
            "msg": str(data.get("msg") or ""),
            "payurl": str(data.get("payurl") or ""),
            "qrcode": str(data.get("qrcode") or ""),
            "img": str(data.get("img") or ""),
            "payurl2": str(data.get("payurl2") or ""),
            "trade_no": str(data.get("trade_no") or ""),
            "raw": raw[:500],
        }
