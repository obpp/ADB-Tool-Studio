# -*- coding: utf-8 -*-
"""my-adb-app 的功能模块。

每个模块导出一个 Tool 子类，在 server.py 的 TOOLS 列表里登记即可出现在一级页面。
"""

from tools.always_adb import AlwaysAdbTool
from tools.replace_image import ReplaceImageTool
from tools.script_runner import ScriptRunnerTool
from tools.ssh_tunnel import SshTunnelTool

# 侧边栏按这个顺序展示
TOOL_CLASSES = (AlwaysAdbTool, SshTunnelTool, ScriptRunnerTool, ReplaceImageTool)

__all__ = ["AlwaysAdbTool", "SshTunnelTool", "ScriptRunnerTool",
           "ReplaceImageTool", "TOOL_CLASSES"]
