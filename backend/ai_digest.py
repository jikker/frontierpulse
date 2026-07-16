#!/usr/bin/env python3
"""FrontierPulse — AI 大廠動態聚合後端

每 N 小時執行一次：
  Grok（CLI 訂閱或 xAI API）live search 讀各 AI 大廠官方消息
  → 產生雙語（EN + 繁中）結構化 digest JSON
  → 合併當日既有條目（同故事演進式更新，不重複）
  → 維護 models.json（模型發布追蹤器，累積式）
  → rebuild index.json / latest.json
  → git commit + push 到公開 repo（GitHub Pages 供 App 直讀）

環境變數：
  DIGEST_BACKEND        cli（預設，吃 Grok 訂閱免 key）或 api（需 XAI_API_KEY）
  GROK_BIN              選填，Grok CLI 路徑（預設自動找 PATH / ~/.grok/bin/grok）
  GROK_TIMEOUT          CLI 逾時秒數（預設 1800）
  GROK_MODEL            選填，CLI/API 模型（api 預設 grok-4-fast）
  XAI_API_KEY           api 後端必填
  FRONTIERPULSE_ENC_KEY 選填，64 hex＝32 bytes。設了就把公開 JSON 以
                        AES-256-GCM 加密成信封（同 MarsRadar 機制）；沒設寫明文。
  REPO_DIR              公開 repo 根目錄（預設：本檔上一層）
  NO_PUSH=1             只寫檔不 git push（測試用）
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

TAIPEI = timezone(timedelta(hours=8))
DIGEST_BACKEND = os.environ.get("DIGEST_BACKEND", "cli").strip().lower()
ENC_ENVELOPE_TAG = "frontierpulse_enc"

# 監控的「實驗室」＝App 的六大分類。official_handles 供 Grok 佐證用，
# 產品核心是「官方公告 + 模型發布」而非某個人的 X 帳號（與 MarsRadar 定位不同）。
LABS = {
    "openai":    {"name": "OpenAI",          "sources": ["openai.com/news", "@OpenAI", "@sama"]},
    "anthropic": {"name": "Anthropic",       "sources": ["anthropic.com/news", "@AnthropicAI"]},
    "google":    {"name": "Google DeepMind", "sources": ["deepmind.google", "blog.google", "@GoogleDeepMind", "@GoogleAI"]},
    "meta":      {"name": "Meta AI",         "sources": ["ai.meta.com", "@AIatMeta"]},
    "xai":       {"name": "xAI",             "sources": ["x.ai/news", "@xai"]},
    "frontier_other": {"name": "Open source & more",
                       "sources": ["mistral.ai", "huggingface.co/blog", "@MistralAI", "@nvidia", "@MSFTResearch"]},
}

CATEGORIES = list(LABS.keys())
SOURCE_TYPES = {"official_blog", "official_x", "paper", "product", "news", "mixed"}


def env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


# ----------------------------------------------------------- Prompt ----

SYSTEM_RULES = """You are FrontierPulse's editor. You produce a concise, accurate, bilingual
(English + Traditional Chinese / 繁體中文) digest of what the major frontier AI labs actually
announced and shipped: OpenAI, Anthropic, Google DeepMind / Google AI, Meta AI, xAI, plus
notable open-source / other players (Mistral, NVIDIA, Microsoft, Hugging Face...).

SOURCE PRIORITY (strict — this is the core of the product):
1) PRIMARY = the labs' OWN official channels in the window: official blogs / news pages,
   official X accounts, research paper releases, product changelogs, developer docs.
   Lead with "what the lab announced/shipped", in their own words when quotable.
2) SECONDARY = reputable tech press (Reuters, Bloomberg, The Verge, TechCrunch...) for
   corroboration, context, or industry stories involving these labs (funding, lawsuits,
   policy, talent moves). Never let a rumor outrank an official announcement.

ANTI-HALLUCINATION:
- Use ONLY facts found via live web/X reading. Do NOT invent announcements, quotes, model
  names, benchmark numbers, or URLs.
