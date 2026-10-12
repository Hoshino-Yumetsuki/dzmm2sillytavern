import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dzmm2sillytavern.convert import ConversionError, convert_text


def jsonl(records):
    return (
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    )


def dzmm_chat():
    return {
        "chatId": "chat-1",
        "chat": {
            "model": "source-model",
            "chunks": [
                {"id": "root", "children": ["old", "chosen"]},
                {
                    "id": "old",
                    "parent": "root",
                    "active": False,
                    "children": ["hidden"],
                },
                {"id": "chosen", "parent": "root", "active": True, "children": []},
                {"id": "hidden", "parent": "old", "active": True, "children": []},
            ],
        },
        "messages": [
            {
                "chunk_id": "root",
                "messages": [
                    {"role": "user", "content": "问题\n第二行", "createAt": 2000},
                ],
            },
            {
                "chunk_id": "old",
                "messages": [
                    {"role": "assistant", "content": "另一个答案", "createAt": 3000},
                ],
            },
            {
                "chunk_id": "chosen",
                "messages": [
                    {"role": "assistant", "content": "当前答案", "createAt": 1000},
                    {"role": "user", "content": "继续", "createAt": 500},
                ],
            },
            {
                "chunk_id": "hidden",
                "messages": [
                    {"role": "user", "content": "未选分支后续", "createAt": 4000},
                ],
            },
        ],
    }


