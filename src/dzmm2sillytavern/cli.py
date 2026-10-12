import argparse
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from time import perf_counter

from rich.console import Console

from .convert import ConversionError, convert_text


class _Parser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            sys.stderr.write(message)

    def format_usage(self):
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self):
        return super().format_help().replace("usage: ", "用法：", 1)

    def error(self, message):
        translations = (
            ("the following arguments are required", "缺少必需参数"),
            ("unrecognized arguments", "无法识别的参数"),
            ("invalid choice", "无效选项"),
            ("choose from", "可选值"),
            ("expected one argument", "需要一个参数值"),
            ("argument ", "参数 "),
        )
        for original, translated in translations:
            message = message.replace(original, translated)
        self.print_usage()
        self.exit(2, f"错误：{message}\n")


def _common_options(parser):
    # 子命令未出现的选项不能覆盖命令前已经解析的值。
    parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT",
        default=argparse.SUPPRESS,
        help="输出路径；- 表示标准输出",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=argparse.SUPPRESS,
        help="替换已有输出文件（绝不覆盖输入）",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="隐藏进度和成功摘要，保留警告与错误",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default=argparse.SUPPRESS,
        help="颜色模式；auto 遵循 NO_COLOR",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        default=argparse.SUPPRESS,
        help="关闭动态进度，不影响颜色",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version="dzmm2sillytavern 0.1.0",
        help="显示版本并退出",
    )


def _parser():
    parser = _Parser(
        prog="dzmm2sillytavern",
        add_help=False,
        allow_abbrev=False,
        description="在 DZMM JSON 与 SillyTavern JSONL 之间双向转换聊天记录。",
        epilog="示例：dzmm2sillytavern convert chat.json -o chat.jsonl",
    )
    parser._positionals.title = "命令"
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    _common_options(parser)
    parser.set_defaults(
        output=None, force=False, quiet=False, color="auto", no_progress=False
    )
    commands = parser.add_subparsers(dest="command", metavar="命令")
    command = commands.add_parser(
        "convert",
        add_help=False,
        allow_abbrev=False,
        help="转换聊天记录",
        description="转换聊天记录；INPUT 为 - 时从标准输入读取。",
    )
    command._positionals.title = "位置参数"
    command._optionals.title = "选项"
    command.add_argument("-h", "--help", action="help", help="显示帮助并退出")
    command.add_argument("input", metavar="INPUT", help="输入路径，或 - 表示标准输入")
    _common_options(command)
    command.add_argument(
        "--from",
        dest="source_format",
        choices=("auto", "dzmm", "sillytavern"),
        default="auto",
        help="输入格式（默认自动检测）",
    )
    command.add_argument(
        "--to",
        dest="target_format",
        choices=("dzmm", "sillytavern"),
        help="输出格式（默认转换为另一种格式）",
    )
    command.add_argument("--char-name", metavar="NAME", help="覆盖角色名称")
    command.add_argument("--user-name", metavar="NAME", help="覆盖用户名称")
    command.add_argument("--model", metavar="MODEL", help="覆盖模型名称")
    return parser


def _check_output(source, output, force):
    if source is not None:
        try:
            same_path = source.resolve() == output.resolve()
        except RuntimeError:
            raise ConversionError(f"路径包含循环符号链接：{output}") from None
        same_file = output.exists() and source.samefile(output)
        if same_path or same_file:
            raise ConversionError(f"输出不能覆盖输入文件（包括链接别名）：{output}")
    if os.path.lexists(output) and not force:
        raise ConversionError(f"输出文件已存在：{output}；使用 --force 允许替换")
    if output.is_dir():
        raise ConversionError(f"输出路径是目录：{output}")


def _write_atomic(source, output, text, force):
    _check_output(source, output, force)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _check_output(source, output, force)
        if force:
            os.replace(temporary, output)
        else:
            # 原子创建：检查之后出现的文件也不会被覆盖。
            try:
                os.link(temporary, output)
            except FileExistsError:
                raise ConversionError(
                    f"输出文件已存在：{output}；使用 --force 允许替换"
                ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if not arguments:
            parser.print_help()
            return 2
        args = parser.parse_args(arguments)
        if args.command is None:
            parser.error("缺少命令：请使用 convert；用 --help 查看帮助")
    except SystemExit as exc:
        return exc.code

    color = args.color
    console = Console(
        stderr=True,
        markup=False,
        highlight=False,
        force_terminal=True
        if color == "always"
        else False
        if color == "never"
        else None,
        no_color=color == "never" or (color == "auto" and "NO_COLOR" in os.environ),
        color_system=None if color == "never" else "auto",
        soft_wrap=True,
    )
    start = perf_counter()
    try:
        source = None if args.input == "-" else Path(args.input)
        progress = sys.stderr.isatty() and not args.quiet and not args.no_progress
        with console.status("正在转换…", spinner="dots") if progress else nullcontext():
            text = (
                sys.stdin.read()
                if source is None
                else source.read_text(encoding="utf-8-sig")
            )
            result = convert_text(
                text,
                source_format=args.source_format,
                target_format=args.target_format,
                char_name=args.char_name,
                user_name=args.user_name,
                model=args.model,
            )
            if args.output is not None:
                destination = args.output
            elif source is None:
                destination = "-"
            else:
                suffix = (
                    ".sillytavern.jsonl"
                    if result.target_format == "sillytavern"
                    else ".dzmm.json"
                )
                destination = str(source.with_name(source.stem + suffix))
            if destination == "-":
                sys.stdout.write(result.text)
                sys.stdout.flush()
            else:
                _write_atomic(source, Path(destination), result.text, args.force)
        for warning in result.warnings:
            console.print(f"警告：{warning}", style="yellow", markup=False)
        if not args.quiet:
            label = "标准输出" if destination == "-" else destination
            console.print(
                f"已转换 {result.source_format} → {result.target_format} · "
                f"{result.message_count} 条消息 · {result.swipe_count} 个备选 · "
                f"{perf_counter() - start:.2f}s · {label}",
                style="green",
                markup=False,
            )
        return 0
    except BrokenPipeError:
        # 避免解释器退出时再次刷新已经关闭的管道。
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        console.print("已中断。", style="yellow", markup=False)
        return 130
    except ConversionError as exc:
        console.print(f"错误：{exc}", style="red", markup=False)
        return 1
    except (OSError, UnicodeError) as exc:
        console.print(f"错误：读取或写入失败：{exc}", style="red", markup=False)
        return 1
