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
    def run_cli(self, *args, text=None, names=("角色", "读者")):
        env = dict(os.environ, NO_COLOR="1")
        if names is not None:
            args = (*args, f"--char-name={names[0]}", f"--user-name={names[1]}")
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

    def test_tree_order_and_complete_branch_projection(self):
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
        self.assertEqual(messages[2]["swipes"], ["未选分支后续", "继续"])
        self.assertEqual(messages[2]["swipe_id"], 1)
        self.assertEqual(result.swipe_count, 2)
        self.assertEqual(result.warnings, [])

    def test_nested_longer_and_mixed_role_branches_remain_independent_swipes(self):
        data = dzmm_chat()
        data["messages"][1]["messages"].append(
            {"role": "system", "content": "支线系统", "createAt": 3100}
        )
        data["messages"][3]["messages"] = [
            {"role": "assistant", "content": "重复文本", "createAt": 4000},
            {"role": "assistant", "content": "重复文本", "createAt": 5000},
        ]
        data["chat"]["chunks"][3]["children"] = ["tail-a", "tail-b"]
        for index, chunk_id in enumerate(("tail-a", "tail-b")):
            data["chat"]["chunks"].append(
                {"id": chunk_id, "parent": "hidden", "children": [], "active": index == 0}
            )
            data["messages"].append({"chunk_id": chunk_id, "messages": [
                {"role": "assistant", "content": chunk_id, "createAt": 6000 + index},
            ]})
        result = self.run_cli("convert", "-", text=json.dumps(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.split("\n") if line][1:]
        self.assertEqual([row["mes"] for row in rows],
                         ["问题\n第二行", "当前答案", "继续", "", "", "", ""])
        self.assertTrue(rows[2]["is_user"])
        self.assertTrue(rows[3]["is_system"])
        self.assertEqual(rows[3]["swipes"], ["支线系统", ""])
        self.assertEqual(rows[4]["swipes"], ["重复文本", ""])
        self.assertEqual(rows[5]["swipes"], ["重复文本", ""])
        self.assertEqual(rows[6]["swipes"], ["tail-a", "tail-b", ""])
        self.assertEqual(rows[6]["swipe_info"][1]["send_date"], "1970-01-01T00:00:06.001Z")
        for row in rows[1:]:
            if "swipes" in row:
                self.assertEqual(row["mes"], row["swipes"][row["swipe_id"]])
        # Switching one answer is an independent edit; later selected messages
        # and every alternative remain intact through the reverse conversion.
        rows[1]["swipe_id"] = 0
        rows[1]["mes"] = rows[1]["swipes"][0]
        back = convert_text(convert_text(jsonl(rows)).text)
        restored = [json.loads(line) for line in back.text.split("\n") if line][1:]
        self.assertEqual([row["mes"] for row in restored],
                         ["问题\n第二行", "另一个答案", "继续", "", "", "", ""])
        self.assertEqual([row.get("swipes") for row in restored],
                         [row.get("swipes") for row in rows])

    def test_no_active_child_and_empty_chunks_do_not_hide_swipes(self):
        data = dzmm_chat()
        data["chat"]["chunks"][2]["active"] = False
        data["messages"][0]["messages"] = []
        data["messages"][1]["messages"][0]["content"] = ""
        converted = convert_text(json.dumps(data))
        rows = [json.loads(line) for line in converted.text.split("\n") if line][1:]
        self.assertEqual([row["swipes"] for row in rows],
                         [["", "当前答案", ""], ["未选分支后续", "继续", ""]])
        self.assertEqual([row["swipe_id"] for row in rows], [2, 2])

    def test_cli_requires_both_nonblank_names_in_both_directions(self):
        sources = [json.dumps(dzmm_chat()), jsonl([
            {"is_user": False, "mes": "回答", "send_date": 1000},
        ])]
        options = [(), ("--char-name", "角色"), ("--user-name", "用户"),
                   ("--char-name", "", "--user-name", "用户"),
                   ("--char-name", "角色", "--user-name", "  ")]
        for source in sources:
            for args in options:
                with self.subTest(args=args, source=source):
                    result = self.run_cli("convert", "-", *args, text=source, names=None)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX shell quoting")
    def test_shell_quoted_names_preserve_spaces_and_special_characters(self):
        command = r'''exec "$@" --char-name "  角色 A & [夜] \"Q\" \$5  " --user-name="-用户 B; * ?"'''
        result = subprocess.run(
            ["/bin/sh", "-c", command, "name-test", sys.executable, "-m",
             "dzmm2sillytavern", "convert", "-"],
            input=json.dumps(dzmm_chat()), capture_output=True, text=True,
            encoding="utf-8", timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        names = ('  角色 A & [夜] "Q" $5  ', "-用户 B; * ?")
        rows = [json.loads(line) for line in result.stdout.split("\n") if line]
        self.assertEqual(rows[0]["character_name"], names[0])
        self.assertEqual(rows[0]["user_name"], names[1])
        self.assertEqual(rows[2]["name"], names[0])
        self.assertEqual(rows[1]["name"], names[1])
        self.assertEqual([info["name"] for info in rows[2]["swipe_info"]], [names[0], names[0]])
        reverse = self.run_cli("convert", "-", text=result.stdout, names=names)
        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        restored = self.run_cli("convert", "-", text=reverse.stdout, names=names)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual([json.loads(line)["name"] for line in restored.stdout.split("\n")[1:] if line],
                         [row["name"] for row in rows[1:]])

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
