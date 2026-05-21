"""
HeroSMS Phone Service — 替代 SMSToMe，使用 SMS-Activate 兼容 API

API 基础地址: https://hero-sms.com/stubs/handler_api.php
认证方式: api_key 作为 query 参数

Country codes (SMS-Activate 标准):
  日本: 22
  美国: 12
  泰国: 66

Service code for ChatGPT/OpenAI: go

接口:
  getNumber:  ?api_key=xxx&action=getNumber&service=go&country=22
    → "access.1111111.2222222" (access.ID.PHONE)
    → "no_numbers" / "no_balance"
  getStatus:  ?api_key=xxx&action=getStatus&id=ACTIVATION_ID
    → status.1 (pending), status.2 (ready), status.3+CODE (received)
    → status.6 (complete), status.8 (cancel)
  setStatus:  ?api_key=xxx&action=setStatus&id=ACTIVATION_ID&status=6
    → "access" / "no_cancel" / "already_cancel"
  getAllSms:  ?api_key=xxx&action=getAllSms&activationId=ACTIVATION_ID
"""

from __future__ import annotations

import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlencode

import httpx


# SMS-Activate country codes
COUNTRY_CODES = {
    "japan": 22,
    "usa": 12,
    "united-states": 12,
    "thailand": 66,
}

# Default country preference order
DEFAULT_COUNTRY_ORDER = ["japan", "usa", "thailand"]

# ChatGPT/OpenAI service code
SERVICE_CODE = "go"

# API base URL
HERO_SMS_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

# Default max price in USD
MAX_PRICE_USD = 0.4

# Status codes
STATUS_WAIT_CODE = 1  # waiting for SMS
STATUS_READY = 2      # ready for receive
STATUS_RECEIVED = 3   # SMS received (with code)
STATUS_COMPLETE = 6   # complete
STATUS_CANCEL = 8     # cancel


@dataclass
class PhoneEntry:
    """与 SMSToMe PhoneEntry 兼容的数据结构"""
    phone: str
    country_slug: str
    activation_id: str
    detail_url: str = ""


@dataclass
class HeroSMSConfig:
    """HeroSMS 配置"""
    api_key: str = ""
    country_order: list[str] = field(default_factory=lambda: list(DEFAULT_COUNTRY_ORDER))
    max_price_usd: float = MAX_PRICE_USD
    max_attempts: int = 3
    otp_timeout_seconds: int = 120
    poll_interval_seconds: int = 5
    service_code: str = SERVICE_CODE


