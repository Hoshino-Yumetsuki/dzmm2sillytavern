import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NotRequired, TypedDict


@dataclass
class ConversionResult:
    text: str
    source_format: str
    target_format: str
    message_count: int
    swipe_count: int
    warnings: list[str]


class ConversionError(ValueError):
    """输入格式或聊天结构错误（包含字段或行号）。"""


class _Chunk(TypedDict):
    parent: str | None
    children: list[str]
    active: bool
    explicit_children: bool


class _DzmmChunk(TypedDict):
    id: str
    children: list[str]
    parent: NotRequired[str]
    active: NotRequired[bool]


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MISSING = object()


def _object(value, context):
    if not isinstance(value, dict):
        raise ConversionError(f"{context}: 必须是 JSON 对象")
    return value


def _list(value, context):
    if not isinstance(value, list):
        raise ConversionError(f"{context}: 必须是数组")
    return value


def _string(value, context):
    if not isinstance(value, str):
        raise ConversionError(f"{context}: 必须是字符串")
    return value


def _boolean(value, context):
    if not isinstance(value, bool):
        raise ConversionError(f"{context}: 必须是布尔值")
    return value


def _optional_string(obj, key, context):
    if key in obj:
        return _string(obj[key], f"{context}.{key}")
    return None


def _timestamp(value, context, warnings, *, dzmm=False, fallback=0):
    if value is _MISSING:
        warnings.append(f"{context}: 缺少时间，使用确定性毫秒值 {fallback}")
        return fallback
    try:
        if dzmm:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("DZMM 时间必须是整数毫秒")
            result = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise ValueError("非有限数值")
            result = int(value)
        elif isinstance(value, str):
            value = value.strip()
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
                # Decimal strings are parsed without losing large integer precision.
                result = int(value.partition(".")[0])
            else:
                match = re.fullmatch(
                    r"(\d{4})-(\d{1,2})-(\d{1,2})\s*@\s*(\d{1,2})h\s*"
                    r"(\d{1,2})m\s*(\d{1,2})s(?:\s*(\d{1,3})ms)?",
                    value,
                )
                if match:
                    year, month, day, hour, minute, second, millis = match.groups()
                    date = datetime(
                        int(year),
                        int(month),
                        int(day),
                        int(hour),
                        int(minute),
                        int(second),
                        int(millis or 0) * 1000,
                        tzinfo=UTC,
                    )
                else:
                    legacy = re.fullmatch(
                        r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap]m)",
                        value,
                        re.IGNORECASE,
                    )
                    if legacy:
                        month_name, day, year, hour, minute, ampm = legacy.groups()
                        months = (
                            "january",
                            "february",
                            "march",
                            "april",
                            "may",
                            "june",
                            "july",
                            "august",
                            "september",
                            "october",
                            "november",
                            "december",
                        )
                        month = months.index(month_name.lower()) + 1
                        hour = int(hour)
                        if not 1 <= hour <= 12:
                            raise ValueError("12 小时制小时无效")
                        date = datetime(
                            int(year),
                            month,
                            int(day),
                            hour % 12 + (12 if ampm.lower() == "pm" else 0),
                            int(minute),
                            tzinfo=UTC,
                        )
                    else:
                        date = datetime.fromisoformat(value)
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=UTC)
                    date = date.astimezone(UTC)
                delta = date - _EPOCH
                result = (
                    delta.days * 86400 + delta.seconds
                ) * 1000 + delta.microseconds // 1000
        else:
            raise ValueError("时间类型无效")
        return result
    except (ValueError, OverflowError, TypeError) as exc:
        raise ConversionError(f"{context}: 无效时间 {value!r}（{exc}）") from exc


