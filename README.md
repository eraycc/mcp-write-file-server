# mcp-write-file-server
用于ai编程工具，如codex，claudecode等，基于python提供文件读写mcp工具

关联项目：https://github.com/hebulin/mcp-read-file-server

Python stdio MCP server for file operations on machines where Node.js writes are
automatically encrypted by endpoint encryption software.

Global intended usage:

- Read encrypted files with `mcp-read-file-server`.
- Write, overwrite, and edit files with `mcp-write-file-server` when the result
  must remain normal plaintext.

Why this server exists:

- `mcp-read-file-server` runs under Node.js. On this machine Node.js is trusted by
  the encryption software, so it can read encrypted files as plaintext.
- Node.js writes may be encrypted on disk by IP-Guard/LvDun/TSD-style software.
- This server runs under Python and writes plaintext files for cases where the
  output must not be encrypted.

Encoding behavior:

- Files are written as UTF-8 bytes.
- Chinese and other normal Unicode text are supported.
- Invalid Unicode surrogate code points in incoming MCP arguments are replaced
  with U+FFFD instead of causing `UnicodeEncodeError`.
- Newlines are preserved as provided; the server does not perform Windows text
  newline conversion during writes.
- Reads use UTF-8 with replacement for invalid byte sequences, so a bad byte does
  not crash the tool.

Tools:

- `read_file`
- `read_files`
- `write_file`
- `write_files`
- `edit_file`
- `search_files`
- `create_directory`
- `file_info`
- `check_status`

After changing `server.py`, restart the MCP server or restart Codex so the active
MCP process loads the new implementation.
