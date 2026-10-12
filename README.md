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

```sh
# DZMM → SillyTavern；默认生成 chat.sillytavern.jsonl
uv run dzmm2sillytavern convert chat.json --char-name "角色名" --user-name "用户名"

# SillyTavern → DZMM；默认生成 chat.dzmm.json
uv run dzmm2sillytavern convert chat.jsonl

# 显式指定格式和输出位置；不依赖文件扩展名识别
uv run dzmm2sillytavern convert export.txt --from dzmm --to sillytavern -o result.jsonl

# 标准输入/输出：- 表示管道；stdin 默认输出到 stdout
uv run dzmm2sillytavern convert chat.json -o - | uv run dzmm2sillytavern convert - --to dzmm -o restored.json
```

`--to` 默认选择输入的另一种格式；指定相同的输入/输出格式会报错。
名称优先使用命令行覆盖值，其次使用源文件中的名称；缺失时使用“用户”“角色”“系统”。`--model` 覆盖源模型名称，否则保留可用的源模型信息。

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

- 消息位置按从根到该消息之前的消息总数计算，与 chunk 边界无关；不按时间戳重排。主线由根及 `active` 子节点确定。
- 同一位置、同一角色的**所有分支消息**映射为同一条 ST 消息的 `swipes`，按 `children` 顺序深度优先收集；每条源消息只投影一次，不按文本去重。`swipe_id` 指向主线版本，同一位置有多种角色时先输出主线角色行。
- 不关联后文：每条消息的 swipes 独立，不会同步切换后续消息；原始树的跨消息分支关联不再保留。
- 主线没有对应消息的位置（分支更长或角色不同）使用空字符串作为选中 swipe，并给出提示；同一位置不同角色拆成独立行。空选中项使用该组首条分支消息的时间、名称和模型，并非原始主线消息。
- 重复节点、悬空引用、环、多根、多 active 子节点以及非法字段会报错。

**ST 界面限制：** 输出使用标准 `swipes`、`swipe_id`、`swipe_info`，没有把分支藏在自定义元数据里。当前 ST 上游只允许末条非用户消息直接左右切换；历史助手消息可以在 swipe picker 中查看备选并创建分支，用户消息没有原生 swipe 按钮。这是 ST 的界面限制，不能仅通过 JSONL 字段解除；本工具仍保留用户消息的备选供往返转换或支持这些操作的扩展使用。参见 [ST 切换条件](https://github.com/SillyTavern/SillyTavern/blob/release/public/script.js) 与 [swipe picker](https://github.com/SillyTavern/SillyTavern/blob/release/public/scripts/swipe-picker.js)。

### SillyTavern JSONL

支持标准元数据头、无头消息、UTF-8 BOM、空行、空会话。每条消息需要字符串 `mes` 和布尔值 `is_user`，可附带 `is_system`、`name`、`send_date`、`swipes`、`swipe_id`、`swipe_info`。空文件按空 SillyTavern 会话处理。

- swipes 反向生成为兄弟 chunk，选中项标记为 `active`；后续消息接到选中 chunk。切换某条 swipe 不会由此恢复 DZMM 原始分支联动。
- 当前 `mes` 是权威文本：与选中 swipe 不一致时保留编辑后的 `mes`，并提示更新该变体。
- 时间支持 ISO 8601、毫秒数字/数字字符串，以及 SillyTavern 的旧式 `2024-7-12@01h31m37s123ms`、`2024-6-5 @14h 56m 50s 682ms`、`June 19, 2023 2:20pm` 等格式。
- 无时区的时间按 UTC 解释；输出统一为 UTC，精度为毫秒。缺失消息时间使用 Unix epoch（`0`）并警告；缺失 swipe 时间沿用当前消息时间。已提供但无效的时间会报错。
- 为支持本工具往返转换，DZMM 输出额外携带 `chat.user_name`、`chat.character_name`、消息 `name`/`model`；当前消息时间与选中 swipe 时间不同时，另存 `selectedAt`。

**不是完整无损备份。** 不保留原始 UUID、平台/插件设置、附件、工具调用或推理扩展数据；不同角色拆行时会改变原始树结构。由于 ST swipe 不关联后文，反向转换不能恢复原始跨消息分支联动。仍应保存原始导出文件。

反向输出已通过本工具的往返验证；仓库未提供真实 DZMM 导入器或导入样本，**未验证 DZMM 平台直接导入兼容性**。上述附加字段用于本工具往返，不代表平台官方字段。

## 验证

```sh
uv run --locked python -m unittest discover -s tests -v
```

回归覆盖真实 CLI 双向调用、系统/用户/助手消息、分支顺序、swipe 选择与时间、旧式日期、Unicode 行分隔符、非法树结构，以及覆盖/链接别名保护。

SillyTavern 字段和旧式日期参考：[聊天导入实现](https://github.com/SillyTavern/SillyTavern/blob/release/src/endpoints/chats.js)、[时间解析实现](https://github.com/SillyTavern/SillyTavern/blob/release/public/scripts/utils.js)。
