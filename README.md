# 小说阅读自动化系统 · README

> 依据需求规格 `REQUIREMENTS_v3.2.1.md`（唯一实现依据）实现；本手册面向**使用者与维护者**，
> 零基础用户请另见 **《使用说明_小白版.md》**。

## 0. 这是什么

一个「AI 读书」工具：把一本小说的分片（`chunks\part_001.txt …`）交给大模型逐片阅读，
自动产出：

- **情节时间线** `plot_timeline.md`（完整事件记录，唯一可信的时间线）
- **读书笔记** `notes\part_XXX~part_YYY.md`（每批一份，五段式固定排版）
- **滚动摘要** `summary.md`（单段 ≤ `summary_max` 字，**有损压缩**，见 §7 告诫）
- 全程审计日志 `run.log` / `llm.log`（NDJSON，脱敏）

主程序常驻自动循环：读片 → 问 AI → 记时间线 → 写笔记 → 核对 → 推进度 →
隔窗压缩小结 → 全书读完自动标记 `done`。**断电/断网/崩溃均可从断点续跑**。

**两种操作方式，同一套引擎**：

| 方式 | 适用 | 入口 |
|---|---|---|
| 🖥️ **图形界面 GUI** | 日常使用（推荐） | `python gui_launcher.py` |
| ⌨️ **命令行 CLI** | 维护 / 调试 / 脚本化 | `python run.py …` + `cli\*.py` |

GUI 与 CLI 共用同一套 core 逻辑、同一把锁、同一份 `config.json`——**绝不同时开两个
进程操作同一本书**（GUI 内点「停止」= 命令行 Ctrl+C 语义）。

---

## 1. 环境与安装

| 项 | 要求 |
|---|---|
| 操作系统 | Windows（GUI 的密钥加密依赖 Windows DPAPI） |
| Python | 3.10（本项目使用 `D:\data\dsh_wz1\pyfordsh\python.exe`） |

**依赖包**（按用途分三类，缺哪类装哪类）：

| 包 | 必需性 | 用途 |
|---|---|---|
| `openai` | **必需**（核心） | 调用 OpenAI 兼容接口（本地 llama-server / 远程服务商） |
| `PyQt6` | **必需（GUI）** | 图形界面（`gui_launcher.py` 依赖；只用命令行可不安） |
| `pytest` | 测试 | 跑回归测试（§11） |
| `tiktoken` | 可选 | token 计数，缺失自动降级 |

安装（全部）：

```bat
D:\data\dsh_wz1\pyfordsh\python.exe -m pip install openai PyQt6 pytest tiktoken
```

只跑命令行、不要界面：

```bat
D:\data\dsh_wz1\pyfordsh\python.exe -m pip install openai pytest
```

**调用约定（铁律）**：一律用 `D:\data\dsh_wz1\pyfordsh\python.exe`（禁裸 `python`）；
工作目录固定为 `D:\data\dsh_wz1\novel_skill`（下称 `CODE_ROOT`）；所有命令在
`CODE_ROOT` 下执行。

---

## 2. 快速开始

### 2.1 准备分片（GUI 与 CLI 通用）

在 `BASE = D:\data\dsh_wz1\novel_reading` 下建 `示例书名\chunks\`，把小说切成
**UTF-8 无 BOM、LF 换行**的纯文本分片，命名 `part_001.txt`、`part_002.txt` …
（零填充位数全书一致，如 3 位；单文件去除空白后 ≤ 30000 字，K28 上限）。

> CLI 方式下书名目录名即 `--novel` 参数；GUI 方式直接「浏览」选到该目录，无需手输书名。

### 2.2 方式 A：图形界面（推荐日常使用）

```bat
cd /d D:\data\dsh_wz1\novel_skill
D:\data\dsh_wz1\pyfordsh\python.exe gui_launcher.py
```

弹出「小说阅读自动化 · 控制台」窗口，按 §4 的界面说明操作：
选路径 → 填双套配置 → 保存配置 → 初始化 → 开始。**API Key 输入后经 DPAPI 加密
落盘（`gui\.secrets.json`），`config.json` 永不存明文**（详见 §5.3）。

### 2.3 方式 B：命令行

配置 API Key（二选一）：

```bat
set NOVEL_LLM_API_KEY=sk-xxxx
```

或写入 `CODE_ROOT\config.json` 的 `llm.api_key`（支持 `${环境变量名}` 形式展开）。
**绝不把 key 写进任何日志/快照**；缺失 key 启动即报错（退出码 2），不发空 key 请求。

运行：

```bat
:: 初始化（检查分片合法性，生成骨架）
D:\data\dsh_wz1\pyfordsh\python.exe run.py init --novel 示例书名

