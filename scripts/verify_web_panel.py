"""Verify the installed panel bundle and the state contract on its local port."""
from __future__ import annotations

from html.parser import HTMLParser
from http.client import HTTPConnection
import json
from pathlib import Path


class BundleReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: set[str] = set()

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "script" or (tag == "link" and fields.get("rel") == "stylesheet"):
            path = fields.get("src" if tag == "script" else "href") or ""
            if path.startswith("/assets/"):
                self.assets.add(path)


def verify(dist: Path, port: int = 8765) -> None:
    def read(path: str) -> bytes:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            if response.status != 200:
                raise ValueError(f"{path} returned HTTP {response.status}")
            return response.read()
        finally:
            conn.close()

    index = (dist / "index.html").read_bytes()
    if not index or read("/") != index:
        raise ValueError("installed index differs from the running panel")
    refs = BundleReferences()
    refs.feed(index.decode("utf-8"))
    if not any(asset.endswith(".js") for asset in refs.assets):
        raise ValueError("compiled JavaScript bundle is missing")
    for asset in refs.assets:
        path = dist / asset.lstrip("/")
        if not path.is_file() or read(asset) != path.read_bytes():
            raise ValueError(f"running panel does not serve {asset}")
    state = json.loads(read("/api/state"))
    if (not isinstance(state, dict) or state.get("schema_version") != 2
            or not isinstance(state.get("runtime_state"), dict)
            or state.get("actions", {}).get("endpoints", {}).get("state") != "/api/state"):
        raise ValueError("unexpected lecture-stt state response")


if __name__ == "__main__":
    verify(Path(__file__).resolve().parents[1] / "frontend" / "web-panel" / "dist")