- Every link must be a real URL you actually retrieved; prefer the official announcement URL.
- NEVER copy a full article. Paraphrase in your OWN 1-3 sentences (fair use).
- Respect timestamps: only include items from the stated lookback window; do NOT resurface
  old announcements as if new. If you cannot verify a claim or URL, omit it.

SAME-DAY STORY MERGE (critical — avoid duplicates):
- You will receive EXISTING_ITEMS for today (UTC+8 / Taipei calendar day). Treat them as the
  canonical list so far.
- ONE real-world story = ONE item for the whole day. When new info arrives, UPDATE the matching
  item in place (keep its story_id, preserve first_seen, refresh summaries/importance/links/
  quote/updated_at). Only create a NEW item if it is genuinely a different story.
- Do NOT delete an existing item unless it was proven false. Return the FULL merged day list.
- Rebuild brief_en / brief_zh from the merged full-day picture.

MODEL RELEASE TRACKER (second product surface — report separately):
- Additionally report any NEW AI model / major model-version release or deprecation you saw in
  the window (any of the tracked labs; include open-weights releases). A "model release" =
  a named model or versioned update (e.g. a new GPT/Claude/Gemini/Llama/Grok/Mistral model),
  not a minor UI feature.
- If none in the window, return an empty model_releases list. Never invent releases.

OUTPUT:
- Categorize each item by lab: openai | anthropic | google | meta | xai | frontier_other
  (cross-lab industry stories go to the lab most central to the story, or frontier_other).
- Rate importance 1 (minor) to 5 (major); a flagship model launch = 5.
- Bilingual title + summary for every item.
- Add a clearly separated bilingual editorial take for every item. The take must explain why
  the development matters, what changes, or what remains uncertain. It is FrontierPulse's
  analysis, not a second summary: 1-2 concise sentences, evidence-based, no hype, no investment
  advice, and never present inference as confirmed fact.
- Top-level one-minute brief in EN and ZH（一分鐘看懂今日 AI 大廠動態）."""

SCHEMA_BLOCK = """Return STRICT JSON with EXACTLY this shape:
{{
  "brief_en": "<=60 words, full-day one-minute brief across all labs",
  "brief_zh": "<=60字 一分鐘看懂今日AI大廠動態（整日合併後）",
  "items": [
    {{
      "story_id": "stable-lowercase-kebab-id (e.g. openai-gpt6-launch-2026-07-13)",
      "category": "openai|anthropic|google|meta|xai|frontier_other",
      "source_type": "official_blog|official_x|paper|product|news|mixed",
      "title_en": "...",
      "title_zh": "...",
      "summary_en": "1-3 sentences, your own words; lead with what the lab announced",
      "summary_zh": "1-3 句，用你自己的話；先寫該實驗室宣布了什麼",
      "analysis_en": "1-2 sentences of original FrontierPulse analysis: why it matters / what changes / key uncertainty",
      "analysis_zh": "1-2 句 FrontierPulse 原創觀點：為何重要／改變什麼／關鍵不確定性",
      "quote": "short verbatim excerpt from the official announcement/exec post (<=280 chars); empty string if none",
      "quote_zh": "官方原話的繁中翻譯；無則空字串",
      "importance": 1,
      "first_seen": "ISO-8601 UTC — PRESERVE from the existing item if you are updating it",
      "updated_at": "ISO-8601 UTC — now",
      "links": [
        {{"label": "Exact source label, e.g. OpenAI Blog / Sam Altman on X / Reuters",
          "url": "https://..."}}
      ]
    }}
  ],
  "model_releases": [
    {{
      "model_id": "stable-kebab-id, e.g. claude-fable-5",
      "name": "official model name",
      "lab": "openai|anthropic|google|meta|xai|frontier_other",
      "kind": "model|update|open_weights|deprecation",
      "released": "YYYY-MM-DD",
      "note_en": "one line: what it is / what changed",
      "note_zh": "一句話：這是什麼／改了什麼",
      "link": "https://official announcement url"
    }}
  ]
}}
Rules:
- links[0] must be the PRIMARY source (official announcement when available).
- items = the FULL merged list for today AFTER applying EXISTING_ITEMS (not a delta).
- Aim for 6-15 high-signal items for the whole day across all labs. Drop low-value noise.
- model_releases: ONLY releases actually announced in the window (usually 0-3; empty is normal)."""

USER_TEMPLATE = """Today is {date} (UTC+8 / Taipei time). Current run time (UTC): {run_iso}.

