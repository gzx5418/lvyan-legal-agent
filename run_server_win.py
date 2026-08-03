"""Windows 专用启动脚本：强制 SelectorEventLoop 以兼容 psycopg async。

Windows 默认用 ProactorEventLoop，它不支持 ``loop.add_reader``，
而 psycopg 的 async 模式依赖此方法。必须用 SelectorEventLoop。
"""
import asyncio
import selectors
import sys

if sys.platform == "win32":
    _loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    asyncio.set_event_loop(_loop)

import uvicorn

config = uvicorn.Config(
    "lvyan.api.server:create_app",
    factory=True,
    port=8000,
    loop="asyncio",
)
server = uvicorn.Server(config)
_loop.run_until_complete(server.serve())