:: 自动读完整本（可随时 Ctrl+C 中断，重启续跑）
D:\data\dsh_wz1\pyfordsh\python.exe run.py run --novel 示例书名

:: 只看进度
D:\data\dsh_wz1\pyfordsh\python.exe run.py status --novel 示例书名

:: 配置体检
D:\data\dsh_wz1\pyfordsh\python.exe run.py validate
```

> 首次直接 `run` 也可以：metadata 缺失时**自动按默认参数 init**（K31/K42），无需手动 init。

### 2.4 预演（不花一分钱）

```bat
D:\data\dsh_wz1\pyfordsh\python.exe run.py run --novel 示例书名 --dry-run
```

打印本批范围/待调片数/小结是否触发/预计退出码，**不写任何文件、不调 AI**。

---

## 3. 目录结构

```
CODE_ROOT = D:\data\dsh_wz1\novel_skill
├── config.json            # LLM 与运行参数（§5）
├── run.py                 # CLI 主入口（run/init/status/validate）
├── gui_launcher.py        # GUI 入口（PyQt6）
├── core\                  # 业务逻辑（唯一实现源，勿改）
├── cli\                   # 12 个人工调试命令（见 §6）
├── gui\                   # GUI 界面层（QThread 调 core，不重复业务逻辑）
│   ├── main_window.py     #   主窗口（路径/双套配置/元数据窗/日志）
│   ├── worker.py          #   RunWorker：QThread 封装 init/run + 停止
│   ├── config_io.py       #   config.json 读写（url/model/api_key_env）
│   ├── secrets.py         #   Windows DPAPI 加解密（gui\.secrets.json）
│   └── .secrets.json      #   API Key 密文（生成后出现，已 gitignore）
├── prompts\               # AI 模板（版本锁定，勿改）
└── tests\                 # 223 项测试（backend=fake 全离线）

BASE = D:\data\dsh_wz1\novel_reading
├── .示例书名.run.lock     # 单实例锁（BASE 级）
└── 示例书名\              # ROOT
    ├── metadata.json      # 任务状态唯一真源
    ├── run.log llm.log config.snapshot.json
    ├── plot_timeline.md   # 情节时间线
    ├── summary.md         # 滚动摘要（单段有损）
    ├── summary_draft.txt  # 小结草稿（消费后删除）
    ├── .summary_applied   # 小结已应用标记（内容=processed 值）
    ├── .batch\part_XXX.json    # 本批片级 AI 产物（临时）
    ├── notes\part_XXX~part_YYY.md
    ├── .rollback\batch_<id>_<phase>_*.md   # 事务快照（done 后自动清理）
    ├── metadata.corrupt_<ts>   # emergency 改名保留现场（异常时）
    └── chunks\part_001.txt …   # 用户预分片（只读）
```

---

## 4. GUI 使用说明

### 4.1 界面总览

启动 `gui_launcher.py` 后窗口分四区：

| 区域 | 内容 |
|---|---|
| ① 小说路径 | 路径输入框 + 「浏览…」；**自动推导 base 与小说名，无需手输小说名** |
| ② 双套配置 | 左「阅读模型（Reader）」/ 右「小结模型（Summarizer）」，各含 URL/模型名/API Key |
| ③ 按钮行 | 保存配置 / 初始化 / 开始 / 停止 |
| ④ 下方分栏 | 左：元数据观察窗（每 2 秒刷新）；右：日志窗（按级别着色，可清空） |

### 4.2 选择小说路径（不再依赖传入小说名）

点「浏览…」，两种选法都可以：

- **选小说根目录**（含 `chunks\` 或 `metadata.json`）→ 自动识别 base=上级目录、
  小说名=当前目录名；路径旁显示 `base: … · 小说: …`；
- **选小说库根目录**（`novel_reading` 本身）→ 提示「请在库内选择小说子目录」，
  需再选到具体小说目录。

选定小说后，`NOVEL_BASE` 环境变量自动指向该 base，core 全程按此读取，
不再绑定写死的 BASE。

### 4.3 双套配置（reader / summarizer 彻底分离）

两套配置各自独立，互不共用：

| 字段 | 说明 |
|---|---|
| 接口 URL | 各自模型的 OpenAI 兼容端点（如本地 `http://127.0.0.1:1235/v1` 或远程服务商） |
| 模型名称 | 各自模型名（如 `Qwen` / `deepseek-v4-flash-0731`） |
| API Key | 密码框输入（点「显示」可明文查看）；**留空 = 沿用已保存的密钥** |

保存后：URL/模型名写 `config.json` 的 `llm.profiles.{reader|summarizer}`；
API Key **只进 DPAPI 密文**（`gui\.secrets.json`），`config.json` 仅记录
`api_key_env`（`NOVEL_LLM_READER_KEY` / `NOVEL_LLM_SUMMARIZER_KEY`）。
运行时由 GUI 解密注入进程环境变量，core 按 phase 取各自 key，绝不分发日志。

