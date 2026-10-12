# dzmm2sillytavern

DZMM 聊天记录 JSON 与 SillyTavern 聊天记录 JSONL 双向转换 CLI。Python 3.11+，使用 `uv` 管理项目。

自动识别格式；终端显示短暂进度、转换数量和耗时；错误带字段或行号。所有状态信息写入 stderr，stdout 只用于转换结果，适合管道调用。

## 安装与运行

在项目目录执行：

```sh
uv sync --locked
uv run dzmm2sillytavern --help
uv run dzmm2sillytavern convert --help
```

也可以安装为独立命令：

```sh
uv tool install https://github.com/Hoshino-Yumetsuki/dzmm2sillytavern.git
dzmm2sillytavern --version
```

支持模块入口：`uv run python -m dzmm2sillytavern`。

## 双向转换

`--char-name` 和 `--user-name` 在双向转换中都必填，不能是空字符串或纯空白；shell 会把引号中的整段内容作为一个参数。名称原样保留，包括首尾空格，不会再次拆词或去除名称中的引号。

```sh
# 名称以 - 开头时使用 --选项="值" 形式
uv run dzmm2sillytavern convert chat.json --char-name "夜之角色 A & [主线]" --user-name "读者 B"
uv run dzmm2sillytavern convert chat.json --char-name="-角色" --user-name="用户;*?"
```

```sh
# DZMM → SillyTavern；默认生成 chat.sillytavern.jsonl
uv run dzmm2sillytavern convert chat.json --char-name "角色名" --user-name "用户名"

# SillyTavern → DZMM；默认生成 chat.dzmm.json
uv run dzmm2sillytavern convert chat.jsonl --char-name "角色名" --user-name "用户名"

# 显式指定格式和输出位置；不依赖文件扩展名识别
uv run dzmm2sillytavern convert export.txt --from dzmm --to sillytavern -o result.jsonl --char-name "角色名" --user-name "用户名"

# 标准输入/输出：- 表示管道；stdin 默认输出到 stdout
uv run dzmm2sillytavern convert chat.json -o - --char-name "角色名" --user-name "用户名" | uv run dzmm2sillytavern convert - --to dzmm -o restored.json --char-name "角色名" --user-name "用户名"
```

`--to` 默认选择输入的另一种格式；指定相同的输入/输出格式会报错。
命令行名称会原样写入输出；`--model` 覆盖源模型名称，否则保留可用的源模型信息。

双引号内的 `$`、反引号和反斜杠仍可能被 shell 解释；需要字面量时使用单引号，或遵循 shell 转义规则。例如 `--char-name '角色 $5 "夜"' --user-name "读者 B"`。作为分组符的外层引号不会成为名称的一部分。

## 输出与安全

- 默认拒绝覆盖已有文件；`--force` 允许替换输出，但始终禁止覆盖输入，包括符号链接和硬链接别名。
- 文件先完整转换，再写入同目录临时文件并原子提交；输入错误不会截断已有输出。输出目录必须已存在。
- `-q` / `--quiet` 隐藏进度和成功摘要，但保留警告和错误。
- `--color auto|always|never` 控制颜色。`auto` 遵循 `NO_COLOR`；显式 `always` 优先于 `NO_COLOR`。
- `--no-progress` 关闭动态进度；非终端环境不会显示动画。
- `-o`、`--force`、`-q`、`--color`、`--no-progress` 可放在 `convert` 前或后。
- `-h` / `--help` 查看帮助，`-V` / `--version` 查看版本；帮助和版本也写入 stderr。
- 退出码：成功 `0`，转换或文件错误 `1`，参数错误 `2`，用户中断 `130`。

## 格式与转换边界

本工具转换**聊天记录**，不是角色卡或图片。

### DZMM JSON

采用仓库原有转换脚本使用的结构：顶层 `chatId`、`chat.chunks` 和 `messages`。chunk 使用 `id`、`parent`、`children`、`active` 建树；每组消息通过 `chunk_id` 关联，消息包含 `role`、`content`、`createAt`（整数毫秒）。支持 `user`、`assistant`、`system` 三种角色。