TASK: Produce today's FrontierPulse digest by MERGING new developments from the LAST
{lookback_hours} HOURS into any existing items for this calendar day (UTC+8 / Taipei).

=== EXISTING_ITEMS (today so far — UPDATE/MERGE these in place, do NOT duplicate stories) ===
{existing_items_json}

=== LABS & OFFICIAL SOURCES TO MONITOR ===
{labs_block}

=== WHAT TO PRIORITIZE ===
1. Official announcements: new models, APIs, products, pricing, research papers, safety/policy posts.
2. Executive posts that move the story (Altman / Amodei / Hassabis / LeCun / Musk on their labs).
3. Major industry stories involving these labs: funding, lawsuits, regulation, talent moves.
4. Skip: influencer hot takes, speculation threads, minor UI tweaks, old news resurfacing.

""" + SCHEMA_BLOCK


def _labs_block() -> str:
    lines = []
    for key, lab in LABS.items():
        lines.append(f"- {lab['name']} (category `{key}`): " + ", ".join(lab["sources"]))
    return "\n".join(lines)


def _compact_existing(items: list, limit: int = 25) -> list:
    """精簡今日既有條目再餵給 Grok：只留辨識同故事所需欄位，控制 prompt 大小。"""
    ranked = sorted(items, key=lambda x: -int(x.get("importance", 3)))[:limit]
    return [{
        "story_id": it.get("story_id") or it.get("id"),
        "category": it.get("category"),
        "importance": it.get("importance", 3),
        "title_en": it.get("title_en", ""),
        "summary_en": (it.get("summary_en", "") or "")[:240],
        "analysis_en": (it.get("analysis_en", "") or "")[:180],
        "first_seen": it.get("first_seen", ""),
    } for it in ranked]


def build_prompt(date_str: str, run_iso: str = "", existing_items: list | None = None,
                 lookback_hours: int = 12, existing_limit: int = 25) -> str:
    existing_json = (json.dumps(_compact_existing(existing_items, limit=existing_limit),
                                ensure_ascii=False, indent=1)
                     if existing_items else "[]")
    user = USER_TEMPLATE.format(date=date_str, run_iso=run_iso or date_str,
                                lookback_hours=lookback_hours,
                                existing_items_json=existing_json,
                                labs_block=_labs_block())
    return (SYSTEM_RULES + "\n\n" + user
            + "\n\nOutput STRICT JSON only — no markdown fences, no commentary before or after.")


def extract_json_object(text: str) -> dict:
    """從可能含前後雜訊（preamble、```fence）的文字中抽出第一個完整 JSON 物件。"""
    text = text.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.startswith("json"):
            text = text[4:]
        if text.startswith("\n"):
            text = text[1:]
        end_fence = text.rfind("```")
        if end_fence != -1:
            text = text[:end_fence]
        text = text.strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("回應中找不到 JSON 物件起始 '{'")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("回應中的 JSON 物件未正確閉合")


# ------------------------------------------------------ Grok CLI 後端 ----

def _resolve_grok_bin() -> str:
    cand = os.environ.get("GROK_BIN", "").strip()
    if cand and Path(cand).exists():
        return cand
    found = shutil.which("grok")
    if found:
        return found
    home_bin = Path.home() / ".grok" / "bin" / "grok"
    if home_bin.exists():
        return str(home_bin)
    raise RuntimeError(
        "找不到 Grok CLI。請先安裝（curl -fsSL https://x.ai/cli/install.sh | bash）"
        "或設定環境變數 GROK_BIN=/path/to/grok。")


