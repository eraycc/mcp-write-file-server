from __future__ import annotations

import base64
import fnmatch
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


SERVER_NAME = "write-file-server"
SERVER_VERSION = "1.1.0"


# ---------------------------------------------------------------------------
# 关键修复 1：强制 stdio 使用 UTF-8，避免 Windows GBK 默认编码导致的乱码
# ---------------------------------------------------------------------------
def _force_utf8_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                # errors 用 replace（stdin/stdout），stderr 保持宽松
                stream.reconfigure(encoding="utf-8", errors="replace", newline="")
        except Exception:
            # 某些被重定向的流不支持 reconfigure，尽力而为
            pass


def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


# ---------------------------------------------------------------------------
# 关键修复 2：读文件用 utf-8-sig 去除 BOM，保留原始换行
# ---------------------------------------------------------------------------
def read_text(path: str) -> str:
    # utf-8-sig 会自动剥离开头 BOM；对无 BOM 文件行为与 utf-8 相同
    with Path(path).open("r", encoding="utf-8-sig", errors="replace", newline="") as file:
        return file.read()


def replace_surrogates(text: str) -> tuple[str, int]:
    replacements = 0
    chars: list[str] = []
    for char in text:
        if 0xD800 <= ord(char) <= 0xDFFF:
            chars.append("\uFFFD")
            replacements += 1
        else:
            chars.append(char)
    return "".join(chars), replacements


# ---------------------------------------------------------------------------
# 关键修复 3：原子写入（临时文件 + os.replace），避免半截损坏文件
# ---------------------------------------------------------------------------
def write_bytes(path: str, content: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 在同目录创建临时文件，保证 os.replace 是同一文件系统上的原子操作
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())  # 确保落盘，防止断电/崩溃丢数据
        os.replace(tmp_name, target)  # 原子替换
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 关键修复 4：可选换行归一化 + surrogate 清洗
# ---------------------------------------------------------------------------
def normalize_newlines(content: str, mode: str | None) -> str:
    if not mode or mode == "keep":
        return content
    # 先统一成 \n
    unified = content.replace("\r\n", "\n").replace("\r", "\n")
    if mode == "lf":
        return unified
    if mode == "crlf":
        return unified.replace("\n", "\r\n")
    return content


def write_text(path: str, content: str, newline: str | None = None) -> int:
    normalized = normalize_newlines(content, newline)
    safe_content, replacements = replace_surrogates(normalized)
    # surrogatepass 作为最后兜底，避免 encode 抛异常（正常已被清洗）
    write_bytes(path, safe_content.encode("utf-8", errors="surrogatepass"))
    return replacements


def replacement_warning(count: int) -> str:
    if count == 0:
        return ""
    return f"\nWARNING: replaced {count} invalid Unicode surrogate character(s) with U+FFFD."


