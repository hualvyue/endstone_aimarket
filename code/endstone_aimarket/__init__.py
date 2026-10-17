"""aimarket —— 一体化经济内核与按钮商店。

自建经济系统（账户 / 复式账本 / 交易税），不依赖也不适配任何第三方经济插件；
运行期只用 Python 标准库，唯一的外部依赖是 Endstone 本身。
"""
from .aimarket import AimarketPlugin

__all__ = ["AimarketPlugin"]