def call_grok_cli(date_str: str, run_iso: str = "", existing_items: list | None = None) -> dict:
    """呼叫本機 Grok Build CLI（headless）。逾時自動縮小 prompt 降級重試。"""
    grok_bin = _resolve_grok_bin()
    timeout = env_int("GROK_TIMEOUT", 1800)
    model = os.environ.get("GROK_MODEL", "").strip()
    attempts = [(12, 25, timeout), (6, 12, timeout), (3, 6, timeout)]

    timeout_notes = []
    for attempt_no, (lookback_hours, existing_limit, attempt_timeout) in enumerate(attempts, 1):
        prompt = build_prompt(date_str, run_iso, existing_items,
                              lookback_hours=lookback_hours, existing_limit=existing_limit)
        # CLI 會載入 cwd 的 .mcp.json（含需 OAuth 的 server 會卡死）→ 用乾淨臨時目錄當 cwd。
        workdir = tempfile.mkdtemp(prefix="frontierpulse_grok_")
        prompt_file = Path(workdir) / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        cmd = [grok_bin, "--cwd", workdir, "--always-approve",
               "--output-format", "json", "--prompt-file", str(prompt_file)]
        if model:
            cmd += ["--model", model]
        try:
            proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                                  timeout=attempt_timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            note = (f"attempt {attempt_no} timeout: lookback={lookback_hours}h "
                    f"existing_limit={existing_limit} timeout={attempt_timeout}s")
            timeout_notes.append(note)
            print(f"[grok] {note}")
            continue
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if proc.returncode != 0:
            raise RuntimeError(
                f"Grok CLI 失敗 (exit {proc.returncode})：{(proc.stderr or proc.stdout)[:500]}")
        try:
            envelope = json.loads(proc.stdout.strip())
            inner_text = envelope.get("text", "")
        except json.JSONDecodeError:
            envelope = {}
            inner_text = proc.stdout
        data = extract_json_object(inner_text)
        data["_usage"] = {
            "backend": "grok-cli", "model": model or "grok-build",
            "requestId": envelope.get("requestId", ""),
            "attempt": attempt_no, "lookback_hours": lookback_hours,
            "timeout_notes": timeout_notes,
        }
        return data
    raise RuntimeError("Grok CLI 逾時；已嘗試降級重試仍未完成：" + " | ".join(timeout_notes))


