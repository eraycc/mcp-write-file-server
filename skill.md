---
name: mcp-write-file-server
description: 在 IP-Guard、天锐绿盾、TSD、亿赛通、深信服等文件加密软件环境中，只要用户需要写入、覆盖、编辑、保存或生成文件，并且结果必须保持普通明文、不能被加密软件加密，就必须使用本 skill。读取加密或未知文件时先用 mcp-read-file-server 读取明文；写回、覆盖、批量写入或精确编辑时使用 mcp-write-file-server 的 Python 工具。不要用 mcp-read-file-server 的写入工具写回需要保持不加密明文的文件。
---

# MCP Write File Server Skill

本 skill 定义加密软件环境里的写入策略，尤其用于“写回后必须保持不加密明文”的场景。

当前机器上存在两条不同的文件通道：

- `mcp-read-file-server`：Node.js 进程。Node.js 通常在加密软件白名单中，因此适合读取被 IP-Guard、绿盾、TSD 等保护文件的明文。
- `mcp-write-file-server`：Python 进程。它用于写出普通明文文件，避免 Node.js 白名单写入触发加密软件自动加密落盘。

当用户希望写入结果保持普通明文时，写入阶段必须使用 `mcp-write-file-server`。

## 核心规则

1. 读取加密文件、受保护文件或未知状态文件时，使用 `mcp-read-file-server.read_file` 或 `mcp-read-file-server.read_files`。
2. 覆盖写入并保持明文时，使用 `mcp-write-file-server.write_file`。
3. 批量写入并保持明文时，使用 `mcp-write-file-server.write_files`。
4. 精确编辑并保持明文时，先用 `mcp-read-file-server.read_file` 读取原文，再用 `mcp-write-file-server.edit_file`，其中 `oldString` 必须来自读取结果并保持完全一致。
5. 不要使用内置 Read、Write、Edit、MultiEdit、Grep，也不要用 PowerShell `Get-Content`、`cat`、`grep`、`rg`、`Select-String`、shell 重定向或临时脚本直接读写文件内容。
6. 不要用 `mcp-read-file-server.write_file` 写回必须保持明文的文件；Node.js 白名单进程写入可能导致文件被加密软件自动加密。

## 工具选择表

| 场景 | 必用工具 | 原因 |
|---|---|---|
| 读取单个加密或未知文件 | `mcp-read-file-server.read_file` | Node.js 白名单进程能读取明文 |
| 批量读取加密或未知文件 | `mcp-read-file-server.read_files` | 批量读取明文 |
| 搜索加密或未知文件内容 | `mcp-read-file-server.search_files` | 避免 shell 搜索读到密文 |
| 覆盖写入并保持明文 | `mcp-write-file-server.write_file` | Python 写入，避免加密落盘 |
| 批量写入并保持明文 | `mcp-write-file-server.write_files` | Python 批量写入普通明文 |
| 精确替换并保持明文 | `mcp-write-file-server.edit_file` | Python 读改写，结果保持明文 |
| 创建目录 | `mcp-write-file-server.create_directory` | 为明文写入准备目录 |
| 查看文件元信息 | `mcp-write-file-server.file_info` | 不读取文件正文 |

## 标准流程

### 读取后覆盖写回同一文件并保持明文

1. 用 `mcp-read-file-server.read_file` 读取目标文件明文。
2. 在上下文中生成最终要写入的完整内容。
3. 用 `mcp-write-file-server.write_file` 覆盖写入同一路径。
4. 必要时再读取校验。

### 精确编辑已有文件并保持明文

1. 用 `mcp-read-file-server.read_file` 读取原始内容。
2. 从读取结果中原样复制 `oldString`，包括空格、缩进和换行。
3. 用 `mcp-write-file-server.edit_file` 替换内容。
4. 必要时读取校验。

### 新建普通明文文件

直接使用 `mcp-write-file-server.write_file`。该工具会自动创建父目录。

## 用户意图判断

用户出现以下表达时，写入阶段必须使用 `mcp-write-file-server`：

- “保持明文”
- “不要加密”
- “写回时必须不加密”
- “避免被 IP-Guard 加密”
- “避免绿盾加密”
- “用 Python MCP 写”
- “用 mcp-write-file-server”
- “覆盖写入刚读取的内容”
- “写回后肯定要不加密”

如果当前环境已知存在文件加密软件，而用户没有明确要求输出继续受加密保护，默认写入阶段选择 `mcp-write-file-server`。

## 编码和换行行为

当前优化后的 `mcp-write-file-server` 行为如下：

- 写入时按 UTF-8 bytes 落盘。
- 支持中文和其他正常 Unicode 字符。
- MCP 参数中如果出现非法 Unicode surrogate，会替换为 `U+FFFD`，避免 `UnicodeEncodeError` 导致写入失败。
- 写入时保留传入内容里的换行，不做 Windows 文本模式的自动换行转换。
- 读取时使用 UTF-8，并对非法字节做替换，避免单个坏字节导致工具崩溃。
- 如果直接传中文参数的调用链出现乱码，可使用 `contentBase64` 传 UTF-8 字节的 Base64 文本，避免中间层转码。

这意味着：正常中文内容应直接写入；如果出现 `\udca8`、`\udc85` 这类非法 surrogate，工具会尽量完成写入并在返回消息中提示替换数量。对于 skill、说明文档等中文长文本，优先使用 `contentBase64` 方式写入更稳。

## 重要注意事项

- `mcp-write-file-server.write_file` 是覆盖写入，调用前必须确认 `content` 或 `contentBase64` 是完整目标内容。
- 编辑已有文件前必须先读取，避免凭记忆构造 `oldString`。
- 对加密文件做“读后原样覆盖”时，读用 `mcp-read-file-server`，写用 `mcp-write-file-server`。
- Shell 命令只适合列文件名、查看进程、运行测试、启动服务等不读写文件正文的操作。
- 修改 `mcp-write-file-server/server.py` 后，需要重启 MCP server 或重启 Codex，当前会话中的 MCP 进程才会加载新实现。
