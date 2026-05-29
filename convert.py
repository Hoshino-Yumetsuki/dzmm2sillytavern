"""
将 chat_export (dzmm格式) 的聊天记录 JSON 转换为 SillyTavern JSONL 格式。

用法:
  python convert.py --input chat_export_dzmm.json --output output.jsonl --char_name 角色名 --user_name 用户名
"""

import argparse
import json
import hashlib
import uuid
from datetime import datetime, timezone


def parse_args():
    parser = argparse.ArgumentParser(description="将聊天记录从 dzmm 格式转换为 SillyTavern 格式")
    parser.add_argument("--input", "-i", required=True, help="输入 JSON 文件路径")
    parser.add_argument("--output", "-o", default="output.jsonl", help="输出 JSONL 文件路径 (默认: output.jsonl)")
    parser.add_argument("--char_name", "-c", required=True, help="角色名称")
    parser.add_argument("--user_name", "-u", required=True, help="用户名称")
    parser.add_argument("--model", "-m", default="custom", help="模型名称 (默认: custom)")
    return parser.parse_args()


def timestamp_to_iso(ts_ms: int) -> str:
    """将毫秒时间戳转换为 ISO 8601 格式字符串"""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_chunk_tree(chunks: list) -> dict:
    """构建 chunk 树结构，返回 {id: chunk_data} 的映射"""
    chunk_map = {}
    for chunk in chunks:
        chunk_map[chunk["id"]] = chunk
    return chunk_map


def find_root_chunk(chunk_map: dict) -> str:
    """找到根 chunk（没有 parent 字段的 chunk）"""
    for chunk_id, chunk in chunk_map.items():
        if "parent" not in chunk:
            return chunk_id
    raise ValueError("找不到根 chunk（没有 parent 字段的节点）")


def walk_active_path(chunk_map: dict, root_id: str) -> list:
    """从根节点沿 active 路径走到叶子，返回 active chunk id 列表"""
    path = [root_id]
    current_id = root_id

    while True:
        current_chunk = chunk_map[current_id]
        children = current_chunk.get("children", [])
        if not children:
            break

        # 找到 active 的子节点
        active_child = None
        for child_id in children:
            if chunk_map[child_id].get("active", False):
                active_child = child_id
                break

        if active_child is None:
            # 没有 active 子节点，停止
            break

        path.append(active_child)
        current_id = active_child

    return path


def build_message_lookup(messages_data: list) -> dict:
    """构建 chunk_id -> messages 的映射"""
    lookup = {}
    for entry in messages_data:
        chunk_id = entry["chunk_id"]
        lookup[chunk_id] = entry["messages"]
    return lookup


def get_siblings(chunk_map: dict, chunk_id: str) -> list:
    """获取某个 chunk 的非 active 兄弟节点 ID 列表"""
    chunk = chunk_map[chunk_id]
    parent_id = chunk.get("parent")
    if parent_id is None:
        return []

    parent = chunk_map[parent_id]
    siblings = []
    for child_id in parent.get("children", []):
        if child_id != chunk_id:
            siblings.append(child_id)
    return siblings


def collect_swipes_for_first_message(
    chunk_id: str,
    chunk_map: dict,
    message_lookup: dict,
) -> list:
    """
    对于一个 active chunk 的第一条消息，收集其兄弟 chunk 中同角色的第一条消息作为 swipes。
    返回 [(content, createAt)] 列表。
    """
    active_messages = message_lookup.get(chunk_id, [])
    if not active_messages:
        return []

    target_role = active_messages[0]["role"]
    siblings = get_siblings(chunk_map, chunk_id)
    swipes = []

    for sib_id in siblings:
        sib_messages = message_lookup.get(sib_id, [])
        if sib_messages and sib_messages[0]["role"] == target_role:
            swipes.append((sib_messages[0]["content"], sib_messages[0]["createAt"]))

    return swipes


def generate_chat_id_hash(chat_id: str) -> int:
    """生成一个伪随机 chat_id_hash"""
    h = hashlib.md5(chat_id.encode()).hexdigest()
    return int(h[:16], 16) % (2**53)


def build_metadata(chat_id: str) -> dict:
    """构建 SillyTavern chat_metadata 头"""
    return {
        "chat_metadata": {
            "integrity": str(uuid.uuid4()),
            "chat_id_hash": generate_chat_id_hash(chat_id),
            "note_prompt": "",
            "note_interval": 1,
            "note_position": 1,
            "note_depth": 4,
            "note_role": 0,
            "timedWorldInfo": {"sticky": {}, "cooldown": {}},
            "tainted": True,
            "lastInContextMessageId": 0,
        },
        "user_name": "unused",
        "character_name": "unused",
    }


def build_user_message(content: str, send_date: str, user_name: str) -> dict:
    """构建 SillyTavern 用户消息"""
    return {
        "name": user_name,
        "is_user": True,
        "is_system": False,
        "send_date": send_date,
        "mes": content,
        "extra": {"isSmallSys": False, "reasoning": ""},
        "force_avatar": "/thumbnail?type=persona&file=user-default.png",
        "variables": [{}],
        "variables_initialized": [True],
        "is_ejs_processed": [True],
    }


