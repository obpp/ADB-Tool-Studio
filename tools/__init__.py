# -*- coding: utf-8 -*-
"""my-adb-app 的功能模块。

每个模块导出一个 Tool 子类，在 server.py 的 TOOLS 列表里登记即可出现在一级页面。
"""

from tools.always_adb import AlwaysAdbTool
from tools.ssh_tunnel import SshTunnelTool

# 一级页面按这个顺序展示
TOOL_CLASSES = (AlwaysAdbTool, SshTunnelTool)

__all__ = ["AlwaysAdbTool", "SshTunnelTool", "TOOL_CLASSES"]
