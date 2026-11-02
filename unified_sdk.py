"""Публичный интерфейс плагинов FunPay BOT; версия контракта API_VERSION = 1."""

from decimal import Decimal

from funpay_bot.commerce import CommercePlugin, CommerceRuntime
from funpay_bot.framework import (
    BoundComponent,
    CallbackRouter,
    FrameworkPlugin,
    PluginConfigStore,
    PluginContext,
    PluginRuntime,
    ProductAdapter,
    ProviderAdapter,
    SettingsSchema,
    plugin_capability,
)
from funpay_bot.lots import LotProfile, ManagedLot
from funpay_bot.metadata import VERSION
from funpay_bot.primitives import (
    ActivityGeneration,
    RetryPolicy,
    SerialTaskQueue,
    TimerRegistry,
)

__all__ = [
    "Decimal",
    "VERSION",
    "FrameworkPlugin",
    "PluginRuntime",
    "PluginConfigStore",
    "CallbackRouter",
    "ProviderAdapter",
    "ProductAdapter",
    "SettingsSchema",
    "BoundComponent",
    "PluginContext",
    "plugin_capability",
    "CommercePlugin",
    "CommerceRuntime",
    "LotProfile",
    "ManagedLot",
    "ActivityGeneration",
    "TimerRegistry",
    "SerialTaskQueue",
    "RetryPolicy",
]