def _iso(milliseconds):
    return (
        (_EPOCH + timedelta(milliseconds=milliseconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _load(text, context):
    try:
        return json.loads(
            text, parse_constant=lambda value: _bad_constant(value, context)
        )
    except json.JSONDecodeError as exc:
        raise ConversionError(
            f"{context}: JSON 错误，第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc
    except RecursionError as exc:
        raise ConversionError(f"{context}: JSON 嵌套过深") from exc
    except ValueError as exc:
        raise ConversionError(f"{context}: 无效 JSON 数值（{exc}）") from exc


def _bad_constant(value, context):
    raise ConversionError(f"{context}: 非标准 JSON 数值 {value}")


def _read_st(text, warnings):
    header = {}
    messages = []
    model = None
    metadata_dropped = False
    for line_number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        context = f"第 {line_number} 行"
        obj = _object(_load(line, context), context)
        if "chat_metadata" in obj and "mes" not in obj:
            if header or messages:
                raise ConversionError(f"{context}: 元数据头只能位于第一条记录")
            _object(obj["chat_metadata"], f"{context}.chat_metadata")
            for key in ("user_name", "character_name"):
                _optional_string(obj, key, context)
            header = obj
            metadata_dropped |= bool(obj["chat_metadata"]) or bool(
                set(obj) - {"chat_metadata", "user_name", "character_name"}
            )
            continue
        content = _string(obj.get("mes"), f"{context}.mes")
        is_user = _boolean(obj.get("is_user"), f"{context}.is_user")
        is_system = _boolean(obj.get("is_system", False), f"{context}.is_system")
        if is_user and is_system:
            raise ConversionError(f"{context}: is_user 与 is_system 不能同时为 true")
        role = "system" if is_system else "user" if is_user else "assistant"
        name = _optional_string(obj, "name", context)
        extra = _object(obj.get("extra", {}), f"{context}.extra")
        message_model = _optional_string(extra, "model", f"{context}.extra")
        model = model or message_model
        timestamp = _timestamp(
            obj.get("send_date", _MISSING), f"{context}.send_date", warnings
        )
        swipes = _list(obj.get("swipes", []), f"{context}.swipes")
        for index, swipe in enumerate(swipes):
            _string(swipe, f"{context}.swipes[{index}]")
        selected = obj.get("swipe_id", 0)
        if isinstance(selected, bool) or not isinstance(selected, int):
            raise ConversionError(f"{context}.swipe_id: 必须是整数")
        if (swipes and not 0 <= selected < len(swipes)) or (
            not swipes and selected != 0
        ):
            raise ConversionError(f"{context}.swipe_id: 超出 swipes 范围")
        info = _list(obj.get("swipe_info", []), f"{context}.swipe_info")
        if len(info) > len(swipes):
            raise ConversionError(f"{context}.swipe_info: 条目数超过 swipes")
        variants = []
        for index, swipe in enumerate(swipes or [content]):
            variant_context = f"{context}.swipe_info[{index}]"
            details = _object(info[index], variant_context) if index < len(info) else {}
            variant_extra = _object(
                details.get("extra", {}), f"{variant_context}.extra"
            )
            variant_model = (
                _optional_string(variant_extra, "model", f"{variant_context}.extra")
                or message_model
            )
            variant_name = _optional_string(details, "name", variant_context) or name
            variant_time = (
                _timestamp(
                    details["send_date"], f"{variant_context}.send_date", warnings
                )
                if "send_date" in details
                else timestamp
            )
            # mes is authoritative; retain both current and per-variant timestamps.
            if index == selected:
                if swipe != content:
                    warnings.append(
                        f"{context}.mes: 与选中 swipe 不同，保留当前 mes 并更新该变体"
                    )
                swipe = content
                variant_name = name or variant_name
                variant_model = message_model or variant_model
            variants.append(
                {
                    "role": role,
                    "content": swipe,
                    "createAt": variant_time,
                    "name": variant_name,
                    "model": variant_model,
                }
            )
            if index == selected and timestamp != variant_time:
                variants[-1]["selectedAt"] = timestamp
            model = model or variant_model
            metadata_dropped |= bool(
                set(details) - {"send_date", "extra", "name"}
            ) or bool(set(variant_extra) - {"model"})
        messages.append((variants, selected))
        metadata_dropped |= bool(
            set(obj)
            - {
                "name",
                "is_user",
                "is_system",
                "mes",
                "send_date",
                "extra",
                "swipes",
                "swipe_id",
                "swipe_info",
            }
        ) or bool(set(extra) - {"model"})
    if metadata_dropped:
        warnings.append("SillyTavern 平台/插件元数据不在核心转换范围内，未保留。")
    return header, messages, model


def _read_dzmm(data, warnings):
    data = _object(data, "DZMM")
    _string(data.get("chatId"), "chatId")
    chat = _object(data.get("chat"), "chat")
    for key in ("model", "user_name", "character_name"):
        _optional_string(chat, key, "chat")
    raw_chunks = _list(chat.get("chunks"), "chat.chunks")
    raw_entries = _list(data.get("messages"), "messages")
    chunks: dict[str, _Chunk] = {}
    contexts = {}
    for index, raw in enumerate(raw_chunks):
        context = f"chat.chunks[{index}]"
        raw = _object(raw, context)
        chunk_id = _string(raw.get("id"), f"{context}.id")
        if chunk_id in chunks:
            raise ConversionError(f"{context}.id: 重复节点 {chunk_id!r}")
        parent = raw.get("parent")
        if parent is not None:
            _string(parent, f"{context}.parent")
        if "active" in raw:
            _boolean(raw["active"], f"{context}.active")
        children = _list(raw.get("children", []), f"{context}.children")
        for child in children:
            _string(child, f"{context}.children")
        if len(children) != len(set(children)):
            raise ConversionError(f"{context}.children: 存在重复子节点")
        chunks[chunk_id] = {
            "parent": parent,
            "children": list(children),
            "active": raw.get("active", False),
            "explicit_children": "children" in raw,
        }
        contexts[chunk_id] = context
    roots = []
    for chunk_id, chunk in chunks.items():
        parent = chunk["parent"]
        if parent is None:
            roots.append(chunk_id)
        elif parent not in chunks:
            raise ConversionError(
                f"{contexts[chunk_id]}.parent: 节点 {parent!r} 不存在"
            )
        elif chunks[parent]["explicit_children"]:
            if chunk_id not in chunks[parent]["children"]:
                raise ConversionError(
                    f"{contexts[chunk_id]}.parent: 父节点 children 未包含此节点"
                )
        else:
            chunks[parent]["children"].append(chunk_id)
    for chunk_id, chunk in chunks.items():
        for child in chunk["children"]:
            if child not in chunks:
                raise ConversionError(
                    f"{contexts[chunk_id]}.children: 节点 {child!r} 不存在"
                )
            if chunks[child]["parent"] != chunk_id:
                raise ConversionError(
                    f"{contexts[chunk_id]}.children: 子节点 {child!r} 的 parent 不一致"
                )
        if sum(chunks[child]["active"] for child in chunk["children"]) > 1:
            raise ConversionError(
                f"{contexts[chunk_id]}.children: 存在多个 active 子节点"
            )
    # Iterative root traversal also detects disconnected cycles without recursion.
    reached = set()
    pending = list(roots)
    while pending:
        chunk_id = pending.pop()
        if chunk_id in reached:
            raise ConversionError(f"{contexts[chunk_id]}: 节点图存在循环或重复路径")
        reached.add(chunk_id)
        pending.extend(chunks[chunk_id]["children"])
    if len(reached) != len(chunks):
        unseen = next(chunk_id for chunk_id in chunks if chunk_id not in reached)
        raise ConversionError(f"{contexts[unseen]}: 节点图存在循环")
    if chunks and len(roots) != 1:
        raise ConversionError("chat.chunks: 必须恰好有一个根节点")
    lookup = {}
    metadata_dropped = bool(set(data) - {"chatId", "chat", "messages"}) or bool(
        set(chat) - {"model", "chunks", "user_name", "character_name"}
    )
    for index, raw in enumerate(raw_chunks):
        metadata_dropped |= bool(set(raw) - {"id", "parent", "children", "active"})
    for index, entry in enumerate(raw_entries):
        context = f"messages[{index}]"
        entry = _object(entry, context)
        chunk_id = _string(entry.get("chunk_id"), f"{context}.chunk_id")
        if chunk_id not in chunks:
            raise ConversionError(f"{context}.chunk_id: 节点 {chunk_id!r} 不存在")
        if chunk_id in lookup:
            raise ConversionError(f"{context}.chunk_id: 重复消息组 {chunk_id!r}")
        messages = _list(entry.get("messages"), f"{context}.messages")
        parsed = []
        for message_index, raw in enumerate(messages):
            message_context = f"{context}.messages[{message_index}]"
            raw = _object(raw, message_context)
            role = raw.get("role")
            if role not in ("user", "assistant", "system"):
                raise ConversionError(f"{message_context}.role: 未知角色 {role!r}")
            parsed.append(
                {
                    "role": role,
                    "content": _string(
                        raw.get("content"), f"{message_context}.content"
                    ),
                    "createAt": _timestamp(
                        raw.get("createAt", _MISSING),
                        f"{message_context}.createAt",
                        warnings,
                        dzmm=True,
                    ),
                    "name": _optional_string(raw, "name", message_context),
                    "model": _optional_string(raw, "model", message_context)
                    or chat.get("model"),
                }
            )
            if "selectedAt" in raw:
                parsed[-1]["selectedAt"] = _timestamp(
                    raw["selectedAt"],
                    f"{message_context}.selectedAt",
                    warnings,
                    dzmm=True,
                )
            metadata_dropped |= bool(
                set(raw)
                - {"role", "content", "createAt", "name", "model", "selectedAt"}
            )
        lookup[chunk_id] = parsed
        metadata_dropped |= bool(set(entry) - {"chunk_id", "messages"})
    path = []
    current = roots[0] if roots else None
    while current is not None:
        path.append(current)
        current = next(
            (child for child in chunks[current]["children"] if chunks[child]["active"]),
            None,
        )
    projected = set()
    messages = []
    for chunk_id in path:
        for index, message in enumerate(lookup.get(chunk_id, [])):
            projected.add((chunk_id, index))
            variants = [message]
            selected = 0
            parent = chunks[chunk_id]["parent"]
            if index == 0 and parent is not None:
                variants = []
                for sibling in chunks[parent]["children"]:
                    sibling_messages = lookup.get(sibling, [])
                    if (
                        sibling_messages
                        and sibling_messages[0]["role"] == message["role"]
                    ):
                        if sibling == chunk_id:
                            selected = len(variants)
                        variants.append(sibling_messages[0])
                        projected.add((sibling, 0))
            messages.append((variants, selected))
    dropped = sum(len(group) for group in lookup.values()) - len(projected)
    if dropped:
        warnings.append(
            f"DZMM 非活动分支有 {dropped} 条消息无法投影到当前路径/swipes，未保留。"
        )
    if metadata_dropped:
        warnings.append("DZMM 平台元数据不在核心转换范围内，未保留。")
    return chat, messages, chat.get("model")


def _names(header, messages, char_name, user_name):
    names = {
        "user": user_name if user_name is not None else header.get("user_name"),
        "assistant": char_name
        if char_name is not None
        else header.get("character_name"),
    }
    for variants, selected in messages:
        message = variants[selected]
        if message["role"] in names and names[message["role"]] is None:
            names[message["role"]] = message["name"]
    return {
        "user": names["user"] if names["user"] is not None else "用户",
        "assistant": names["assistant"] if names["assistant"] is not None else "角色",
        "system": "系统",
    }


def _message_name(message, names, char_name, user_name):
    override = (
        user_name
        if message["role"] == "user"
        else char_name
        if message["role"] == "assistant"
        else None
    )
    return (
        override
        if override is not None
        else message["name"]
        if message["name"] is not None
        else names[message["role"]]
    )


def _to_st(header, messages, inferred_model, char_name, user_name, model):
    names = _names(header, messages, char_name, user_name)
    rows = [
        {
            "chat_metadata": {},
            "user_name": names["user"],
            "character_name": names["assistant"],
        }
    ]
    for variants, selected in messages:
        message = variants[selected]

        def extra(variant):
            value = model if model is not None else variant["model"] or inferred_model
            return {"model": value} if value is not None else {}

        row = {
            "name": _message_name(message, names, char_name, user_name),
            "is_user": message["role"] == "user",
            "is_system": message["role"] == "system",
            "mes": message["content"],
            "send_date": _iso(message.get("selectedAt", message["createAt"])),
            "extra": extra(message),
        }
        if (
            len(variants) > 1
            or message["role"] == "assistant"
            or "selectedAt" in message
        ):
            row.update(
                swipes=[variant["content"] for variant in variants],
                swipe_id=selected,
                swipe_info=[
                    {
                        "send_date": _iso(variant["createAt"]),
                        "extra": extra(variant),
                        "name": _message_name(variant, names, char_name, user_name),
                    }
                    for variant in variants
                ],
            )
        rows.append(row)
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _to_dzmm(header, messages, inferred_model, char_name, user_name, model):
    names = _names(header, messages, char_name, user_name)
    root: _DzmmChunk = {"id": str(uuid.uuid4()), "children": []}
    chunks = [root]
    groups = []
    parent = root
    for variants, selected in messages:
        next_parent = parent
        for index, variant in enumerate(variants):
            chunk: _DzmmChunk = {
                "id": str(uuid.uuid4()),
                "parent": parent["id"],
                "children": [],
                "active": index == selected,
            }
            parent["children"].append(chunk["id"])
            chunks.append(chunk)
            message = {
                "role": variant["role"],
                "content": variant["content"],
                "createAt": variant["createAt"],
                "name": _message_name(variant, names, char_name, user_name),
            }
            if "selectedAt" in variant:
                message["selectedAt"] = variant["selectedAt"]
            variant_model = (
                model if model is not None else variant["model"] or inferred_model
            )
            if variant_model is not None:
                message["model"] = variant_model
            groups.append({"chunk_id": chunk["id"], "messages": [message]})
            if index == selected:
                next_parent = chunk
        parent = next_parent
    chat = {
        "chunks": chunks,
        "user_name": names["user"],
        "character_name": names["assistant"],
    }
    chat_model = model if model is not None else inferred_model
    if chat_model is not None:
        chat["model"] = chat_model
    return (
        json.dumps(
            {"chatId": str(uuid.uuid4()), "chat": chat, "messages": groups},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def convert_text(
    text: str,
    *,
    source_format: str = "auto",
    target_format: str | None = None,
    char_name: str | None = None,
    user_name: str | None = None,
    model: str | None = None,
) -> ConversionResult:
    """转换核心消息及 swipes；不承诺保留平台/插件元数据或非活动子树。"""
    _string(text, "输入")
    if source_format not in ("auto", "dzmm", "sillytavern"):
        raise ConversionError("source_format: 必须是 auto、dzmm 或 sillytavern")
    if target_format not in (None, "dzmm", "sillytavern"):
        raise ConversionError("target_format: 必须是 dzmm 或 sillytavern")
    for key, value in (
        ("char_name", char_name),
        ("user_name", user_name),
        ("model", model),
    ):
        if value is not None:
            _string(value, key)
    text = text.lstrip("\ufeff")
    data = None
    if source_format == "auto":
        try:
            data = _load(text, "输入") if text.strip() else None
        except ConversionError:
            # JSONL is not a single JSON document; its own parser supplies line context.
            pass
        source_format = (
            "dzmm"
            if isinstance(data, dict)
            and any(key in data for key in ("chat", "chatId", "messages"))
            else "sillytavern"
        )
    target_format = target_format or (
        "sillytavern" if source_format == "dzmm" else "dzmm"
    )
    if source_format == target_format:
        raise ConversionError("source_format/target_format: 输入和输出格式不能相同")
    warnings = []
    if source_format == "dzmm":
        if data is None:
            data = _load(text, "DZMM 输入")
        header, messages, inferred_model = _read_dzmm(data, warnings)
        output = _to_st(header, messages, inferred_model, char_name, user_name, model)
    else:
        header, messages, inferred_model = _read_st(text, warnings)
        output = _to_dzmm(header, messages, inferred_model, char_name, user_name, model)
    return ConversionResult(
        output,
        source_format,
        target_format,
        len(messages),
        sum(len(variants) - 1 for variants, selected in messages),
        warnings,
    )
