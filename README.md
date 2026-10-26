# Codex Thread Cleaner

把本机 Codex 聊天记录导出为 Markdown，方便保存、阅读或交给其他 AI 分析。支持同一会话的多段 JSONL 记录合并，脚本直接处理本地文件，导出时无需让模型阅读聊天正文。

## 功能

- 接受会话 UUID 或 `codex://threads/<UUID>` 链接。
- 从 `~/.codex/sessions/` 和 `~/.codex/archived_sessions/` 查找记录。
- 按记录时间合并同一会话的多个文件，用消息 ID 去重。同一消息保留最后保存的内容及首次出现的位置，不按正文去重，保留正常重复发言。
- 默认保留用户和 AI 的正文、链接、代码片段及附件名称或路径，移除系统提示、推理和工具事件。
- 处理语音转写，优先采用已保存的对话消息，去除相邻的相同语音片段。
- 可选导出工具上下文，生成包含来源文件、消息数量、去重数量和解析警告的 JSON 报告。

脚本只读取源记录，生成 UTF-8 文件，不修改原始 JSONL，也不发送网络请求。

## 安装为技能

需要 Python 3.10 或更新版本，导出脚本仅使用 Python 标准库。

把此仓库放到本机的 Codex 技能目录下，目录名使用 `codex-thread-cleaner`。默认位置：

```text
~/.codex/skills/codex-thread-cleaner/
```

Windows 对应 `%USERPROFILE%\.codex\skills\codex-thread-cleaner\`。使用自定义 Codex 目录时，放到对应的 `skills` 目录。

目录结构：

```text
codex-thread-cleaner/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
└── scripts/
    ├── clean_thread.py
    └── test_clean_thread.py
```

在 Codex 中可以这样请求：

```text
用 $codex-thread-cleaner 把这个会话导出为 Markdown：
codex://threads/11111111-1111-4111-8111-111111111111
只导出，不分析聊天内容。
```

示例 UUID 需要换成实际会话 ID。本机须保存该会话的记录。

## 直接运行

在仓库目录执行：

```shell
python scripts/clean_thread.py --thread-id 11111111-1111-4111-8111-111111111111 --output outputs/conversation.md --report outputs/report.json
```

`--thread-id` 也接受完整的 `codex://threads/` 链接。输出目录会自动创建。

| 参数 | 作用 |
| --- | --- |
| `--thread-id` | 必填，会话 UUID 或链接 |
| `--output` | 必填，Markdown 输出路径 |
| `--report` | 可选，JSON 报告路径 |
| `--codex-root` | 可选，指定本地 Codex 数据目录；默认 `~/.codex` |
| `--mode screen` | 默认，只导出清理后的对话 |
| `--mode context` | 在对话中增加脚本支持的命令和工具记录 |
| `--keep-visible-meta` | 保留记录中已有的可见用时信息；本地 JSONL 缺少该字段时不补造 |

例如，把自定义目录中的会话连同工具上下文一起导出：

```shell
python scripts/clean_thread.py --thread-id 11111111-1111-4111-8111-111111111111 --codex-root /path/to/.codex --output outputs/context.md --report outputs/context-report.json --mode context
```

## 合并与报告

匹配到多个本地文件时，脚本会校验每个文件的 `session_meta`，然后按时间合并。正文消息通过 `metadata.retained_source.id` 识别；跨文件延续的同一轮对话会保留各段内容。

报告中的 `sources` 列出参与合并的文件，`source_count` 是文件数量，`messages` 是导出的消息数量，`duplicate_records` 是合并时去除的重复记录数量。解析问题写入 `warnings`，导出后应一并检查。`complete` 是脚本对已读取输入的标记，不证明本机保存了该会话的全部历史。

找不到记录、缺少 `session_meta` 或会话 ID 不匹配时，脚本报错并以状态码 `2` 退出。

## 导出范围

当前正文提取依赖带有 `retained_source` 元数据的本地消息格式，已在 Windows、Python 3.11 环境及真实多段会话上验证。其他版本的 Codex 记录格式需要按实际文件核对。

导出保留正文顺序，不总结或改写观点；会整理换行和空白，并移除内部标记，因此不用于逐字节还原原始记录。附件只导出记录中已有的名称、路径或链接，不读取附件内容，也不导出二进制数据。

聊天正文及工具上下文可能包含私有信息，报告含本地来源路径；分享时检查导出文件。仓库忽略默认输出目录和 JSONL 文件，真实聊天记录无需随技能上传。

## 验证

```shell
python scripts/test_clean_thread.py
```

现有 5 项回归覆盖分段顺序、跨段同一轮对话、重复消息更新、保留相同正文的独立消息、单文件读取及来源校验。