### 4.4 按钮流程

1. **保存配置**：把当前双套 URL/模型/Key 落盘（key 加密）；
2. **初始化**：扫描 `chunks\` 合法性，生成 `metadata.json` 与骨架（等价
   `run.py init`）；
3. **开始**：常驻自动阅读循环（等价 `run.py run`），直到读完标记 done；
4. **停止**：**等价 Ctrl+C**——置 stop_flag，当前片完成后优雅退出；再点「开始」
   从断点续跑（GUI 关闭窗口也会先请求停止再退出）。

### 4.5 元数据观察窗与日志

- 元数据窗 2 秒刷新一次 `metadata.json`，**24 项全中文化**：状态（待初始化/
  运行中/已完成/出错，出错红字/完成绿字）、总片数、已读片数、批大小、小结上限、
  单片上限字数、tokens 统计、错误信息等；
- 日志窗镜像 core 的 stdout 输出（`Logger echo=True`），按 OK/WARN/SKIP/ERROR 着色；
  最多保留 5000 行，可点「清空」。

---

## 5. 配置说明（config.json）

### 5.1 当前配置全貌（真实示例）

> 下面是本项目**当前正在使用的 `config.json`**，可直接对照你的文件：
> reader 走本地 Qwen（免费/内网），summarizer 走远程 deepseek 兼容端点——
> 两套 URL、模型、Key 完全独立，互不影响。

```json
{ "llm": {
    "backend": "openai",                          // openai | fake（测试）
    "base_url": "http://127.0.0.1:1235/v1",       // 顶层兜底 URL（profile 缺省时用）
    "api_key_env": "NOVEL_LLM_API_KEY",           // 顶层兜底 key 环境变量名
    "api_key": "not-needed",                      // 顶层兜底（本地服务不校验；远程须换成真 key）
    "models": { "reader": "Qwen", "summarizer": "deepseek-v4-flash-0731" },
    "profiles": {                                 // ★ 双套分离（GUI 保存的落点）
      "reader": {
        "api_key_env": "NOVEL_LLM_READER_KEY",    //   reader 独立 key 环境变量名
        "base_url":    "http://127.0.0.1:1235/v1",//   reader 独立端点（本地）
        "model":       "Qwen" },                  //   reader 独立模型
      "summarizer": {
        "api_key_env": "NOVEL_LLM_SUMMARIZER_KEY",//   summarizer 独立 key 环境变量名
        "base_url":    "https://tbtk.asia/v1",    //   summarizer 独立端点（远程）
        "model":       "deepseek-v4-flash-0731" } //   summarizer 独立模型
    },
    "timeout_s": 300,                             // 单次请求超时（秒）
    "max_retries": 3,                             // 网络/协议重试次数（阶段 A 预算）
    "timeout_fatal_threshold": 3,                 // 连续超时达此数 → FATAL
    "retry_backoff_s": [2, 6, 18],                // 退避秒数，长度 == max_retries
    "retry_on_status": [408, 409, 429, 500, 502, 503, 504],
    "temperature": { "reader": 0.3, "summarizer": 0.3 },
    "max_output_tokens": { "reader": 32768, "summarizer": 32768 },
    "response_format": "json_object" },           // json_object | text
  "run": { "sanitize_output": true, "budget_prompt_tokens": null, "log_level": "INFO" } }
