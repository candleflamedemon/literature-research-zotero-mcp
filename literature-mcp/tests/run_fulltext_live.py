"""Explicit 3-paper MCP stdio smoke test. No real Zotero writes.

Run with --run. Fetches at most two OA PDFs; third paper is classification only.
The configured local mcp executable is used, not direct Python tool functions.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROJECT = Path(__file__).resolve().parents[1]
DOIS = ["10.3390/rs16213953", "10.1038/s41597-024-02998-7", "10.1016/j.biosystemseng.2026.104418"]
TOOLS = {"find_oa_fulltext", "classify_fulltext_access", "fetch_oa_pdf", "fetch_institutional_pdf", "queue_browser_access", "attach_pdf_to_zotero", "acquisition_report"}


async def main():
    parameters = StdioServerParameters(command=str(Path(sys.executable).parent/"Scripts"/"mcp.exe"), args=["run", str(PROJECT/"server.py")], cwd=PROJECT, env=dict(os.environ))
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=300) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert TOOLS <= {t.name for t in listed.tools}
            async def call(name, args):
                result = await session.call_tool(name, args)
                assert not result.is_error, result
                payload = result.structured_content
                if payload is None:
                    try:
                        payload = json.loads(result.content[0].text)
                    except ValueError:
                        payload = {"result": result.content[0].text}
                print(json.dumps({"tool": name, "result": payload}, ensure_ascii=False), flush=True)
                return payload
            await call("ping", {})
            for index, doi in enumerate(DOIS):
                await call("classify_fulltext_access", {"doi": doi})
                if index < 2 and sys.argv[1:] == ["--run"]:
                    await call("fetch_oa_pdf", {"doi": doi})
                await asyncio.sleep(2)
            report = await call("acquisition_report", {})
            assert report["count"] >= 3
            # No write or browser-cookie tool exists in this test path.
            print("LIVE_TEST_COMPLETED: 3 papers, <=2 OA fetches (--verify-only: 0), 0 Zotero writes", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] not in (["--run"], ["--verify-only"]):
        raise SystemExit("Explicit opt-in required: --run or --verify-only")
    asyncio.run(main())
