# 阅读任务（单分片）

## 任务

阅读小说《$novel_name》的分片 $chunk_id（单片），并输出该片的结构化阅读笔记。你只处理这一片，不处理也不推测任何其他分片的内容。

## 前情摘要

以下为全书滚动摘要（截至本片之前的全部已知信息；为空表示「（无前情）」）：

$summary_text

## 时间线（前情事件）

以下为前情情节时间线的事件行（仅作背景参考，行数可能为空）：

$timeline_tail

$batch_section

## 本片正文

$chunk_text

## 输出要求

- 只处理分片 $chunk_id；跨分片事件只记录本片发生/推进的部分，禁止编造其他分片内容。
- 严格输出**单个 JSON 对象**：禁止 Markdown 围栏、禁止任何前后缀文字、禁止额外说明。
- `events` 数组长度须在 $event_min 到 $event_max 之间；每条 `event`/`impact` 内禁止换行与半角竖线 `|`（如需要请用全角 `｜`）。
- `notes.characters` ≤ 500 字；`notes.plots` 每条 ≤ 100 字；`notes.questions` ≤ 3 条；`notes.abstract` ≤ 300 字。
- 输出结构必须完全符合以下 JSON Schema：

$json_schema