```

### 5.2 双套分离语义（`llm.profiles`）

- 每个 phase（reader / summarizer）可独立指定 `base_url` + `api_key_env` + `model`；
- 优先取 profile 值；**profile 缺失的字段回退顶层**（`base_url` / `api_key_env` /
  `api_key` / `models[phase]`），**旧版 config（无 profiles）行为完全不变**；
- 即使两套 URL 相同，core 也会为每个 phase **各自创建独立的 OpenAI client**
  （连接/凭据互不共享）；
- API Key 解析链：`profiles[phase].api_key_env` 指向的环境变量 → （${ENV} 展开）→
  顶层 `api_key`；无 phase 时逐个 phase 校验，缺失即报错（消息含 phase 名）。

**典型用法（正是当前配置）**：reader 用本地 llama-server（Qwen，免费、无 Key 校验），
summarizer 用远程兼容端点（deepseek，需真 Key）——各填各的，互不干扰。

### 5.3 Key 存哪（GUI vs CLI）

| 方式 | key 落点 | 形态 |
|---|---|---|
| GUI | `gui\.secrets.json` | **DPAPI 密文**（当前用户级，hex），config.json 无明文 |
| CLI | 环境变量 / config.json `api_key` | 明文或 `${ENV}`；不进日志/快照 |

> 注意：若你的 summarizer 走**远程服务**，必须在 GUI 的 Summarizer 栏填入真 Key
> （或设置 `NOVEL_LLM_SUMMARIZER_KEY` 环境变量）；本地 Qwen 填 `not-needed` 即可。

### 5.4 可调项与 init 运行参数

| 可调项 | 说明 |
|---|---|
| `profiles.reader / profiles.summarizer`（或顶层 models） | 两阶段模型与端点（可不同，如 reader 用本地便宜模型、summarizer 用远程强模型） |
| `max_output_tokens.summarizer` | 小结输出上限；**init 硬校验 `summary_max ≤ 其 × 0.6`（K28）** |
| `retry_backoff_s` / `max_retries` | 网络失败重试节奏；内容类失败另有固定 1 次重发（阶段 B，K16） |
| `timeout_s` / `timeout_fatal_threshold` | 单请求超时；连续超时阈值（远程端点建议调大 timeout_s） |

`run.py init --novel 名 [--batch-size N] [--timeline-window N] [--summary-max N]
[--chunk-max-chars N] [--summary-chunks N] [--current-batch N]`

| 参数 | 默认 | 范围 | 含义 |
|---|---|---|---|
| `--batch-size` | 5 | 1–20 | 每批片数（事务/回滚单元） |
| `--timeline-window` | 10 | 1–50 | 相隔多少片触发一次小结（片数） |
| `--summary-max` | 5000 | 1000–20000 | 滚动摘要字数上限 |
| `--chunk-max-chars` | 20000 | 1000–30000 | 单片字数上限（超限该片 FATAL，K29/K28） |

> 运行期 `run` 不读命令行参数，四个值固化在每本书的 `metadata.json`（init 时写入，
> 另记录于 `config.snapshot.json`）。**想改参数：见 §10 FAQ Q4**（`init --recover` 仅用于
> metadata 缺失时重建，不是改参数入口）。

---

## 6. 命令参考

### GUI

| 命令 | 说明 |
|---|---|
| `python gui_launcher.py` | 启动图形界面（等价 GUI 内 init/run/stop 的引擎入口） |

### run.py（主入口，直接 import core，不子进程调 cli）

| 命令 | 说明 | 加锁 |
|---|---|---|
| `run.py run --novel N [--once] [--max-rounds M] [--dry-run]` | 常驻自动循环 | ✅ |
| `run.py init --novel N [--recover] [参数]` | 初始化 / 从时间线恢复 | ✅ |
| `run.py status --novel N` | 查看状态 | ❌ |
| `run.py validate` | 配置体检（含 key） | ❌ |

### cli\（人工调试/验收；与 run.py 共用同一把锁）

| 命令 | 作用 |
|---|---|
| `cli/init_novel.py` | 同 run.py init |
| `cli/backup.py --novel N --batch <id> --phase read\|summarize [--force]` | 事务快照（K23 三态） |
| `cli/append_timeline.py --novel N [--batch <id>]` | 本批时间线原子追加 |
| `cli/verify_timeline.py --novel N [--check-only] [--batch <id>]` | 时间线校验 / 预检三态 |
| `cli/tail_timeline.py --novel N (--chunk-start M --chunk-end K \| --lines L)` | 时间线窗口截取 |
| `cli/chunk_stats.py --novel N` | 各片字数（§1.6 口径） |
| `cli/render_notes.py --novel N --start A --end B` | 渲染读书笔记 |
| `cli/verify_notes.py --novel N --start A --end B` | 笔记验收 |
| `cli/write_summary.py --novel N --processed P [--max M]` | 草稿 → summary.md |
| `cli/verify_summary.py --novel N` | 小结验收 |
| `cli/update_metadata.py --novel N [--processed P] [--summary S] [--status X] [--retries R] …` | 人工推进状态 |
| `cli/rollback.py --novel N --phase read\|summarize [--purge]` | 从快照恢复 / 清理快照 |

**退出码**：0 成功 · 2 参数/环境/配置 · 3 数据校验/状态冲突/LLM 耗尽 · 4 分片编码探测（仅 init）。

---

## 7. ⚠️ 重要告诫（必读）

1. **锁（K25）**：`run`/`init` 与全部 cli **写命令**（backup/append/render_notes/
   write_summary/update_metadata/rollback）使用同一把 BASE 级锁，**互斥运行**——
   **绝不在运行中手动执行这些 cli 写命令，也绝不同时开 GUI 与命令行操作同一本书**；
   `status`/`validate`/`--dry-run`/GUI 元数据观察窗不加锁可随时运行。锁残留（进程被
   杀）下次运行自动识别接管并 WARN；**自动流程从不带 `--force`**，确需人工强开才用
   （锁与 backup 两处独立旗标）。
2. **`summary.md` 是有损滚动摘要（P2-15）**：每 `timeline_window+1` 片把前情压缩到
   ≤ `summary_max` 字，**早期细节永久丢失**；`plot_timeline.md` 才是完整记录。
3. **`--log-payload` 版权提示**：`llm.log` 只记请求指纹（req_fp 哈希），**绝不记录
   完整 prompt/响应**；调试用 `--log-payload`（默认关，DEBUG 级）会将**含版权的小
   说正文**写入 `llm_payload.log`——该文件**严禁外传**。
4. **原子写与单写入口**：metadata 只经 `core/state.py`；时间线/小结/笔记一律
   「临时文件 + `os.replace`」原子写。**不要手动编辑 metadata.json / plot_timeline.md /
   summary.md / .batch / .rollback**——手动改动会被校验识破并停下报告（K40/G13）。
   **唯一例外：改运行参数**，按 §10 FAQ Q4 的安全步骤。
5. `chunks\` 只读：分片集合（数量/最大序号/零填充）运行期变化会被检测（G13）→ 报错。
6. **GUI 关闭窗口 = 请求停止**：点窗口 × 会先 stop_flag 再退出（最多等 8 秒），
   属正常优雅退出，重启 GUI 后从断点续跑即可。
7. **远程端点与 Key**：summarizer 走远程服务时必须配真 Key；远程端点建议调大
   `timeout_s`（300 起步）以容纳网络抖动，连续超时 3 次会 FATAL（K8）。

---

## 8. 成本估算与模型选型（§7.2 口径）

单片输入 ≈ `summary`（≤ summary_max 字）＋ 前情窗口（最近 `timeline_window` **片**、
行数上限 `timeline_window×5`）＋ **正文（≤ chunk_max_chars=30000 字）**。中文按
**1 字 ≈ 1 token** 粗估：

| 场景 | 单片输入 token（约） | 说明 |
|---|---|---|
| 默认（summary_max=5000、window=10） | ≈ 35k–40k | 30000 正文 + 5000 摘要 + 窗口 |
| `chunk_max_chars=30000` 上限 | ≈ 40k–45k | 最坏情况 |

**选型提示**：建议选用**上下文 ≥ 64k token** 的模型（如 deepseek 系、gpt-4o-mini
128k 均可胜任 30000 字片）；小上下文模型遇大片会触发 HTTP 413 →
`PAYLOAD_TOO_LARGE`（K38，FATAL 快速失败）——**按模型容量下调
`--chunk-max-chars`** 即可避免。

**当前双套的成本形态**（读者常驻、小结低频）：

- **Reader（读正文，调用最频繁）**：本地 Qwen（llama-server）→ **免费**，不受量约束；
- **Summarizer（写小结，每 `timeline_window+1` 片一次，§7 告诫 2）**：远程 deepseek 按 token 计费，
  但调用次数少——一本 12 片书仅约 1–2 次小结调用，花费极小。

> 通用参考：按 gpt-4o-mini 定价（输入 $0.15/1M、输出 $0.6/1M），一本 12 片书约
> 12×40k 输入 + 小结 ≈ **$0.1 左右**（数千字规模的书约 0.1–0.3 美元量级）。实际以
> 所用模型定价为准。

---

## 9. 错误恢复速查表（§10.1 A–D）

| 现象 | 处置 |
|---|---|
| **A. metadata 损坏**（出现 `metadata.corrupt_<ts>`，status=error，进度字段 -1） | ① 核对现场（corrupt 文件/.rollback/时间线/笔记）② 二选一：手工重建 metadata 后 `--status running`；或删 metadata 后 `init --recover`（时间线有数据禁普通 init，K37）③ `--status running` 自动清空 error/计数 ④ 若 summary.md 超新上限仅警告 |
| **B. 时间线被外部破坏**（预检 PARTIAL/解析失败） | ① 人工核对 PARTIAL 清单 ② 快照可信 → `rollback --phase read`；整体损坏 → 从最近快照/手工重建（须满足 §9.3）③ `--status running` 续跑 ④ 恢复不完整会再次 PARTIAL→再次 error，不静默越过 |
| **C. 小结两次失败**（summary_retries=2，status=error） | 均以 `--status running` 重置为首步：a) 仅收尾（processed==total）→ 压缩草稿后手动 `write_summary --processed` 落盘 → 续跑补推进；b) 未压缩 → 让驱动器重调（草稿会被覆写） |
| **D. 回滚失败**（K24：`rollback` 非零 → 文件系统半状态，无自愈） | ① 停止驱动 ② 人工核对 `.rollback\batch_<id>_<phase>_*` 快照与 timeline/summary 实际内容 ③ 快照完整 → 重新 `rollback --phase <phase>` 覆盖半状态；快照缺失/损坏 → 从其余快照或手工重建 ④ 确认一致后 `--status running` 续跑。**禁止自动重试 rollback** |
| **E. 批/小结 LLM 故障置 error**（最常见：`run.log` 尾部 `[reader] [llm] ERROR`，如 TIMEOUT 连击/网络断连 → 批重试 2 次仍失败 → K8 置 error） | ① 看 `run.log` 尾部 `[driver] [error]` 行与 `metadata.json.error` 定位原因（`run.py status --novel 名` 快速查看）② LLM 暂时不可用（超时/断连/服务重启）→ 直接 `cli/update_metadata.py --novel 名 --status running` 重置（自动清空 error/双重试，D15/G1）→ 重跑 `run.py run` 或 GUI「开始」续跑（未提交的 .batch 自动重调，不回滚已完成内容）③ 若反复同片失败 → 先修 LLM 侧（timeout_s/重试护栏/重启 llama-server）再重置，避免重置后立刻再 error |

> 通用：任何「status=error」都先看 `run.log` 尾部的 `[driver] [error]` 行与
> `metadata.json` 的 `error` 字段定位原因；CLI 用 `run.py status --novel 名`，
> **GUI 直接在元数据观察窗看「状态」与「错误信息」两行**（红字即 error）。

---

## 10. 常见问题（FAQ）

### Q1. 一次默认读几片？
默认 `batch_size=5`（K42；init 固化，范围 1–20）。主循环每轮读 5 片 → 时间线追加
5 行 → 小结一次，循环直到读完（收尾不足 5 片按实际剩余读）。

**补充：5 片不是打包一次发**。每一片都是**一次独立的 LLM 请求**（一片一次调用），
请求内容 = 前情摘要 ＋ 最近 `timeline_window` **片**的事件行（起 `max(1, processed−window+1)`、
止 `processed`，K33；**批内不推进**，见 §13）＋ **本批已读前片事件**（K43 批内接力，
批内第 2..N 片才有）＋ 当前这 1 片正文，返回这 1 片
的事件行。5 次调用全部成功后，5 行事件才**一起**写进时间线（append）、一起渲染笔记、
一起验证、一起提交（commit）——所以 `batch_size` 是**事务单位**（一起存、一起退），
**不是**「一次请求发 5 片」。中途任一片失败 → 整批回滚、5 片重读（K22/K24）。

**代价与解决（批内接力，K43 / 需求书 v3.2.2 §15）**：v3.2.1 时期批内第 2..N 片
**看不到本批前面片的剧情**——每次请求的快照是「批开始前」的：前情摘要只覆盖到上次
小结，时间线只到批开始前（默认参数下读 11–15 片时摘要甚至还是空的——第一次小结要
到 processed=15 之后才触发），第 15 片只见「摘要 + 1–10 片时间线行 + 第 15 片正文」，
11–14 的剧情对它不可见。**K43 已解决**：批内第 N 片请求时注入「本批已读前片事件」
段（读 `.batch\` 中已成功前片的 events），第 3 片能看到第 1、2 片事件行，第 5 片
能看到前 4 片；失败重跑时前片不重调（K22）、注入内容不变 → 重读依然可复现。
剩余边界：注入的是事件行，不含前片 notes 细节；摘要仍只到上次小结。详见 §13。

### Q2. `timeline_window` 默认多少？每次阅读喂给模型的时间线是多少？
默认 `timeline_window=10`（范围 1–50）。**量尺是「片」不是「行」**：读每片时，取
**上次提交进度往前数 `timeline_window` 片**的事件行（起 `max(1, processed−window+1)`、
止 `processed`，K33 钳制），行数上限 `timeline_window×5`（K20，超限整片丢弃、保最新）。
一片通常 3–5 行，所以默认参数下窗口一般是 30–50 行；时间线还没满就把现有的行全给
（开头几片依次是 5 行、10 行…直到满窗）。

**注意「止于 processed」= 止于批开始前**：同一批的 3–5 片共用这一份快照，批内不推进
（§13）；批内第 2..N 片的即时前情由 **K43 批内接力**从 `.batch\` 注入，见
`run.log` 的 `[window]` 行——`scope=pre-batch` 是时间线窗口，`inj=Np/Mr` 是本批接力
注入的片数/行数：

```
[reader] [window] [batch=30] [SKIP] scope=pre-batch tail=[82,87] rows=26 cap=30 inj=1p/4r
```

读第 89 片时 `tail` 仍止于 87（批内未提交），但 `inj=1p/4r` 说明第 88 片的 4 条事件
已经注入本片 prompt——**这不是窗口没跟上，两段合起来才是本片看到的全部前情**。

### Q3. 总结时提供时间线多少行？
**不是固定行数**：提供**上次小结之后新写出的全部行**（切片
`[summary_chunks+1, processed_chunks]`，正常就是最近 5–10 行上下）。两个兜底：
① 切片为空 → 回退读最后 `window+5=15` 行（K33）；② 切片超过
`(window+batch)×5=75` 行 → 报错停止（C7，防止参数配得离谱）。

### Q4. 暂停后可以手动改 metadata.json 吗（改运行参数）？
**可以，但只允许改四个运行参数**：`batch_size` / `timeline_window` / `summary_max` /
`chunk_max_chars`。这是改参数**唯一途径**——`cli/update_metadata.py` 只支持进度/状态/
重试字段，`run.py init` 遇已存在 metadata 直接拒绝（I4），`init --recover` 仅用于
metadata **缺失**时重建，都不是改参数入口。

安全步骤：

1. **确认无进程在跑**：GUI 元数据窗显示非「运行中」且无运行窗口，或 CLI
   `run.py status --novel 名` 显示非 running（K25 锁互斥，运行中手改会和原子写打架）；
2. **先备份**：复制 `metadata.json` 为 `metadata.json.bak`（改坏可还原）；
3. **值必须在合法范围**（越界即触发 emergency，A 类事故，见 §9）：

| 字段 | 合法范围 |
|---|---|
| `batch_size` | 1–20 |
| `timeline_window` | 1–50 |
| `summary_max` | 1000–20000（建议 ≤ `max_output_tokens.summarizer`×0.6，K28） |
| `chunk_max_chars` | 1000–30000（K28 上限） |

4. **只动这四个字段**，其它字段（status / processed / summary / current_batch /
   retries …）一律不碰；
5. 改完先验证——能正常输出即通过 schema 校验（§1.3），再续跑；新值从下次 run
   生效（**已处理内容不重读**，新参数只管之后的批次/小结）。

⚠️ 改坏（JSON 语法 / 类型 / 越界 / 动了别的字段）→ 下次读取触发 `MetadataCorrupt`
→ 整本书进入 emergency（`metadata.corrupt_<ts>`、进度字段 -1），恢复流程见 §9 A 类
——比改之前麻烦得多，所以「先备份」是硬要求。

### Q5. GUI 常见问题

| 现象 | 处置 |
|---|---|
| 元数据窗全是「—」 | 还没选小说目录，或该书尚未初始化（先「初始化」） |
| 点「开始」没反应 | 状态栏提示先选小说目录；选了小说库根目录要再选到含 `chunks\` 的子目录 |
| API Key 留空会怎样 | 沿用已保存的密文密钥；从没保存过则缺 key 启动报错（退出码 2） |
| 关窗口再开，配置还在吗 | 在。URL/模型在 config.json，Key 在 DPAPI 密文（自动解密回填输入框） |
| GUI 和命令行能混用吗 | 不同时操作同一本书即可（同一把锁）；GUI 停止 = Ctrl+C 语义，续跑互认断点 |
| 停止后如何续跑 | 直接再点「开始」（或命令行 `run.py run --novel 名`），从断点继续 |
| 远程小结报「认证失败/401」 | Summarizer 栏 Key 没填或填错（§5.3）；填对后「保存配置」再「开始」 |

### Q6. `run.log` 里写着 `tail=[82,87]`，可我已经在读 89 了，是 bug 吗？
不是。`tail` 是**时间线窗口**，止于「上次提交进度」（批开始前）——整批一起 append，
批内不推进（§6.2 事务边界、§13）；批内已读前片的剧情由 **K43 批内接力**注入，记在
同一行的 `inj=Np/Mr` 上。判定口径：

| 看什么 | 含义 |
|---|---|
| `scope=pre-batch` | 这一行的时间线窗口是「批开始前」快照，非实时进度 |
| `tail=[a,b]` | 时间线取到哪几片（`b` = 上次 commit 的 `processed`） |
| `inj=Np/Mr` | 本批已读前片注入了几片、几行事件（`inj=0p/0r` = 批首片/无前片） |
| `rows=` / `cap=` | 窗口实际行数 / K20 上限（`window×5`，超限整片丢弃 → `dropped=part_XXX`） |

真正要警惕的是：`[reader] [llm] ... [ERROR]`、`[driver] [error]`、以及
`attempt` 一次次变大——那才是故障（见 §9 速查表）。详细口径见 §10 Q2。

---

## 11. 测试

```bat
cd /d D:\data\dsh_wz1\novel_skill
set PYTHONIOENCODING=utf-8
D:\data\dsh_wz1\pyfordsh\python.exe -m pytest
```

- **223 项全离线**（backend=fake，无网络无费用可复现；含出口测试 52 = `[window]`
  日志的 `scope=pre-batch` / `inj=Np/Mr` 回归锁，见 §10 Q6）；
- 测试 39（真实模型冒烟，3 片小书）唯一需网络/付费环节——已用本地
  Qwen（llama-server 127.0.0.1:1235）真机验证通过，样例书保留在
  `novel_reading\smoke39`（时间线/笔记/小结可自行查看）；
- 真实 BASE（`novel_reading`）有测试污染防线，跑测试不影响真实数据。

---

## 12. 已知限制与风险

| 项 | 说明 |
|---|---|
| 时间线单文件无限增长 | ≥1000 分片性能退化时另行提需求（分卷），当前规模可接受 |
| 批内上下文盲区 | v3.2.1 遗留：`batch_size>1` 时批内第 2..N 片看不到本批前面片剧情——**已由 K43 批内接力解决**（注入前片事件行，保持重读可复现）；剩余边界见 §13 |
| 小上下文模型 413 | K38 快速失败；按模型容量选 `chunk_max_chars`（§8） |
| FakeLLM 与真实模型行为漂移 | 故障注入矩阵覆盖 + 测试 39 真机把关 |
| Windows 强杀（taskkill /F） | 优雅中断不生效，残留锁由下次接管（K26/K41） |
| GUI 依赖 PyQt6 | 仅 Windows 验证；非 Windows 上 DPAPI 侧退化为无密文保存（`secrets.py` 返回 None 不抛），key 需走环境变量 |
| 远程端点依赖公网 | summarizer 走远程时断网 → 连续超时 FATAL（§7 告诫 7）；本地 Qwen 不受影响 |
| 变更控制 | 需求级改动必须先出需求书勘误（K43+）再改代码，禁止代码先行 |

---

## 13. 设计取舍说明：`batch_size` 与批内上下文盲区（已由 K43 批内接力解决）

**一句话**：`batch_size` 不是「一次读多少片」，而是「**事务/回滚单元**」；它带来的
「批内上下文盲区」已在 **K43 批内接力**（需求书 v3.2.2 §15，2026-09-01）中解决。

**需求书原文（§1 阅读粒度）**：

> 逐分片一次调用（1 片 = 1 次 LLM）；`batch_size` 退化为**事务/回滚单元**。

**原始机制（v3.2.1）**：每一片 = 一次独立的 LLM 请求；每次请求的上下文快照 =
「前情摘要（覆盖到上次小结）＋ 最近 `timeline_window` **片**时间线行（覆盖到批开始前）＋
本片正文」。**批内不实时追加、不更新**——5 次调用共用批开始时的同一份快照，全部成功
后才一起写入时间线。

**原始代价（v3.2.1 真实存在）**：批内第 2..N 片**看不到本批前面片的剧情**。默认
`batch_size=5`、`timeline_window=10` 时最典型：读 11–15 片时摘要甚至还是空的（第一
次小结要到 `processed=15` 之后才触发），第 15 片只见「摘要 ＋ 1–10 片时间线行 ＋ 第 15
片正文」，11–14 的剧情对它完全不可见。盲区大小随 `batch_size` 线性增长。

**K43 批内接力（v3.2.2 解决）**：批内第 N 片请求时，从 `.batch\` 读取本批已读前片
（序号 < N）的 `events`，格式化为「本批已读前片事件」段注入 prompt（置于时间线段
之后、本片正文之前）。**第 3 片能看到第 1、2 片的事件行，第 5 片能看到前 4 片**——
盲区（事件级）消除。关键保证（§15 条款 5）：批失败回滚重跑时已成功片**不重调**、
`.batch` 结果原样保留（K22），注入内容与上次一致 → 重读结果依然可复现，事务底线
不变。失败隔离（条款 4）：`.batch` 缺失/损坏 → 跳过注入仅 WARN，不判失败。

**剩余边界（诚实记录）**：
- 注入的是前片**事件行**（event/impact），不含前片 `notes` 细节（角色发展/摘要）；
- 前情摘要仍只覆盖到上次小结（批内窗口语义不变，但事件级盲区已消除）；
- `batch_size=1` 本就无盲区，无需注入。

**历史缓解办法（v3.2.1 时期，现已被 K43 取代大半）**：

| 办法 | 效果 | 现状 |
|---|---|---|
| `batch_size=1` | 每片一提交，无盲区 | 仍有效（代价：快照/提交开销 ×5） |
| `batch_size` 调小 | 盲区变小 | K43 后非必需 |
| 正文按连续文本切分 | 情节缓解 | K43 后非必需 |
| 调小 `timeline_window` | **无效**（摘要只到批开始前） | 不变 |

**相关**：FAQ Q1（快速版）、§12 已知限制表、需求书 §15（K43 条款全文）。
