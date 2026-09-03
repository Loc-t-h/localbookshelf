"""Drive headless Chrome over CDP to screenshot real UI states.

    uv run python tests/shoot.py <url> <out.png> [--theme light|dark] [--click SELECTOR]

Used to check the shelf and the detail panel in both themes without needing a
browser automation dependency.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9222


async def shoot(url: str, out: str, theme: str | None, click: str | None, height: int) -> None:
    import websockets

    proc = subprocess.Popen(
        [
            CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
            f"--remote-debugging-port={PORT}", "--user-data-dir=/tmp/chrome-shoot",
            f"--window-size=1280,{height}", "--force-device-scale-factor=2",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json") as response:
                    tabs = json.load(response)
                target = next((t for t in tabs if t["type"] == "page"), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not target:
            raise RuntimeError("Chrome did not start")

        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=64 * 2**20) as ws:
            counter = [0]

            async def send(method, params=None):
                counter[0] += 1
                await ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == counter[0]:
                        return message.get("result", {})

            await send("Page.enable")
            await send("Runtime.enable")
            if theme:
                await send("Emulation.setEmulatedMedia", {
                    "features": [{"name": "prefers-color-scheme", "value": theme}]
                })
            await send("Page.navigate", {"url": url})
            await asyncio.sleep(2.5)
            if click:
                await send("Runtime.evaluate", {
                    "expression": f"document.querySelector({click!r}).click()"
                })
                await asyncio.sleep(1.5)
            result = await send("Page.captureScreenshot", {"format": "png"})
            with open(out, "wb") as handle:
                handle.write(base64.b64decode(result["data"]))
            print(f"wrote {out}")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    args = sys.argv[1:]
    url, out = args[0], args[1]
    theme = args[args.index("--theme") + 1] if "--theme" in args else None
    click = args[args.index("--click") + 1] if "--click" in args else None
    height = int(args[args.index("--height") + 1]) if "--height" in args else 900
    asyncio.run(shoot(url, out, theme, click, height))