class HeroSMSPhoneService:
    """HeroSMS 手机号服务 — 兼容 SMSToMePhoneService 接口"""

    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)
        self._parsed_config = self._parse_config()
        self._http_client = self._build_http_client()
        # 状态追踪：activation_id -> reuse_count
        self._activation_reuse: dict[str, int] = {}
        # 排除的手机号前缀（每号注册3次后加入）
        self._excluded_phones: set[str] = set()

    def _parse_config(self) -> HeroSMSConfig:
        cfg = HeroSMSConfig()
        cfg.api_key = str(self.config.get("hero_sms_api_key", "") or "").strip()
        try:
            cfg.max_attempts = max(1, int(str(self.config.get("hero_sms_max_attempts", 3) or "3").strip()))
        except (ValueError, TypeError):
            cfg.max_attempts = 3
        try:
            cfg.otp_timeout_seconds = max(30, int(str(self.config.get("hero_sms_otp_timeout", 120) or "120").strip()))
        except (ValueError, TypeError):
            cfg.otp_timeout_seconds = 120
        try:
            cfg.poll_interval_seconds = max(2, int(str(self.config.get("hero_sms_poll_interval", 5) or "5").strip()))
        except (ValueError, TypeError):
            cfg.poll_interval_seconds = 5
        return cfg

    def _build_http_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(30.0, connect=15.0),
            trust_env=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            },
        )

    def _api_url(self, params: dict) -> str:
        params["api_key"] = self._parsed_config.api_key
        return f"{HERO_SMS_BASE_URL}?{urlencode(params)}"

    def _api_get(self, params: dict) -> str:
        url = self._api_url(params)
        try:
            resp = self._http_client.get(url)
            resp.raise_for_status()
            return resp.text.strip()
        except Exception as e:
            self.log_fn(f"[HeroSMS] API 请求失败: {e}")
            return ""

    @property
    def enabled(self) -> bool:
        return bool(self._parsed_config.api_key)

    @property
    def max_attempts(self) -> int:
        return self._parsed_config.max_attempts

    def prefix_hint(self, phone: str) -> str:
        value = str(phone or "").strip()
        return value[: min(len(value), 7)] if value else ""

    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneEntry]:
        """获取一个可用手机号"""
        if not self.enabled:
            self.log_fn("[HeroSMS] 未配置 api_key，无法获取手机号")
            return None

        # 检查余额
        balance_text = self._api_get({"action": "getBalance"})
        if not balance_text:
            self.log_fn("[HeroSMS] 获取余额失败")
            return None

        try:
            # Access format: "ACCESS.100.50" (or just number)
            balance_parts = balance_text.upper().split(".")
            balance_value = balance_parts[-1] if len(balance_parts) > 1 else balance_parts[0]
            balance = float(balance_value)
            self.log_fn(f"[HeroSMS] 当前余额: ${balance:.2f}")
            if balance < 0.1:
                self.log_fn("[HeroSMS] 余额不足，无法获取手机号")
                return None
        except (ValueError, IndexError):
            self.log_fn(f"[HeroSMS] 解析余额失败: {balance_text}")

        # 按国家顺序尝试获取手机号
        for country_name in DEFAULT_COUNTRY_ORDER:
            country_code = COUNTRY_CODES.get(country_name, 22)
            country_display = country_name.upper()

            self.log_fn(f"[HeroSMS] 尝试获取 {country_display} 手机号...")
            result = self._api_get({
                "action": "getNumber",
                "service": SERVICE_CODE,
                "country": country_code,
            })

            if not result:
                self.log_fn(f"[HeroSMS] {country_display} 无响应，跳过")
                continue

            result = result.strip()
            if result.lower().startswith("access"):
                # "ACCESS.ACTIVATION_ID.PHONE_NUMBER"
                parts = result.split(".")
                if len(parts) >= 3:
                    activation_id = parts[1]
                    phone = parts[2]
                    self._activation_reuse[activation_id] = 0
                    self.log_fn(
                        f"[HeroSMS] 获取到 {country_display} 手机号: +{phone} "
                        f"(activation_id={activation_id})"
                    )
                    return PhoneEntry(
                        phone=phone,
                        country_slug=country_name,
                        activation_id=activation_id,
                    )
            elif result.lower() == "no_numbers":
                self.log_fn(f"[HeroSMS] {country_display} 无可用号码")
                continue
            elif result.lower() == "no_balance":
                self.log_fn(f"[HeroSMS] {country_display} 余额不足")
                break
            else:
                self.log_fn(f"[HeroSMS] {country_display} 返回异常: {result}")
                continue

        self.log_fn("[HeroSMS] 所有国家均无法获取手机号")
        return None

    def mark_blacklisted(self, phone: str) -> None:
        """标记手机号为已使用/黑名单"""
        self._excluded_phones.add(str(phone or "").strip())
        self.log_fn(f"[HeroSMS] 手机号 {phone} 已标记为不可用")

    def wait_for_code(
        self, entry: PhoneEntry, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        """等待短信验证码"""
        activation_id = entry.activation_id
        wait_seconds = max(int(timeout or self._parsed_config.otp_timeout_seconds), 30)
        poll_interval = self._parsed_config.poll_interval_seconds
        deadline = time.monotonic() + wait_seconds

        self.log_fn(
            f"[HeroSMS] 等待验证码 (activation_id={activation_id}, "
            f"timeout={wait_seconds}s)"
        )

        while time.monotonic() < deadline:
            # 先将状态设为准备接收
            if int(time.monotonic() - (deadline - wait_seconds)) < 3:
                self._api_get({
                    "action": "setStatus",
                    "id": activation_id,
                    "status": STATUS_READY,
                })

            result = self._api_get({
                "action": "getStatus",
                "id": activation_id,
            })

            if not result:
                remaining = max(0, int(deadline - time.monotonic()))
                self.log_fn(f"[HeroSMS] 查询状态失败，剩余 {remaining}s")
                time.sleep(poll_interval)
                continue

            result = result.strip()

            # "status.3.CODE" — received with code
            if result.startswith("status.3"):
                parts = result.split(".")
                if len(parts) >= 3:
                    code = parts[2]
                    self.log_fn(f"[HeroSMS] 收到验证码: {code}")
                    # 标记完成
                    self._api_get({
                        "action": "setStatus",
                        "id": activation_id,
                        "status": STATUS_COMPLETE,
                    })
                    # 统计复用次数
                    self._activation_reuse[activation_id] = (
                        self._activation_reuse.get(activation_id, 0) + 1
                    )
                    return code

            # "status.1" — waiting
            # "status.2" — ready
            elif result.startswith("status.1") or result.startswith("status.2"):
                pass  # 继续等待

            # "status.4" — ready with code
            elif result.startswith("status.4"):
                parts = result.split(".")
                if len(parts) >= 3:
                    code = parts[2]
                    self.log_fn(f"[HeroSMS] 收到验证码 (status.4): {code}")
                    self._api_get({
                        "action": "setStatus",
                        "id": activation_id,
                        "status": STATUS_COMPLETE,
                    })
                    self._activation_reuse[activation_id] = (
                        self._activation_reuse.get(activation_id, 0) + 1
                    )
                    return code

            # "status.6" — complete
            # "status.8" — cancel
            elif result.startswith("status.6") or result.startswith("status.8"):
                self.log_fn(f"[HeroSMS] 激活已终止: {result}")
                return None

            remaining = max(0, int(deadline - time.monotonic()))
            if remaining > 0:
                time.sleep(poll_interval)

        # 超时，取消激活
        self.log_fn(f"[HeroSMS] 等待验证码超时 ({wait_seconds}s)，取消激活")
        self._api_get({
            "action": "setStatus",
            "id": activation_id,
            "status": STATUS_CANCEL,
        })
        return None

    def cancel_activation(self, entry: PhoneEntry) -> bool:
        """取消激活"""
        result = self._api_get({
            "action": "setStatus",
            "id": entry.activation_id,
            "status": STATUS_CANCEL,
        })
        cancelled = "cancel" in (result or "").lower()
        if cancelled:
            self.log_fn(f"[HeroSMS] 激活已取消 (id={entry.activation_id})")
        else:
            self.log_fn(f"[HeroSMS] 取消失败: {result}")
        return cancelled

    def close(self):
        """清理 HTTP 客户端"""
        try:
            self._http_client.close()
        except Exception:
            pass