def build_assistant_message(
    content: str,
    send_date: str,
    char_name: str,
    model: str,
    swipes: list = None,
    is_greeting: bool = False,
) -> dict:
    """
    构建 SillyTavern 助手消息。
    swipes: [(content, createAt)] 的列表，不含当前 active 的那条。
    """
    if swipes is None:
        swipes = []

    # 所有 swipe 文本列表：先放非 active 的，再放 active 的
    # 实际上 ST 的 swipe_id 指向当前选中的 swipe
    # 顺序：sibling swipes + active swipe，active 排最后
    all_swipe_texts = [s[0] for s in swipes] + [content]
    active_index = len(all_swipe_texts) - 1

    all_swipe_info = []
    for swipe_content, swipe_ts in swipes:
        all_swipe_info.append(
            {
                "send_date": timestamp_to_iso(swipe_ts),
                "extra": {
                    "api": "custom",
                    "model": model,
                    "reasoning": "",
                    "reasoning_duration": None,
                    "reasoning_signature": None,
                },
            }
        )
    # active swipe info
    all_swipe_info.append(
        {
            "send_date": send_date,
            "extra": {
                "api": "custom",
                "model": model,
                "reasoning": "",
                "reasoning_duration": None,
                "reasoning_signature": None,
            },
        }
    )

    if is_greeting:
        greeting_swipe_info = [
            {"send_date": timestamp_to_iso(s[1]) if i < len(swipes) else send_date, "extra": {}}
            for i, s in enumerate(swipes + [(content, 0)])
        ]
        return {
            "name": char_name,
            "is_user": False,
            "is_system": False,
            "send_date": send_date,
            "mes": content,
            "extra": {},
            "swipes": all_swipe_texts,
            "swipe_id": active_index,
            "swipe_info": greeting_swipe_info,
            "variables": [{}] * len(all_swipe_texts),
            "variables_initialized": [],
            "is_ejs_processed": [True] * len(all_swipe_texts),
        }

    return {
        "extra": {
            "api": "custom",
            "model": model,
            "reasoning": "",
            "reasoning_duration": None,
            "reasoning_signature": None,
        },
        "name": char_name,
        "is_user": False,
        "send_date": send_date,
        "mes": content,
        "title": "",
        "swipes": all_swipe_texts,
        "swipe_id": active_index,
        "swipe_info": all_swipe_info,
        "variables": [{}] * len(all_swipe_texts),
        "variables_initialized": [True] * len(all_swipe_texts),
        "is_ejs_processed": [True] * len(all_swipe_texts),
    }


def convert(input_path: str, output_path: str, char_name: str, user_name: str, model: str):
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    chat = data["chat"]
    chat_id = data["chatId"]
    source_model = chat.get("model", model)
    chunks = chat["chunks"]
    messages_data = data["messages"]

    # 构建数据结构
    chunk_map = build_chunk_tree(chunks)
    message_lookup = build_message_lookup(messages_data)
    root_id = find_root_chunk(chunk_map)
    active_path = walk_active_path(chunk_map, root_id)

    # 收集 active 路径上的所有消息（按时间顺序）
    # 每条消息记录其来源 chunk 以及在 chunk 中的位置
    flat_messages = []  # [(msg_data, chunk_id, index_in_chunk)]

    for chunk_id in active_path:
        chunk_messages = message_lookup.get(chunk_id, [])
        for idx, msg in enumerate(chunk_messages):
            flat_messages.append((msg, chunk_id, idx))

    # 按 createAt 排序（确保时间顺序正确）
    flat_messages.sort(key=lambda x: x[0]["createAt"])

    # 构建输出
    output = []

    # 1. 元数据
    output.append(build_metadata(chat_id))

    # 2. 消息
    is_first_assistant = True

    for msg_data, chunk_id, idx_in_chunk in flat_messages:
        role = msg_data["role"]
        content = msg_data["content"]
        send_date = timestamp_to_iso(msg_data["createAt"])

        if role == "user":
            output.append(build_user_message(content, send_date, user_name))

        elif role == "assistant":
            # 只对 chunk 中的第一条消息收集 swipes
            swipes = []
            if idx_in_chunk == 0:
                swipes = collect_swipes_for_first_message(chunk_id, chunk_map, message_lookup)

            output.append(
                build_assistant_message(
                    content=content,
                    send_date=send_date,
                    char_name=char_name,
                    model=source_model,
                    swipes=swipes,
                    is_greeting=is_first_assistant,
                )
            )
            is_first_assistant = False

    # 写入输出 (JSONL 格式，每行一个 JSON 对象)
    with open(output_path, "w", encoding="utf-8") as f:
        for item in output:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print("转换完成！")
    print(f"  输入: {input_path}")
    print(f"  输出: {output_path}")
    print(f"  消息数: {len(output) - 1}")
    print(f"  角色名: {char_name}")
    print(f"  用户名: {user_name}")


def main():
    args = parse_args()
    convert(args.input, args.output, args.char_name, args.user_name, args.model)


if __name__ == "__main__":
    main()
