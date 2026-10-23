"""路由蓝图：全部路由都注册在名为 ``main`` 的蓝图里。

保持 ``bp`` 名字为 ``main`` 是为了让模板里现有的 ``url_for('main.xxx')`` 全部不变。
各领域实现在同目录下的独立模块里，import 本包时自动注册路由。
"""
from __future__ import annotations

from flask import Blueprint

bp = Blueprint("main", __name__)

# 导入即注册路由（顺序无关）
from . import core, dashboard, users, sms_admin, exports, projects, transactions, lifecycle, dividend_routes, finance  # noqa: E402,F401

__all__ = ["bp"]
