#!/usr/bin/env python3
"""FrontierPulse browser-MCP backend for the logged-in Grok web app.

This replaces only the data-acquisition step in ai_digest.py.  Digest
normalization, same-day merging, model tracking, index generation and git push
remain owned by ai_digest.py, so the public JSON schema does not change.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ai_digest as ed


MCP_BASE = os.environ.get("FRONTIERPULSE_MCP_BASE", "http://127.0.0.1:3457").rstrip("/")
LOOKBACK_HOURS = ed.env_int("WEBGROK_LOOKBACK_HOURS", 12)
GEN_TIMEOUT = ed.env_int("WEBGROK_GEN_TIMEOUT", 300)
REUSE_TAB = os.environ.get("WEBGROK_REUSE_TAB", "1") != "0"
GROK_URL = "https://grok.com/"
EDITOR_SEL = ".tiptap.ProseMirror"
SUBMIT_SEL = "button[data-testid='chat-submit']"


class MCP:
    """Minimal MCP-over-SSE client for the resident browser MCP daemon."""

    def __init__(self, base: str = MCP_BASE):
        self.base = base
        self.endpoint = None
        self.responses: dict[int, dict] = {}
        self.events: dict[int, threading.Event] = {}
        self.ready = threading.Event()
        self.next_id = 1
        self.lock = threading.Lock()
        self.sse_error = None
        threading.Thread(target=self._read_sse, daemon=True).start()

    def _read_sse(self):
        try:
            req = urllib.request.Request(self.base + "/sse", headers={"Accept": "text/event-stream"})
            response = urllib.request.urlopen(req, timeout=900)
        except Exception as exc:
            self.sse_error = exc
            self.ready.set()
            return

        event, data = None, []
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if event == "endpoint" and data:
                    endpoint = data[0]
                    self.endpoint = self.base + endpoint if endpoint.startswith("/") else endpoint
                    self.ready.set()
                elif data:
                    try:
                        message = json.loads(data[0])
                        message_id = message.get("id")
                        if message_id is not None:
                            self.responses[message_id] = message
                            if message_id in self.events:
                                self.events[message_id].set()
                    except Exception:
                        pass
                event, data = None, []
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())

    def _post(self, payload: dict):
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=30).read()

    def _rpc(self, method: str, params: dict, timeout: int = 120):
        with self.lock:
            message_id = self.next_id
            self.next_id += 1
        event = threading.Event()
        self.events[message_id] = event
        self._post({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        if not event.wait(timeout):
            raise TimeoutError(f"{method} timed out")
        message = self.responses.pop(message_id, {})
        if "error" in message:
            raise RuntimeError(message["error"])
        return message.get("result")

    def connect(self):
        if not self.ready.wait(10):
            raise RuntimeError("browser MCP daemon did not return an SSE endpoint")
        if self.sse_error is not None:
            raise RuntimeError(f"cannot connect to browser MCP daemon {self.base}: {self.sse_error!r}")
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "frontierpulse-webgrok", "version": "1.0"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, name: str, arguments: dict, timeout: int = 120):
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if isinstance(result, dict) and "content" in result:
            text = "\n".join(
                part.get("text", "") for part in result["content"] if part.get("type") == "text"
            )
            return text, result.get("isError", False)
        return json.dumps(result), False


def unwrap(value):
    try:
        parsed = json.loads(value)
        while isinstance(parsed, dict) and "result" in parsed:
            parsed = parsed["result"]
        return parsed
    except Exception:
        return value


class GrokTab:
    """Drive one grok.com chat tab through browser MCP."""

    def __init__(self, mcp: MCP):
        self.mcp = mcp
        self.tab_id = None

    def _find_existing(self):
        output, _ = self.mcp.call("browser_list_tabs", {}, timeout=30)
        try:
            data = json.loads(output)
        except Exception:
            return None
        tabs = data.get("tabs", data) if isinstance(data, dict) else data
        if not isinstance(tabs, list):
            return None
        for tab in tabs:
            url = tab.get("url", "") if isinstance(tab, dict) else ""
            if "grok.com" in url and "/imagine" not in url and "/project" not in url:
                return tab.get("tabId") or tab.get("id")
        return None

    def _create_tab(self):
        output, _ = self.mcp.call("browser_create_tab", {"url": GROK_URL}, timeout=60)
        try:
            data = json.loads(output)
            self.tab_id = data.get("tabId") or data.get("id")
        except Exception:
            match = re.search(r'"(?:tabId|id)"\s*:\s*(\d+)', output)
            self.tab_id = int(match.group(1)) if match else None
        if self.tab_id is None:
            raise RuntimeError(f"could not create grok.com tab: {output[:180]}")
        time.sleep(2)
        return self.tab_id

    def open(self):
        if REUSE_TAB:
            self.tab_id = self._find_existing()
            if self.tab_id is not None:
                try:
                    self.mcp.call("browser_switch_tab", {"tabId": self.tab_id}, timeout=30)
                    self.mcp.call("browser_navigate", {"url": GROK_URL, "tabId": self.tab_id}, timeout=60)
                except Exception:
                    pass
                time.sleep(2)
                print(f"[webgrok] reused grok.com tab {self.tab_id} and opened a fresh chat")
                return self.tab_id
        tab_id = self._create_tab()
        print(f"[webgrok] opened grok.com tab {tab_id}")
        return tab_id

    def open_fresh(self):
        tab_id = self._create_tab()
        print(f"[webgrok] opened fallback grok.com tab {tab_id}")
        return tab_id

    def _eval(self, code: str, timeout: int = 30):
        output, _ = self.mcp.call(
            "browser_eval_js_cdp", {"code": code, "tabId": self.tab_id}, timeout=timeout
        )
        return output

    def _page_info(self):
        output, _ = self.mcp.call("browser_get_page_info", {"tabId": self.tab_id}, timeout=30)
        try:
            return json.loads(output)
        except Exception:
            return {}

    def wait_editor(self, timeout: int = 45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            output = self._eval(f"(() => !!document.querySelector('{EDITOR_SEL}'))()")
            if "true" in str(unwrap(output)).lower():
                return True
            time.sleep(2)
        return False

    def _editor_empty(self):
        output = self._eval(
            f"(() => {{const e=document.querySelector('{EDITOR_SEL}');"
            "return e?(e.innerText||'').trim():'';}})()"
        )
        value = unwrap(output)
        return (value if isinstance(value, str) else str(value)) == ""

    def _generating(self):
        output = self._eval(
            "(() => Array.from(document.querySelectorAll('button[aria-label]'))"
            ".some(b => /停止|stop/i.test(b.getAttribute('aria-label')||'')))()"
        )
        return "true" in str(unwrap(output)).lower()

    def send(self, prompt: str):
        for _ in range(30):
            if not self._generating():
                break
            time.sleep(1)
        self.mcp.call(
            "browser_click",
            {"selector": EDITOR_SEL, "tabId": self.tab_id, "humanLike": True},
            timeout=30,
        )
        self.mcp.call(
            "browser_fill",
            {"selector": EDITOR_SEL, "value": prompt, "clear": False, "tabId": self.tab_id},
            timeout=180,
        )
        deadline = time.time() + 12
        while time.time() < deadline and self._editor_empty():
            time.sleep(0.5)
        time.sleep(0.6)
        url_before = self._page_info().get("url", "")
        for _ in range(3):
            self.mcp.call(
                "browser_click",
                {"selector": SUBMIT_SEL, "tabId": self.tab_id, "wait_after": 1500},
                timeout=30,
            )
            for _ in range(6):
                time.sleep(1)
                url = self._page_info().get("url", "")
                if "/c/" in url and url != url_before:
                    return True
            if self._editor_empty():
                self.mcp.call(
                    "browser_fill",
                    {"selector": EDITOR_SEL, "value": prompt, "clear": False, "tabId": self.tab_id},
                    timeout=180,
                )
        return "/c/" in self._page_info().get("url", "")

    GRAB_JS = r"""