class ConversionTests(unittest.TestCase):
    def run_cli(self, *args, text=None):
        env = dict(os.environ, NO_COLOR="1")
        return subprocess.run(
            [sys.executable, "-m", "dzmm2sillytavern", *args],
            check=False, input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=15,
        )

    def test_cli_roundtrip_preserves_selected_messages_and_swipes(self):
        records = [
            {"chat_metadata": {}, "user_name": "读者", "character_name": "角色"},
            {
                "name": "系统",
                "is_user": False,
                "is_system": True,
                "mes": "系统提示",
                "send_date": "2024-07-12T01:31:37.123Z",
            },
            {
                "name": "读者",
                "is_user": True,
                "mes": '中文\n"引号"',
                "send_date": "2024-07-12T01:31:38.000Z",
            },
            {
                "name": "角色",
                "is_user": False,
                "mes": "选中答案",
                "send_date": "2024-07-12T01:31:36.009Z",
                "swipes": ["选中答案", "备用答案"],
                "swipe_id": 0,
                "swipe_info": [
                    {"send_date": "2024-07-12T01:31:36.001Z"},
                    {"send_date": "2024-07-12T01:31:40.002Z"},
                ],
            },
            {
                "name": "读者",
                "is_user": True,
                "mes": "后续问题",
                "send_date": "2024-07-12T01:31:39.003Z",
            },
        ]
        reverse = self.run_cli("convert", "-", "--to", "dzmm", text=jsonl(records))
        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        json.loads(reverse.stdout)
        forward = self.run_cli("convert", "-", text=reverse.stdout)
        self.assertEqual(forward.returncode, 0, forward.stderr)
        restored = [json.loads(line) for line in forward.stdout.splitlines()][1:]
        self.assertEqual(
            [
                (
                    m["name"],
                    m["is_user"],
                    m.get("is_system", False),
                    m["mes"],
                    m["send_date"],
                )
                for m in restored
            ],
            [
                (
                    m["name"],
                    m["is_user"],
                    m.get("is_system", False),
                    m["mes"],
                    m["send_date"],
                )
                for m in records[1:]
            ],
        )
        self.assertEqual(restored[2]["swipes"], records[3]["swipes"])
        self.assertEqual(restored[2]["swipe_id"], 0)
        self.assertEqual(
            [s["send_date"] for s in restored[2]["swipe_info"]],
            [s["send_date"] for s in records[3]["swipe_info"]],
        )
        self.assertNotIn("\x1b", forward.stdout)

    def test_tree_order_and_unrepresentable_branch_warning(self):
        result = convert_text(
            json.dumps(dzmm_chat()),
            char_name="角色",
            user_name="读者",
            model="override",
        )
        messages = [json.loads(line) for line in result.text.splitlines()][1:]
        self.assertEqual(
            [m["mes"] for m in messages], ["问题\n第二行", "当前答案", "继续"]
        )
        self.assertEqual(messages[1]["swipes"], ["另一个答案", "当前答案"])
        self.assertEqual(messages[1]["swipe_id"], 1)
        self.assertEqual(messages[1]["extra"]["model"], "override")
        self.assertEqual(result.swipe_count, 1)
        self.assertRegex(" ".join(result.warnings), r"1.*(消息|message)")

    def test_invalid_graphs_fail_instead_of_dropping_messages(self):
        valid = dzmm_chat()
        variants = []
        duplicate = copy.deepcopy(valid)
        duplicate["chat"]["chunks"].append(
            copy.deepcopy(duplicate["chat"]["chunks"][0])
        )
        variants.append(duplicate)
        missing = copy.deepcopy(valid)
        missing["chat"]["chunks"][0]["children"].append("missing")
        variants.append(missing)
        ambiguous = copy.deepcopy(valid)
        ambiguous["chat"]["chunks"][1]["active"] = True
        variants.append(ambiguous)
        cycle = copy.deepcopy(valid)
        cycle["chat"]["chunks"][3]["children"] = ["root"]
        cycle["chat"]["chunks"][0]["parent"] = "hidden"
        variants.append(cycle)
        unknown_role = copy.deepcopy(valid)
        unknown_role["messages"][0]["messages"][0]["role"] = "tool"
        variants.append(unknown_role)
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ConversionError):
                convert_text(json.dumps(data))

    def test_legacy_and_offset_dates_preserve_milliseconds(self):
        dates = [
            ("2024-7-12@01h31m37s123ms", 1720747897123),
            ("2024-7-12 @01h 31m 37s 7ms", 1720747897007),
            ("2024-7-12@01h31m37s", 1720747897000),
            ("2024-07-12T03:31:37.123+02:00", 1720747897123),
            (1720747897123, 1720747897123),
            ("1720747897123", 1720747897123),
            ("June 19, 2023 2:20pm", 1687184400000),
        ]
        for date, expected in dates:
            with self.subTest(date=date):
                result = convert_text(
                    jsonl(
                        [
                            {
                                "name": "角色",
                                "is_user": False,
                                "mes": "消息",
                                "send_date": date,
                            }
                        ]
                    )
                )
                document = json.loads(result.text)
                messages = [
                    message
                    for entry in document["messages"]
                    for message in entry["messages"]
                ]
                self.assertEqual(messages[0]["createAt"], expected)

    def test_edited_selected_swipe_is_not_replaced_by_stale_text(self):
        original = jsonl(
            [
                {
                    "is_user": False,
                    "name": "角色",
                    "mes": "已编辑",
                    "send_date": 1000,
                    "swipes": ["旧文本", "备用"],
                    "swipe_id": 0,
                }
            ]
        )
        intermediate = convert_text(original)
        restored = convert_text(intermediate.text)
        message = json.loads(restored.text.splitlines()[1])
        self.assertEqual(message["mes"], "已编辑")
        self.assertEqual(message["swipes"], ["已编辑", "备用"])
        self.assertEqual(message["swipe_id"], 0)

    def test_unicode_line_separators_are_message_content(self):
        content = "前\u2028中\u2029后\u0085尾\n下一行"
        source = jsonl([{"is_user": True, "mes": content, "send_date": 1000}])
        reverse = convert_text("\ufeff\n" + source)
        forward = convert_text(reverse.text)
        message = json.loads(forward.text.split("\n")[1])
        self.assertEqual(message["mes"], content)

    def test_file_errors_do_not_overwrite_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.json")
            target = Path(directory, "target.jsonl")
            source.write_text(json.dumps(dzmm_chat()), encoding="utf-8")
            original = source.read_bytes()
            target.write_text("不能覆盖", encoding="utf-8")
            existing = self.run_cli("convert", str(source), "-o", str(target))
            self.assertEqual(existing.returncode, 1, existing.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "不能覆盖")
            for alias in (
                source,
                Path(directory, "symlink.json"),
                Path(directory, "hardlink.json"),
            ):
                if alias.name.startswith("symlink"):
                    alias.symlink_to(source)
                elif alias.name.startswith("hardlink"):
                    os.link(source, alias)
                same = self.run_cli("convert", str(source), "-o", str(alias), "--force")
                self.assertEqual(same.returncode, 1, same.stderr)
                self.assertEqual(source.read_bytes(), original)
            source.write_text('{"chat": broken}', encoding="utf-8")
            invalid = self.run_cli("convert", str(source), "-o", str(target), "--force")
            self.assertEqual(invalid.returncode, 1, invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "不能覆盖")
            self.assertEqual(invalid.stdout, "")


if __name__ == "__main__":
    unittest.main()
