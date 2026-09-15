"""Actual MCP protocol check; offline by default, optional user-only local opt-in.

The --run option requires independent authorization confirmation, not just an
API Key. No article/PDF retrieval, no Zotero writes, no credential persistence.
"""
import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_DOI = "10.1016/j.still.2022.105374"

def masked_input(prompt, read_char=None, stream=None):
    """Windows console input: emit only stars; never fall back to echoed input."""
    if stream is None:
        stream = sys.stdout
    if read_char is None:
        if os.name != "nt" or not sys.stdin.isatty() or not stream.isatty():
            raise RuntimeError("需要本机 Windows 交互终端；不会降级为明文输入。")
        import msvcrt
        read_char = msvcrt.getwch
    stream.write(prompt)
    stream.flush()
    characters = []
    try:
        while True:
            character = read_char()
            if character in ("\r", "\n"):
                stream.write("\n")
                stream.flush()
                return "".join(characters)
            if character == "\x03":
                raise KeyboardInterrupt
            if character in ("\x04", "\x1a", "\x1b"):
                raise EOFError
            if character in ("\x00", "\xe0"):
                read_char()  # Discard Windows extended-key scan code.
                continue
            if character in ("\b", "\x7f"):
                if characters:
                    characters.pop()
                    stream.write("\b \b")
                    stream.flush()
                continue
            if len(character) != 1 or not 32 <= ord(character) <= 126:
                raise ValueError("输入包含不支持的字符；请重新复制密钥。")
            if len(characters) >= 512:
                raise ValueError("输入超出安全长度限制；请检查粘贴内容。")
            characters.append(character)
            stream.write("*")
            stream.flush()
    finally:
        characters.clear()


def confirmed_api_key(read_secret=None):
    """Confirm exact inputs without printing, persisting or normalizing the key."""
    if read_secret is None:
        read_secret = masked_input
    first = read_secret("输入 API Key（仅显示 *，不保存）：")
    if not first:
        raise ValueError("未输入密钥，检测已取消。")
    if (len(first) > 512 or any(not 33 <= ord(c) <= 126 for c in first)
            or '"' in first or "'" in first):
        raise ValueError("密钥包含空格、引号或异常字符；检测已取消，请重新复制。")
    second = read_secret("再次输入同一 API Key（仅显示 *）：")
    if not second.isascii() or not secrets.compare_digest(first, second):
        raise ValueError("两次输入不一致；检测已取消，未请求 API。")
    return first


async def main(live=False, doi=DEFAULT_DOI):
    from elsevier_api import _doi
    if _doi(doi) is None:
        print('{"completed":false,"code":"invalid_doi","api_requests":0}')
        return
    env = dict(os.environ)
    env.pop("ELSEVIER_API_KEY", None)
    env.pop("ELSEVIER_MCP_DIAGNOSTICS_APPROVED", None)
    if live:
        print("仅在已确认 Elsevier/学校授权当前 MCP/AI 只读检测用途时继续；此确认不替代授权。")
        if input("已确认获准，输入 APPROVED；否则按 Enter 退出：").strip() != "APPROVED":
            print('{"completed":false,"code":"use_authorization_unconfirmed","api_requests":0}')
            return
        try:
            entered_value = confirmed_api_key()
        except (ValueError, RuntimeError) as error:
            print(str(error))  # These errors contain only fixed safe messages.
            print('{"completed":false,"code":"key_input_not_confirmed","api_requests":0}')
            return
        except (KeyboardInterrupt, EOFError):
            print('\n{"completed":false,"code":"key_input_cancelled","api_requests":0}')
            return
        print("两次输入一致，输入格式检查通过；这不代表 API Key 已获服务器验证。")
        env["ELSEVIER_API_KEY"] = entered_value
        env["ELSEVIER_MCP_DIAGNOSTICS_APPROVED"] = "yes"
        del entered_value
    params = StdioServerParameters(command=str(Path(sys.executable).parent / "Scripts" / "mcp.exe"), args=["run", str(ROOT / "server.py")], cwd=ROOT, env=env)
    output = {"protocol": "official MCP ClientSession over stdio", "live_opt_in": live, "pdf_downloads": 0, "zotero_writes": 0}
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams, read_timeout_seconds=90) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                names = ("elsevier_api_status", "check_elsevier_entitlement")
                assert all(n in tools for n in names)
                output["tools_registered"] = list(names)
                output["read_only_annotations"] = {n: tools[n].annotations.read_only_hint for n in names}
                assert all(output["read_only_annotations"].values())
                async def call(name, args):
                    r = await session.call_tool(name, args)
                    return r.structured_content if r.structured_content is not None else json.loads(next(c.text for c in r.content if c.type == "text"))
                output["configuration"] = await call("elsevier_api_status", {})
                output["checks"] = []
                for doi in [doi]:
                    result = await call("check_elsevier_entitlement", {"doi": doi})
                    output["checks"].append(result)
                    if not live:
                        assert result["code"] == "use_authorization_unconfirmed"
                    elif result["code"] in ("rate_limited", "cooldown_active", "authentication_failed", "unexpected_content_type_not_read", "redirect_not_followed"):
                        break
                output["final_state"] = await call("elsevier_api_status", {})
                if not live:
                    assert output["final_state"]["requests_this_process"] == 0
                output["completed"] = True
    finally:
        env.pop("ELSEVIER_API_KEY", None)
        env.pop("ELSEVIER_MCP_DIAGNOSTICS_APPROVED", None)
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Elsevier read-only MCP protocol diagnostic; no API calls by default")
    parser.add_argument("--run", action="store_true", help="User-local interactive authorized API check, never full text")
    parser.add_argument("--doi", default=DEFAULT_DOI, help="One DOI; choose an article you can download manually")
    args = parser.parse_args()
    asyncio.run(main(args.run, args.doi))
