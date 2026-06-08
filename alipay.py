from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml


CONFIG_PATH = Path(__file__).resolve().parent / "config" / "alipay.yaml"
DEFAULT_GATEWAY_URL = "https://openapi.alipay.com/gateway.do"
DEFAULT_NOTIFY_PATH = "/payments/alipay/notify"
DEFAULT_RETURN_PATH = "/payments/alipay/return"


class AlipayConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlipayConfig:
    app_id: str
    gateway_url: str
    sign_type: str
    charset: str
    notify_url: str
    return_url: str
    app_private_key: str
    alipay_public_key: str
    seller_id: str
    subject_prefix: str
    enabled: bool
    sandbox: bool
    problems: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "gateway_url": self.gateway_url,
            "notify_url": self.notify_url,
            "return_url": self.return_url,
            "sign_type": self.sign_type,
            "charset": self.charset,
            "seller_id": self.seller_id,
            "sandbox": self.sandbox,
            "problems": list(self.problems),
        }


def load_alipay_config(public_base_url: str = "", config_path: Path | None = None) -> AlipayConfig:
    path = config_path or CONFIG_PATH
    payload: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            payload = raw

    app_id = str(os.environ.get("ALIPAY_APP_ID") or payload.get("app_id") or "").strip()
    gateway_url = str(
        os.environ.get("ALIPAY_GATEWAY_URL")
        or payload.get("gateway_url")
        or DEFAULT_GATEWAY_URL
    ).strip()
    sign_type = str(os.environ.get("ALIPAY_SIGN_TYPE") or payload.get("sign_type") or "RSA2").strip().upper()
    charset = str(os.environ.get("ALIPAY_CHARSET") or payload.get("charset") or "utf-8").strip()
    seller_id = str(os.environ.get("ALIPAY_SELLER_ID") or payload.get("seller_id") or "").strip()
    subject_prefix = str(
        os.environ.get("ALIPAY_SUBJECT_PREFIX") or payload.get("subject_prefix") or "马恩文献检索会员"
    ).strip()
    sandbox = _as_bool(os.environ.get("ALIPAY_SANDBOX"), payload.get("sandbox", False))

    app_private_key = _load_key_value(
        env_value_name="ALIPAY_APP_PRIVATE_KEY",
        env_path_name="ALIPAY_APP_PRIVATE_KEY_PATH",
        payload_value=payload.get("app_private_key"),
        payload_path=payload.get("app_private_key_path"),
    )
    alipay_public_key = _load_key_value(
        env_value_name="ALIPAY_PUBLIC_KEY",
        env_path_name="ALIPAY_PUBLIC_KEY_PATH",
        payload_value=payload.get("alipay_public_key"),
        payload_path=payload.get("alipay_public_key_path"),
    )

    base_url = str(os.environ.get("PUBLIC_BASE_URL") or public_base_url or "").strip().rstrip("/")
    notify_url = str(os.environ.get("ALIPAY_NOTIFY_URL") or payload.get("notify_url") or "").strip()
    return_url = str(os.environ.get("ALIPAY_RETURN_URL") or payload.get("return_url") or "").strip()
    if not notify_url and base_url:
        notify_url = f"{base_url}{DEFAULT_NOTIFY_PATH}"
    if not return_url and base_url:
        return_url = f"{base_url}{DEFAULT_RETURN_PATH}"

    problems: list[str] = []
    if sign_type != "RSA2":
        problems.append(f"当前仅实现 RSA2 签名，收到 sign_type={sign_type!r}。")
    if not app_id:
        problems.append("未配置支付宝 app_id。")
    if not app_private_key:
        problems.append("未配置应用私钥 app_private_key。")
    if not alipay_public_key:
        problems.append("未配置支付宝公钥 alipay_public_key。")
    if not notify_url:
        problems.append("未配置 notify_url；请设置 PUBLIC_BASE_URL 或显式填写 ALIPAY_NOTIFY_URL。")
    if not return_url:
        problems.append("未配置 return_url；请设置 PUBLIC_BASE_URL 或显式填写 ALIPAY_RETURN_URL。")
    if not gateway_url:
        problems.append("未配置支付宝网关 gateway_url。")
    if charset.lower() != "utf-8":
        problems.append("当前仅测试 utf-8 字符集，已收到其他 charset 配置。")
    if (app_id or app_private_key or alipay_public_key) and not _has_crypto_support():
        problems.append("缺少 cryptography 依赖，无法执行支付宝 RSA2 签名/验签。")

    enabled = not problems

    return AlipayConfig(
        app_id=app_id,
        gateway_url=gateway_url,
        sign_type=sign_type,
        charset=charset,
        notify_url=notify_url,
        return_url=return_url,
        app_private_key=app_private_key,
        alipay_public_key=alipay_public_key,
        seller_id=seller_id,
        subject_prefix=subject_prefix,
        enabled=enabled,
        sandbox=sandbox,
        problems=tuple(problems),
    )


