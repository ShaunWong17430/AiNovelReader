"""gui — PyQt6 桌面界面（小说阅读自动化）。

职责（薄界面层，业务全部走 core\\*）：
- 小说路径选择（不依赖传入小说名：选 ROOT 目录 → 推导 base + novel）；
- reader / summarizer 双套配置（url + 模型名 + api key，彻底分离）；
- 初始化 / 开始 / 停止（等价 Ctrl+C 优雅中断）；
- 元数据观察窗（中文化）+ 实时日志。
"""