def call_grok_api(date_str: str, run_iso: str = "", existing_items: list | None = None) -> dict:
    """xAI REST API 後端（GitHub Actions 雲端排程用；需 XAI_API_KEY）。"""
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DIGEST_BACKEND=api 需要環境變數 XAI_API_KEY")
    model = os.environ.get("GROK_MODEL", "grok-4-fast").strip()
    user_msg = USER_TEMPLATE.format(date=date_str, run_iso=run_iso or date_str,
                                    lookback_hours=12,
                                    existing_items_json=json.dumps(
                                        _compact_existing(existing_items or []),
                                        ensure_ascii=False),
                                    labs_block=_labs_block())
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": user_msg},
        ],
        "search_parameters": {
            "mode": "on",
            "max_search_results": 25,
            "return_citations": True,
        },
    }
    req = Request("https://api.x.ai/v1/chat/completions",
                  data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {api_key}"})
    with urlopen(req, timeout=600) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    data = extract_json_object(content)
    data["_usage"] = {"backend": "xai-api", "model": model,
                      "usage": payload.get("usage", {})}
    return data


def call_grok(date_str: str, run_iso: str = "", existing_items: list | None = None) -> dict:
    if DIGEST_BACKEND == "api":
        return call_grok_api(date_str, run_iso, existing_items)
    return call_grok_cli(date_str, run_iso, existing_items)


# ------------------------------------------------------ 公開 JSON 加密 ----

def _enc_key() -> bytes | None:
    h = os.environ.get("FRONTIERPULSE_ENC_KEY", "").strip()
    if not h:
        return None
    key = bytes.fromhex(h)
    if len(key) != 32:
        raise RuntimeError(f"FRONTIERPULSE_ENC_KEY 需 32 bytes（64 hex），目前 {len(key)} bytes")
    return key


def encrypt_text(text: str, key: bytes) -> str:
    """AES-256-GCM → 信封 JSON。blob = base64(nonce12+ct+tag16)，
    與 CryptoKit AES.GCM.SealedBox(combined:) 位元組順序一致。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    blob = base64.b64encode(nonce + ct).decode("ascii")
    return json.dumps({ENC_ENVELOPE_TAG: 1, "alg": "AES-256-GCM", "blob": blob},
                      ensure_ascii=False)


def dump_public(path: Path, obj: dict):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    key = _enc_key()
    path.write_text(text if key is None else encrypt_text(text, key), encoding="utf-8")


def decrypt_text(envelope_text: str, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    env = json.loads(envelope_text)
    raw = base64.b64decode(env["blob"])
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def load_public(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    obj = json.loads(text)
    if isinstance(obj, dict) and obj.get(ENC_ENVELOPE_TAG) and obj.get("blob"):
        key = _enc_key()
        if key is None:
            raise RuntimeError(f"{path.name} 是加密檔，但未設 FRONTIERPULSE_ENC_KEY。")
        return json.loads(decrypt_text(text, key))
    return obj


# --------------------------------------------------------- 合併/寫入 ----

def _slugify(base: str, fallback: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in (base or "").lower()).strip("-")
    slug = "-".join(p for p in slug.split("-") if p)[:60]
    return slug or fallback


def _host_label(url: str) -> str:
    try:
        host = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
    except Exception:
        return "Source"
    return host or "Source"


def _clamp_importance(value) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def normalize_items(items: list) -> list:
    """淨化：保證每筆都有合法 category / source_type / story_id / 欄位預設值，
    連結只留 http(s)。"""
    out = []
    for i, it in enumerate(items):
        cat = it.get("category", "frontier_other")
        if cat not in CATEGORIES:
            cat = "frontier_other"
        st = (it.get("source_type") or "").strip()
        if st not in SOURCE_TYPES:
            st = "news"
        sid = ((it.get("story_id") or it.get("id") or "").strip()
               or _slugify(it.get("title_en", ""), f"item-{i}"))
        links = []
        for link in it.get("links", []):
            if not isinstance(link, dict):
                continue
            url = (link.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            label = (link.get("label") or "").strip() or _host_label(url)
            links.append({"label": label, "url": url})
        importance = _clamp_importance(it.get("importance", 3))
        out.append({
            "id": sid, "story_id": sid, "category": cat, "source_type": st,
            "title_en": (it.get("title_en") or "").strip(),
            "title_zh": (it.get("title_zh") or "").strip(),
            "summary_en": (it.get("summary_en") or "").strip(),
            "summary_zh": (it.get("summary_zh") or "").strip(),
            "analysis_en": (it.get("analysis_en") or "").strip(),
            "analysis_zh": (it.get("analysis_zh") or "").strip(),
            "quote": (it.get("quote") or "").strip(),
            "quote_zh": (it.get("quote_zh") or "").strip(),
            "importance": importance,
            "first_seen": (it.get("first_seen") or "").strip(),
            "updated_at": (it.get("updated_at") or "").strip(),
            "links": links,
        })
    return out


def load_today_items(repo: Path, date_str: str) -> list:
    path = repo / "digests" / f"{date_str}.json"
    if not path.exists():
        return []
    try:
        doc = load_public(path)
        # Repo 初始的展示資料不能混入第一次真實聚合；否則 importance>=4
        # 的假新聞會被「保留重大舊聞」邏輯永久留在公開 feed。
        if is_sample_digest(doc):
            return []
        return doc.get("items_flat", []) or []
    except Exception as e:
        print(f"[merge] 讀今日既有條目失敗（當空處理）：{e}")
        return []


def _merge_one(old: dict, new: dict, run_iso: str) -> dict:
    new["first_seen"] = old.get("first_seen") or new.get("first_seen") or run_iso
    new["importance"] = max(_clamp_importance(new.get("importance", 3)),
                            _clamp_importance(old.get("importance", 3)))
    seen_urls = {l["url"] for l in new.get("links", [])}
    new["links"] = new.get("links", []) + [l for l in old.get("links", [])
                                           if l.get("url") not in seen_urls]
    if not new.get("quote") and old.get("quote"):
        new["quote"] = old["quote"]
        new["quote_zh"] = old.get("quote_zh", "")
    if not new.get("analysis_en") and old.get("analysis_en"):
        new["analysis_en"] = old["analysis_en"]
        new["analysis_zh"] = old.get("analysis_zh", "")
    new["updated_at"] = run_iso
    return new


def is_sample_digest(doc: dict) -> bool:
    runs = doc.get("runs", []) or []
    return bool(runs) and all(
        run.get("backend") == "sample" or run.get("model") == "sample-data"
        for run in runs if isinstance(run, dict)
    )


def merge_daily_items(existing_flat: list, grok_items: list, run_iso: str) -> tuple[list, int, int]:
    old_by_id = {it.get("story_id") or it.get("id"): it for it in existing_flat}
    out, seen_ids = [], set()
    updated = 0
    for it in normalize_items(grok_items):
        sid = it["story_id"]
        if sid in old_by_id:
            it = _merge_one(old_by_id[sid], it, run_iso)
            updated += 1
        else:
            it["first_seen"] = it.get("first_seen") or run_iso
            it["updated_at"] = run_iso
        out.append(it)
        seen_ids.add(sid)
    kept = 0
    for sid, old in old_by_id.items():
        if sid not in seen_ids and _clamp_importance(old.get("importance", 3)) >= 4:
            out.append(normalize_items([old])[0])
            kept += 1
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    out.sort(key=lambda x: -int(x.get("importance", 3)))
    return out, updated, kept


def write_digest(repo: Path, date_str: str, run_iso: str, grok: dict) -> Path:
    digests = repo / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    path = digests / f"{date_str}.json"
    doc = load_public(path) if path.exists() else {"date": date_str, "runs": []}
    if is_sample_digest(doc):
        doc = {"date": date_str, "runs": []}

    existing_flat = doc.get("items_flat", []) or []
    merged, updated_n, kept_n = merge_daily_items(existing_flat, grok.get("items", []), run_iso)
    doc["items_flat"] = merged
    by_cat = {c: [] for c in CATEGORIES}
    for it in merged:
        by_cat[it["category"]].append(it)
    doc["categories"] = by_cat
    doc.setdefault("runs", []).append({
        "generated_at": run_iso,
        "backend": grok.get("_usage", {}).get("backend", DIGEST_BACKEND),
        "model": grok.get("_usage", {}).get("model", ""),
        "item_count": len(merged), "updated_count": updated_n, "kept_count": kept_n,
    })
    doc["brief_en"] = grok.get("brief_en", "")
    doc["brief_zh"] = grok.get("brief_zh", "")
    doc["updated_at"] = run_iso
    dump_public(path, doc)
    return path


# ---------------------------------------------------- 模型發布追蹤器 ----

MODEL_KINDS = {"model", "update", "open_weights", "deprecation"}


def load_model_history_seed(repo: Path) -> tuple[list, set[str]]:
    """讀取經官方來源核對的歷史基線，避免即時視窗被誤當成完整時間軸。"""
    path = repo / "backend" / "model_history_seed.json"
    if not path.exists():
        return [], set()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid model history seed: {path}: {exc}") from exc
    models = doc.get("models", [])
    replace_ids = doc.get("replace_ids", [])
    if not isinstance(models, list) or not isinstance(replace_ids, list):
        raise RuntimeError(f"invalid model history seed schema: {path}")
    return models, {str(mid).strip() for mid in replace_ids if str(mid).strip()}


def update_model_tracker(repo: Path, run_iso: str, releases: list) -> int:
    """合併官方核對歷史基線與本輪新發布，以 model_id 去重後寫入 models.json。

    歷史基線解決「只看最近 N 小時」無法回填舊版本的結構性缺口；即時發布則持續
    累積未來版本。models.json 免費開放（追蹤器是拉新賣點，不上鎖）。
    """
    path = repo / "models.json"
    doc = load_public(path) if path.exists() else {"models": []}
    seeded_models = doc.get("models", []) or []
    if seeded_models and all(str(m.get("model_id", "")).startswith("sample-")
                             for m in seeded_models if isinstance(m, dict)):
        doc = {"models": []}
    history_seed, replace_ids = load_model_history_seed(repo)
    by_id = {m.get("model_id"): m for m in doc.get("models", [])
             if m.get("model_id") and m.get("model_id") not in replace_ids}
    added = 0
    # 即時資料先合併；同 model_id 若已在官方核對基線中，以基線為準，避免模型輸出
    # 把正式名稱、發布日或官方連結覆寫成未核實內容。非基線的新版本仍會正常累積。
    for r in [*(releases or []), *history_seed]:
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or "").strip()
        link = (r.get("link") or "").strip()
        if not name:
            continue
        mid = (r.get("model_id") or "").strip() or _slugify(name, "")
        if not mid:
            continue
        lab = r.get("lab", "frontier_other")
        if lab not in CATEGORIES:
            lab = "frontier_other"
        kind = (r.get("kind") or "model").strip()
        if kind not in MODEL_KINDS:
            kind = "model"
        entry = {
            "model_id": mid, "name": name, "lab": lab, "kind": kind,
            "released": (r.get("released") or "").strip()[:10],
            "note_en": (r.get("note_en") or "").strip(),
            "note_zh": (r.get("note_zh") or "").strip(),
            "link": link if link.startswith(("http://", "https://")) else "",
            "recorded_at": (r.get("recorded_at") or run_iso),
        }
        if mid in by_id:
            old = by_id[mid]
            entry["recorded_at"] = old.get("recorded_at", run_iso)
            by_id[mid] = entry
        else:
            by_id[mid] = entry
            added += 1
    models = sorted(by_id.values(), key=lambda m: (m.get("released", ""), m.get("recorded_at", "")),
                    reverse=True)
    dump_public(path, {"updated_at": run_iso, "models": models})
    return added


# -------------------------------------------------------------- index ----

def rebuild_index(repo: Path, run_iso: str):
    digests = repo / "digests"
    files = sorted(digests.glob("*.json"), reverse=True)
    index = {"updated_at": run_iso, "dates": []}
    for f in files:
        try:
            doc = load_public(f)
        except Exception:
            continue
        index["dates"].append({
            "date": doc.get("date"),
            "file": f"digests/{f.name}",
            "item_count": len(doc.get("items_flat", [])),
            "brief_zh": doc.get("brief_zh", ""),
            "updated_at": doc.get("updated_at", ""),
        })
    dump_public(repo / "index.json", index)
    if files:
        dump_public(repo / "latest.json", load_public(files[0]))


# ---------------------------------------------------------------- git ----

def git_commit_push(repo: Path, date_str: str):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True)
    msg = f"digest: {date_str} auto update"
    r = subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                       capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        print("[git] nothing to commit")
        return
    subprocess.run(["git", "-C", str(repo), "pull", "--rebase", "origin", "main"],
                   capture_output=True)
    p = subprocess.run(["git", "-C", str(repo), "push", "origin", "main"],
                       capture_output=True, encoding="utf-8")
    print("[git] push:", "ok" if p.returncode == 0 else (p.stderr or "")[:300])


# --------------------------------------------------------------- main ----

def main():
    repo = Path(os.environ.get("REPO_DIR", "") or Path(__file__).resolve().parent.parent)
    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.astimezone(TAIPEI).strftime("%Y-%m-%d")
    run_iso = now_utc.isoformat()
    print(f"[run] {run_iso} date={date_str} backend={DIGEST_BACKEND} repo={repo}")

    existing = load_today_items(repo, date_str)
    grok = call_grok(date_str, run_iso, existing)
    path = write_digest(repo, date_str, run_iso, grok)
    added = update_model_tracker(repo, run_iso, grok.get("model_releases", []))
    rebuild_index(repo, run_iso)
    print(f"[ok] wrote {path.name}: {len(load_public(path).get('items_flat', []))} items, "
          f"{added} new model release(s)")

    if os.environ.get("NO_PUSH", "") != "1":
        git_commit_push(repo, date_str)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[fatal] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