def _load_key_value(
    *,
    env_value_name: str,
    env_path_name: str,
    payload_value: Any,
    payload_path: Any,
) -> str:
    direct_value = str(os.environ.get(env_value_name) or payload_value or "").strip()
    if direct_value:
        return direct_value
    path_value = str(os.environ.get(env_path_name) or payload_path or "").strip()
    if not path_value:
        return ""
    key_path = Path(path_value)
    if not key_path.is_absolute():
        key_path = CONFIG_PATH.resolve().parent / key_path
    if not key_path.exists():
        return ""
    return key_path.read_text(encoding="utf-8").strip()


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _has_crypto_support() -> bool:
    try:
        from cryptography.hazmat.primitives import hashes  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import padding  # noqa: F401
        return True
    except Exception:
        return False


def _load_private_key(pem_text: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(pem_text.encode("utf-8"), password=None)


def _load_public_key(pem_text: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_public_key(pem_text.encode("utf-8"))


def _amount_to_yuan(amount_cents: int) -> str:
    value = (Decimal(int(amount_cents)) / Decimal("100")).quantize(
        Decimal("0.00"),
        rounding=ROUND_HALF_UP,
    )
    return format(value, "f")


class AlipayClient:
    def __init__(self, config: AlipayConfig) -> None:
        self.config = config

    def _ensure_enabled(self) -> None:
        if not self.config.enabled:
            raise AlipayConfigError("支付宝支付尚未完成配置。")

    def _build_sign_content(self, params: dict[str, Any]) -> str:
        items: list[tuple[str, str]] = []
        for key in sorted(params.keys()):
            if key in {"sign"}:
                continue
            value = params[key]
            if value is None or value == "":
                continue
            items.append((key, str(value)))
        return "&".join(f"{key}={value}" for key, value in items)

    def sign(self, params: dict[str, Any]) -> str:
        self._ensure_enabled()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        content = self._build_sign_content(params)
        private_key = _load_private_key(self.config.app_private_key)
        signature = private_key.sign(
            content.encode(self.config.charset),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def verify(self, params: dict[str, Any], signature: str) -> bool:
        self._ensure_enabled()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        content = self._build_sign_content(params)
        public_key = _load_public_key(self.config.alipay_public_key)
        try:
            public_key.verify(
                base64.b64decode(signature.encode("utf-8")),
                content.encode(self.config.charset),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception:
            return False

    def build_page_pay_url(
        self,
        *,
        order_no: str,
        subject: str,
        amount_cents: int,
        body: str = "",
    ) -> str:
        self._ensure_enabled()
        biz_content = {
            "out_trade_no": order_no,
            "product_code": "FAST_INSTANT_TRADE_PAY",
            "total_amount": _amount_to_yuan(amount_cents),
            "subject": subject[:256],
        }
        if body:
            biz_content["body"] = body[:128]

        params: dict[str, Any] = {
            "app_id": self.config.app_id,
            "method": "alipay.trade.page.pay",
            "charset": self.config.charset,
            "sign_type": self.config.sign_type,
            "timestamp": _timestamp_text(),
            "version": "1.0",
            "notify_url": self.config.notify_url,
            "return_url": self.config.return_url,
            "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
        }
        params["sign"] = self.sign(params)
        query = "&".join(f"{key}={quote_plus(str(value))}" for key, value in params.items())
        return f"{self.config.gateway_url}?{query}"

    def verify_callback_params(self, params: dict[str, Any]) -> bool:
        signature = str(params.get("sign") or "").strip()
        if not signature:
            return False
        payload = dict(params)
        payload.pop("sign", None)
        return self.verify(payload, signature)


def _timestamp_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