def read_file(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", ""))
    if not path:
        return text_result("ERROR: path is required", True)
    try:
        return text_result(read_text(path))
    except FileNotFoundError:
        return text_result(f"ERROR: file not found: {path}", True)
    except IsADirectoryError:
        return text_result(f"ERROR: path is a directory: {path}", True)
    except Exception as exc:
        return text_result(f"ERROR: read failed: {exc}", True)


def read_files(args: dict[str, Any]) -> dict[str, Any]:
    raw_paths = args.get("paths", "")
    if isinstance(raw_paths, list):
        paths = [str(p).strip() for p in raw_paths if str(p).strip()]
    else:
        paths = [p.strip() for p in str(raw_paths).split(",") if p.strip()]
    if not paths:
        return text_result("ERROR: no file paths provided", True)

    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(f"========== file: {path} ==========\n{read_text(path)}")
        except Exception as exc:
            chunks.append(f"========== file: {path} [read failed] ==========\nERROR: {exc}")
    return text_result("\n\n".join(chunks))


def write_file(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", ""))
    content_base64 = args.get("contentBase64")
    content = args.get("content")
    newline = args.get("newline")  # 可选: keep | lf | crlf
    if not path:
        return text_result("ERROR: path is required", True)
    if content_base64 is not None:
        try:
            write_bytes(path, base64.b64decode(str(content_base64), validate=True))
            return text_result(f"OK: wrote base64 file: {path}")
        except Exception as exc:
            return text_result(f"ERROR: base64 write failed: {exc}", True)
    if content is None:
        return text_result("ERROR: content or contentBase64 is required", True)
    try:
        replacements = write_text(path, str(content), newline)
        return text_result(f"OK: wrote file: {path}{replacement_warning(replacements)}")
    except Exception as exc:
        return text_result(f"ERROR: write failed: {exc}", True)


def write_files(args: dict[str, Any]) -> dict[str, Any]:
    files = args.get("files")
    default_newline = args.get("newline")
    if not isinstance(files, list) or not files:
        return text_result("ERROR: files must be a non-empty array of {path, content}", True)

    written: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            errors.append("invalid item: expected object")
            continue
        path = str(item.get("path", ""))
        content = item.get("content")
        newline = item.get("newline", default_newline)
        if not path or content is None:
            errors.append(f"invalid item: {item!r}")
            continue
        try:
            replacements = write_text(path, str(content), newline)
            written.append(path)
            if replacements:
                warnings.append(f"{path}: replaced {replacements} invalid Unicode surrogate character(s) with U+FFFD")
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    parts: list[str] = []
    if written:
        parts.append("OK: wrote files:\n" + "\n".join(written))
    if warnings:
        parts.append("WARNING:\n" + "\n".join(warnings))
    if errors:
        parts.append("ERROR: write failures:\n" + "\n".join(errors))
    return text_result("\n\n".join(parts), bool(errors))


def edit_file(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", ""))
    old = args.get("oldString")
    new = args.get("newString")
    use_regex = bool(args.get("useRegex", False))
    replace_all = bool(args.get("replaceAll", False))
    ignore_case = bool(args.get("ignoreCase", False))
    newline = args.get("newline")

    if not path:
        return text_result("ERROR: path is required", True)
    if old is None:
        return text_result("ERROR: oldString is required", True)
    if new is None:
        return text_result("ERROR: newString is required", True)
    old_s = str(old)
    new_s = str(new)
    if not use_regex and old_s == "":
        return text_result("ERROR: oldString cannot be empty", True)

    try:
        original = read_text(path)
    except FileNotFoundError:
        return text_result(f"ERROR: file not found: {path}", True)
    except Exception as exc:
        return text_result(f"ERROR: read failed: {exc}", True)

    try:
        if use_regex:
            flags = re.IGNORECASE if ignore_case else 0
            pattern = re.compile(old_s, flags)
            matches = list(pattern.finditer(original))
            count = len(matches)
            if count == 0:
                return text_result(f"WARNING: no match found, file unchanged: {path}", True)
            updated = pattern.sub(new_s, original, 0 if replace_all else 1)
            replaced = count if replace_all else 1
        else:
            haystack = original.lower() if ignore_case else original
            needle = old_s.lower() if ignore_case else old_s
            positions: list[int] = []
            start = 0
            while True:
                index = haystack.find(needle, start)
                if index == -1:
                    break
                positions.append(index)
                start = index + len(needle)
            count = len(positions)
            if count == 0:
                return text_result(f"WARNING: no match found, file unchanged: {path}", True)
            if replace_all:
                pieces: list[str] = []
                last = 0
                for index in positions:
                    pieces.append(original[last:index])
                    pieces.append(new_s)
                    last = index + len(old_s)
                pieces.append(original[last:])
                updated = "".join(pieces)
                replaced = count
            else:
                index = positions[0]
                updated = original[:index] + new_s + original[index + len(old_s):]
                replaced = 1
    except re.error as exc:
        return text_result(f"ERROR: invalid regex: {exc}", True)
    except Exception as exc:
        return text_result(f"ERROR: replace failed: {exc}", True)

    if updated == original:
        return text_result(f"WARNING: replacement made no changes, file unchanged: {path}")

    try:
        replacements = write_text(path, updated, newline)
    except Exception as exc:
        return text_result(f"ERROR: write failed: {exc}", True)

    warning = ""
    if not replace_all and count > 1:
        warning = f"\nWARNING: matched {count} occurrence(s), but replaceAll=false replaced only the first one."
    warning += replacement_warning(replacements)
    return text_result(f"OK: replaced {replaced}/{count} occurrence(s): {path}{warning}")


def create_directory(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", ""))
    if not path:
        return text_result("ERROR: path is required", True)
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return text_result(f"OK: directory exists: {path}")
    except Exception as exc:
        return text_result(f"ERROR: create directory failed: {exc}", True)


def file_info(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", ""))
    if not path:
        return text_result("ERROR: path is required", True)
    target = Path(path)
    if not target.exists():
        return text_result(json.dumps({"path": path, "exists": False}, ensure_ascii=True, indent=2))
    try:
        stat = target.stat()
        info = {
            "path": path,
            "exists": True,
            "type": "directory" if target.is_dir() else "file",
            "size": stat.st_size,
            "modifiedTime": stat.st_mtime,
            "createdTime": stat.st_ctime,
        }
        return text_result(json.dumps(info, ensure_ascii=True, indent=2))
    except Exception as exc:
        return text_result(f"ERROR: file info failed: {exc}", True)


def search_files(args: dict[str, Any]) -> dict[str, Any]:
    root = str(args.get("path", ""))
    pattern = str(args.get("pattern", ""))
    include = args.get("include")
    ignore_case = bool(args.get("ignoreCase", False))
    only_matching = bool(args.get("onlyMatching", False))
    max_results = int(args.get("maxResults") or 200)

    if not root:
        return text_result("ERROR: path is required", True)
    if not pattern:
        return text_result("ERROR: pattern is required", True)

    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return text_result(f"ERROR: invalid regex: {exc}", True)

    include_patterns = None
    if include:
        include_patterns = [p.strip() for p in str(include).split(",") if p.strip()]

    skip_dirs = {"node_modules", ".git", "target", "build", "dist", ".idea", ".vscode"}
    root_path = Path(root)
    if not root_path.exists():
        return text_result(f"ERROR: path does not exist: {root}", True)

    def include_name(name: str) -> bool:
        if not include_patterns:
            return True
        return any(fnmatch.fnmatch(name, pat) for pat in include_patterns)

    def iter_files() -> list[Path]:
        if root_path.is_file():
            return [root_path] if include_name(root_path.name) else []
        files: list[Path] = []
        for current, dirs, names in os.walk(root_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in names:
                if include_name(name):
                    files.append(Path(current) / name)
        return files

    results: list[str] = []
    scanned = 0
    matched_files = set()
    truncated = False
    for file_path in iter_files():
        scanned += 1
        try:
            lines = read_text(str(file_path)).splitlines()
        except Exception:
            continue
        for line_no, line in enumerate(lines, 1):
            if only_matching:
                matches = list(regex.finditer(line))
                if matches:
                    matched_files.add(str(file_path))
                for match in matches:
                    results.append(f"{file_path}:{line_no}:{match.group(0)}")
                    if len(results) >= max_results:
                        truncated = True
                        break
            else:
                if regex.search(line):
                    matched_files.add(str(file_path))
                    results.append(f"{file_path}:{line_no}:{line}")
                    if len(results) >= max_results:
                        truncated = True
            if truncated:
                break
        if truncated:
            break

    if not results:
        return text_result(f"No matches found (scanned {scanned} file(s), root: {root})")
    text = f"Found {len(results)} match(es) in {len(matched_files)} file(s), scanned {scanned} file(s):\n" + "\n".join(results)
    if truncated:
        text += f"\n... truncated at maxResults={max_results}. Increase maxResults for more."
    return text_result(text)


def check_status(_: dict[str, Any]) -> dict[str, Any]:
    return text_result(
        "OK: write-file-server running\n"
        f"Python: {sys.version.split()[0]}\n"
        f"Version: {SERVER_VERSION}\n"
        f"stdio encoding: {getattr(sys.stdout, 'encoding', 'unknown')}\n"
        "Purpose: atomic UTF-8 file I/O with BOM handling and surrogate cleaning."
    )


TOOLS: dict[str, dict[str, Any]] = {
    "read_file": {
        "description": "Read one UTF-8 text file (BOM auto-stripped).",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "handler": read_file,
    },
    "read_files": {
        "description": "Read multiple UTF-8 text files. paths may be a comma-separated string or an array.",
        "inputSchema": {
            "type": "object",
            "properties": {"paths": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}},
            "required": ["paths"],
        },
        "handler": read_files,
    },
    "write_file": {
        "description": "Atomically write one UTF-8 plaintext file. Use contentBase64 for exact bytes. Optional newline: keep|lf|crlf.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "contentBase64": {"type": "string"},
                "newline": {"type": "string", "enum": ["keep", "lf", "crlf"]},
            },
            "required": ["path"],
        },
        "handler": write_file,
    },
    "write_files": {
        "description": "Atomically write multiple UTF-8 files. files is [{path, content, newline?}].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "newline": {"type": "string", "enum": ["keep", "lf", "crlf"]},
                        },
                        "required": ["path", "content"],
                    },
                },
                "newline": {"type": "string", "enum": ["keep", "lf", "crlf"]},
            },
            "required": ["files"],
        },
        "handler": write_files,
    },
    "edit_file": {
        "description": "Read, replace, and atomically write back. Supports replaceAll, ignoreCase, useRegex, newline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "oldString": {"type": "string"},
                "newString": {"type": "string"},
                "useRegex": {"type": "boolean"},
                "replaceAll": {"type": "boolean"},
                "ignoreCase": {"type": "boolean"},
                "newline": {"type": "string", "enum": ["keep", "lf", "crlf"]},
            },
            "required": ["path", "oldString", "newString"],
        },
        "handler": edit_file,
    },
    "search_files": {
        "description": "Search UTF-8 text files recursively.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "include": {"type": "string"},
                "ignoreCase": {"type": "boolean"},
                "onlyMatching": {"type": "boolean"},
                "maxResults": {"type": "number"},
            },
            "required": ["path", "pattern"],
        },
        "handler": search_files,
    },
    "create_directory": {
        "description": "Create a directory recursively.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "handler": create_directory,
    },
    "file_info": {
        "description": "Return file or directory existence, type, size, and timestamps.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "handler": file_info,
    },
    "check_status": {
        "description": "Check write-file-server status.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": check_status,
    },
}


def tool_specs() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for name, spec in TOOLS.items()
    ]


def respond(message_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    # ensure_ascii=True 让所有非 ASCII 转为 \uXXXX，彻底规避传输层编码问题
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None:
        return

    if method == "initialize":
        respond(message_id, {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "tools/list":
        respond(message_id, {"tools": tool_specs()})
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            respond(message_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
            return
        try:
            respond(message_id, spec["handler"](arguments))
        except Exception as exc:
            # 兜底：单个工具崩溃不应拖垮整个连接
            respond(message_id, error={"code": -32603, "message": f"Tool crashed: {exc}"})
    elif method == "ping":
        respond(message_id, {})
    else:
        respond(message_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    _force_utf8_stdio()  # 必须在任何 I/O 之前执行
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as exc:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }, ensure_ascii=True) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
