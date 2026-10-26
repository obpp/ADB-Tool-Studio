# -*- coding: utf-8 -*-
"""ADB Tool Studio 的功能模块。

每个模块导出一个 Tool 子类，在 server.py 的 TOOLS 列表里登记即可出现在一级页面。
"""

from tools.always_adb import AlwaysAdbTool
from tools.push_file import PushFileTool
from tools.replace_image import ReplaceImageTool
from tools.script_runner import ScriptRunnerTool
from tools.ssh_tunnel import SshTunnelTool

# 侧边栏按这个顺序展示
TOOL_CLASSES = (AlwaysAdbTool, SshTunnelTool, ScriptRunnerTool, ReplaceImageTool,
                PushFileTool)

__all__ = ["AlwaysAdbTool", "SshTunnelTool", "ScriptRunnerTool",
           "ReplaceImageTool", "PushFileTool", "TOOL_CLASSES"]
