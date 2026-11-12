import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fp:
        return fp.read()


class SettingsConnectionCardTest(unittest.TestCase):
    def test_card_ids_present(self):
        html = read("static/settings.html")
        for dom_id in ("conn-card", "conn-mode-adb", "conn-mode-ssh",
                       "conn-panel-adb", "conn-panel-ssh", "conn-host",
                       "conn-port", "conn-user", "conn-key", "btn-conn-key",
                       "conn-policy", "conn-timeout", "conn-remount",
                       "conn-status", "btn-conn-test", "btn-conn-save",
                       "conn-running"):
            self.assertIn('id="%s"' % dom_id, html)

    def test_connection_js_loaded(self):
        self.assertIn("/static/connection.js", read("static/settings.html"))


class SidebarConnectionTest(unittest.TestCase):
    def test_sidebar_has_connection_row(self):
        js = read("static/shell.js")
        self.assertIn("'连接'", js)
        self.assertIn("connectionInfo", js)
        self.assertIn("connection-text", js)
        self.assertIn("/settings#conn-card", js)

    def test_ssh_device_branch(self):
        js = read("static/shell.js")
        self.assertIn("SSH 在线", js)
        self.assertIn("连不上", js)

    def test_sidebar_quick_toggle(self):
        js = read("static/shell.js")
        self.assertIn("/api/connection", js)
        self.assertIn("toggleConnection", js)

    def test_toggle_opens_choice_menu(self):
        js = read("static/shell.js")
        # 点击连接行弹菜单让用户选，而不是直接切到另一种模式
        self.assertIn("openToggleMenu", js)
        self.assertIn("'role', 'menu'", js)
        self.assertIn("menuitemradio", js)
        self.assertIn("aria-expanded", js)
        self.assertIn("aria-checked", js)
        self.assertIn("ArrowDown", js)
        self.assertIn("Escape", js)
        self.assertIn("conn-menu", js)
        css = read("static/style.css")
        self.assertIn(".conn-menu", css)


class HiddenAttributeTest(unittest.TestCase):
    def test_hidden_beats_component_display(self):
        # .field { display:flex } 会盖掉浏览器对 [hidden] 的默认 display:none，
        # 结果 SSH 模式下「adb 路径」还露着、ADB 模式下「SSH 目标」也一直显示。
        css = read("static/style.css")
        self.assertIn("[hidden] { display: none !important; }", css)


class ToolPagesTest(unittest.TestCase):
    def test_pages_have_mode_blocks(self):
        for page in ("scripts.html", "replace-image.html", "push-file.html"):
            html = read("static/" + page)
            self.assertIn('id="adb-path-field"', html)
            self.assertIn('id="device-field"', html)
            self.assertIn('id="ssh-target-field"', html)
            self.assertIn("/static/connection.js", html)

    def test_tool_js_skips_serial_in_ssh(self):
        for js in ("scripts.js", "replace-image.js", "push-file.js"):
            text = read("static/" + js)
            self.assertIn("CONNECTION", text)
            self.assertIn("isSsh", text)


class TunnelPageTest(unittest.TestCase):
    def test_title_and_targets(self):
        html = read("static/ssh-tunnel.html")
        self.assertIn("<h1>反向隧道</h1>", html)
        js = read("static/ssh-tunnel.js")
        self.assertIn("targets", js)
        self.assertIn("CONNECTION", js)


class AlwaysAdbAndHelpTest(unittest.TestCase):
    def test_notice_and_guard(self):
        html = read("static/always-adb.html")
        self.assertIn('id="adb-only-notice"', html)
        self.assertIn("本功能仅支持 ADB", html)
        js = read("static/always-adb.js")
        self.assertIn("CONNECTION", js)
        self.assertIn("adbOnly", js)

    def test_help_has_ssh_section(self):
        html = read("static/help.html")
        self.assertIn('id="ssh"', html)
        self.assertIn("BatchMode", html)
        self.assertIn("loopback", html)
