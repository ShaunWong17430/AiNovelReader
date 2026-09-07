# tests\fixtures\ — FakeLLM fixture 键控约定（K10 / K30）

阶段 0 约定。阶段 3 起 `core/llm_client.py` 的 FakeLLM（`backend=="fake"`）
按本约定从本目录全部 `*.json` 读取预设响应（§7.6）。

## 文件格式

每个 `.json` 文件是一个测试场景（如 `smoke_sample.json`；阶段 4 的 12 片
全流程场景可另建 `book_12chunks.json`），含两个顶层段：

| 段 | 键 | 值 |
|---|---|---|
| `responses` | 键控约定（见下） | reader = 完整 JSON 对象（§4.4）；summarizer = 纯文本单段字符串（§4.3） |
| `faults` | 与 responses 同键控 | `{"kind": <§8.2 错误码>, "attempts": <可选，先失败次数，默认 1>, "payload": <可选，坏 JSON/超长注入载荷>}` |

## 键控约定（跨阶段契约，不得擅改）

| 阶段 | 键 | 字符串形式 | 示例 |
|---|---|---|---|
| reader | `(phase, chunk_id)` | `"reader:<chunk_id>"` | `reader:part_001` |
| summarizer | `(phase, "start-end")` | `"summarizer:<start>-<end>"` | `summarizer:1-5` |

- phase 用 `chat()` 形参名 `reader` / `summarizer`；llm.log/backup/rollback
  的阶段码 `read` / `summarize` 是另一套映射（§8.5），**不得混用**。
- summarizer 无 chunk_id（§4.3 输入为 chunk_start/chunk_end）；
  `start-end` 为本次小结覆盖的片号闭区间（K30）。
- 故障注入同键控：同一键在 `faults` 段出现时，FakeLLM 按 `kind` 注入故障
  （kind ∈ §8.2 全码集：NETWORK/TIMEOUT/RATE_LIMIT/SERVER_5XX/AUTH/NOT_FOUND/
  BAD_REQUEST/PAYLOAD_TOO_LARGE/EMPTY/TRUNCATED/PARSE/SCHEMA/ABORTED/UNKNOWN）；
  `attempts=n` 表示先失败 n 次后回落到 responses 的正常响应
  （供阶段 A / B 重试用例使用，K16）。
- 键在加载期由 `tests\conftest.py::FixtureStore` 校验（格式/重复/取值域）；
  用例一律用 `fixture_key(...)` 构造键，禁止手写拼接。

## 相关

- `tests\conftest.py`：`fixture_key` / `split_fixture_key` / `FixtureStore` /
  `fixture_store` fixture；BASE 沙箱（`base_dir` / `root_dir`）与真实 BASE 污染防线。
- 规格：REQUIREMENTS v3.2.1 §7.6（K10/K30）、§8.2、§4.3、§4.4、§8.5。
