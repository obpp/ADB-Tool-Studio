# -*- coding: utf-8 -*-
"""桌面外壳（Windows 上的原生窗口 + 托盘 + 打包配置）。

这一层只负责「怎么把服务开起来、怎么让人看见它」，
网页版的 server.py / toolbase.py / tools/ / static/ 都不属于这里，
也不该反过来 import 这个包 —— 保持网页版零第三方依赖、能单独跑。

* app.py            启动器：内嵌服务 + 原生窗口 + 托盘，是整个桌面版的入口
* tray.py           托盘图标与菜单
* autostart.py      开机自启（HKCU 的 Run 键）
* singleinstance.py 只允许跑一个实例
* make_icon.py      用标准库生成 icon.ico（不依赖 Pillow，方便重新生成）
"""