(() => {
  const blocks = Array.from(document.querySelectorAll('pre code, pre'))
    .map(el => (el.innerText || '').trim()).filter(Boolean);
  let best = '';
  for (const block of blocks) {
    if (block.includes('{') && block.includes('}') && block.length > best.length) best = block;
  }
  return best || (blocks.length ? blocks[blocks.length - 1] : '');
})()
"""

    def grab_codeblock(self):
        direct = unwrap(self._eval(self.GRAB_JS))
        if isinstance(direct, str) and direct.strip():
            return direct
        sentinel = "__FP_GRAB__:"
        self._eval(
            "(() => {try {const r=(" + self.GRAB_JS + ");document.title='"
            + sentinel
            + "'+encodeURIComponent(r||'');}catch(e){document.title='"
            + sentinel
            + "ERR';}return true;})()"
        )
        time.sleep(0.5)
        title = self._page_info().get("title", "")
        if sentinel in title:
            return urllib.parse.unquote(title.split(sentinel, 1)[1])
        return ""

    def wait_reply(self, timeout: int = GEN_TIMEOUT):
        deadline = time.time() + timeout
        last, stable = "", 0
        time.sleep(5)
        while time.time() < deadline:
            generating = self._generating()
            block = self.grab_codeblock() if not generating else ""
            if block and block == last and not generating:
                stable += 1
                if stable >= 2:
                    return block
            else:
                stable = 0
            if block:
                last = block
            time.sleep(4)
        return self.grab_codeblock() or last


def build_web_prompt(date_str: str, run_iso: str, existing_items: list):
    prompt = ed.build_prompt(
        date_str,
        run_iso,
        existing_items,
        lookback_hours=LOOKBACK_HOURS,
        existing_limit=25,
    )
    prompt = prompt.replace(
        "Output STRICT JSON only — no markdown fences, no commentary before or after.",
        "Output the STRICT JSON object inside ONE ```json fenced code block, with nothing before or after it.",
    )
    return prompt


def call_grok_web(date_str: str, run_iso: str, existing_items: list):
    mcp = MCP()
    mcp.connect()
    print(f"[webgrok] connected to browser MCP daemon {MCP_BASE}")
    tab = GrokTab(mcp)
    tab.open()
    if not tab.wait_editor():
        tab.open_fresh()
        if not tab.wait_editor():
            raise RuntimeError("grok.com editor is unavailable; confirm the browser is open and logged in")

    prompt = build_web_prompt(date_str, run_iso, existing_items)
    print(
        f"[webgrok] submitting {len(prompt)} characters; lookback={LOOKBACK_HOURS}h; "
        f"existing={len(existing_items)}"
    )
    if not tab.send(prompt):
        tab.open_fresh()
        if not tab.wait_editor() or not tab.send(prompt):
            raise RuntimeError("failed to submit the FrontierPulse prompt to grok.com")

    block = tab.wait_reply()
    if not block:
        raise RuntimeError(f"no Grok response within {GEN_TIMEOUT}s")
    data = ed.extract_json_object(block)
    if not isinstance(data.get("items"), list) or not isinstance(data.get("model_releases"), list):
        raise ValueError("Grok response does not match the FrontierPulse schema")
    data["_usage"] = {
        "backend": "grok-web",
        "model": "grok.com (browser)",
        "lookback_hours": LOOKBACK_HOURS,
        "item_count": len(data["items"]),
    }
    print(f"[webgrok] parsed {len(data['items'])} digest items")
    return data


def main():
    repo = Path(os.environ.get("REPO_DIR", "") or Path(__file__).resolve().parent.parent)
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.astimezone(ed.TAIPEI).strftime("%Y-%m-%d")
    run_iso = now_utc.isoformat()
    print(f"[run] {run_iso} date={date_str} backend=webgrok repo={repo}")

    existing = ed.load_today_items(repo, date_str)
    digest = call_grok_web(date_str, run_iso, existing)
    path = ed.write_digest(repo, date_str, run_iso, digest)
    added = ed.update_model_tracker(repo, run_iso, digest.get("model_releases", []))
    ed.rebuild_index(repo, run_iso)
    print(
        f"[ok] wrote {path.name}: {len(ed.load_public(path).get('items_flat', []))} items, "
        f"{added} new model release(s)"
    )
    if os.environ.get("NO_PUSH", "") != "1":
        ed.git_commit_push(repo, date_str)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[fatal] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