- 沿根节点及 `active` 子节点提取当前主线，输出主线中的全部消息；保持树和消息数组的原始顺序，不按时间戳重排。没有 active 子节点时停止。
- 对每个活动 chunk 的第一条消息，收集兄弟 chunk 中同角色的第一条消息作为 `swipes`；顺序跟随父节点的 `children`，`swipe_id` 指向主线版本。
- 非活动 chunk 的后续消息、更深层消息和首条角色不匹配的兄弟消息不投影，会报告未保留的消息数量；未知平台字段也会警告。
- 不为未选分支新增消息行或合成空白 swipe，不按位置合并整棵树。源文件中真实存在的空消息仍会保留。
- 重复节点、悬空引用、环、多根、多 active 子节点以及非法字段会报错。

**ST 界面限制：** 输出使用标准 `swipes`、`swipe_id`、`swipe_info`。当前 ST 上游只允许末条非用户消息直接左右切换；历史助手消息可以在 swipe picker 中查看备选并创建分支，用户消息没有原生 swipe 按钮。转换器不绕过这些限制，也不会为此添加空白消息或修改角色。参见 [ST 切换条件](https://github.com/SillyTavern/SillyTavern/blob/release/public/script.js) 与 [swipe picker](https://github.com/SillyTavern/SillyTavern/blob/release/public/scripts/swipe-picker.js)。

### SillyTavern JSONL

支持标准元数据头、无头消息、UTF-8 BOM、空行、空会话。每条消息需要字符串 `mes` 和布尔值 `is_user`，可附带 `is_system`、`name`、`send_date`、`swipes`、`swipe_id`、`swipe_info`。空文件按空 SillyTavern 会话处理。

- swipes 反向生成为兄弟 chunk，选中项标记为 `active`；后续消息接到选中 chunk。切换某条 swipe 不会由此恢复 DZMM 原始分支联动。
- 当前 `mes` 是权威文本：与选中 swipe 不一致时保留编辑后的 `mes`，并提示更新该变体。
- 时间支持 ISO 8601、毫秒数字/数字字符串，以及 SillyTavern 的旧式 `2024-7-12@01h31m37s123ms`、`2024-6-5 @14h 56m 50s 682ms`、`June 19, 2023 2:20pm` 等格式。
- 无时区的时间按 UTC 解释；输出统一为 UTC，精度为毫秒。缺失消息时间使用 Unix epoch（`0`）并警告；缺失 swipe 时间沿用当前消息时间。已提供但无效的时间会报错。
- 为支持本工具往返转换，DZMM 输出额外携带 `chat.user_name`、`chat.character_name`、消息 `name`/`model`；当前消息时间与选中 swipe 时间不同时，另存 `selectedAt`。

**不是完整无损备份。** 不保留原始 UUID、未投影的非活动分支消息、平台/插件设置、附件、工具调用或推理扩展数据。反向转换不能恢复已丢弃的消息或原始跨消息分支关联。仍应保存原始导出文件。

反向输出已通过本工具的往返验证；仓库未提供真实 DZMM 导入器或导入样本，**未验证 DZMM 平台直接导入兼容性**。上述附加字段用于本工具往返，不代表平台官方字段。

## 验证

```sh
uv run --locked python -m unittest discover -s tests -v
```

回归覆盖真实 CLI 双向调用、系统/用户/助手消息、活动主线与兄弟 swipes、长分支和角色差异不生成空白消息、真实空消息保留、名称引号与特殊字符、旧式日期、Unicode 行分隔符、非法树结构，以及覆盖/链接别名保护。

SillyTavern 字段和旧式日期参考：[聊天导入实现](https://github.com/SillyTavern/SillyTavern/blob/release/src/endpoints/chats.js)、[时间解析实现](https://github.com/SillyTavern/SillyTavern/blob/release/public/scripts/utils.js)。
