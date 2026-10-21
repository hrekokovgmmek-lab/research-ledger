"""The ledger: one append-only JSONL file of results, decisions, freezes and reads.

There is no function that rewrites or deletes a row. A wrong number is fixed by a correction row that points at
the original; `find` then shows the original with a warning instead of hiding it.
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys


def path():
    return os.environ.get("RESEARCH_LEDGER") or "LEDGER.jsonl"


def now_iso(now=None):
    t = now or dt.datetime.now(dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(p, short=12):
    try:
        with open(p, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        return h[:short] if short else h
    except OSError:
        return "?"


# --- secrets never reach the file ------------------------------------------------------------------------------
KEY_PARAM = re.compile(r"(api[-_]?key=)[^&\"'\s\\]+", re.I)
KEY_FIELD = re.compile(r"((?:api[-_]?key|apikey|secret|private[-_]?key|token|password)\\?\"?\s*[:=]\s*\\?\"?)"
                       r"[^\"\\\s,}]+", re.I)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
LONG_B58 = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,}\b")
LEAK = re.compile(r"api[-_]?key=(?!REDACTED)", re.I)


def redact(s, base58=True):
    """API keys in URLs and key-like fields, UUIDs and (by default) base58 strings of 64+ characters — the length of
    a private key, but also of a transaction signature: pass base58=False to keep signatures."""
    s = KEY_PARAM.sub(r"\1REDACTED", s)
    s = KEY_FIELD.sub(r"\1REDACTED", s)
    s = UUID.sub("REDACTED-UUID", s)
    if base58:
        s = LONG_B58.sub("REDACTED-B58", s)
    return s


def _clean(v, base58):
    if isinstance(v, str):
        return redact(v, base58)
    if isinstance(v, (list, tuple)):
        if len(v) >= 32 and all(isinstance(x, int) and 0 <= x <= 255 for x in v):
            return "REDACTED-BYTES"                       # a keypair file is a JSON list of 64 bytes
        return [_clean(x, base58) for x in v]
    if isinstance(v, dict):
        return {k: ("REDACTED" if SECRET_NAME.match(str(k)) and x not in (None, "") else _clean(x, base58))
                for k, x in v.items()}
    return v


SECRET_NAME = re.compile(r"^(api[-_]?key|apikey|secret|private[-_]?key|token|password)$", re.I)


def seal(row, base58=True):
    """Redact every string, then hide any string that still carries a key (a pattern redaction missed)."""
    hidden = []

    def walk(v, at):
        if isinstance(v, str):
            if LEAK.search(v):
                hidden.append(at)
                return "HIDDEN: contained a key after redaction"
            return v
        if isinstance(v, list):
            return [walk(x, f"{at}[{i}]") for i, x in enumerate(v)]
        if isinstance(v, dict):
            return {k: walk(x, f"{at}.{k}" if at else k) for k, x in v.items()}
        return v

    out = walk(_clean(row, base58), "")
    if hidden:
        out["hidden"] = hidden
    return out


# --- writing ----------------------------------------------------------------------------------------------------
def append(row, base58=True):
    """Append one row. Never raises: losing the note is better than losing the run that produced it."""
    line = json.dumps(seal(row, base58), ensure_ascii=False)
    try:
        with open(path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[research-ledger] not written: {e}", file=sys.stderr)
    return json.loads(line)


def record(what, sample, n, result, code_file=None, now=None, base58=True):
    """A result: what was measured, on which sample, n, the numbers, and the hash of the code that measured it."""
    f = code_file or (sys.argv[0] if sys.argv and sys.argv[0] else "?")
    return append({"at": now_iso(now), "kind": "result", "what": what, "sample": sample, "n": n, "result": result,
                   "code": {"file": os.path.basename(f), "sha256": sha256_file(f)}}, base58)


def correct(at, what, result, why, now=None):
    """A correction to the row written at `at`. The original stays; `find` flags it."""
    return append({"at": now_iso(now), "kind": "correction", "corrects": at, "what": what, "result": result,
                   "why": why})


# --- reading ----------------------------------------------------------------------------------------------------
def rows():
    p = path()
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:                            # never skip silently: "not found" would be a lie
                print(f"[research-ledger] line {i} is broken and skipped: {line[:80]!r}", file=sys.stderr)
    return out


def find(query):
    """Rows whose JSON contains `query` (case-insensitive), each with the corrections that point at it."""
    all_rows = rows()
    fixes = {}
    for r in all_rows:
        if r.get("kind") == "correction":
            fixes.setdefault(r.get("corrects"), []).append(r)
    q = query.lower()
    return [(r, fixes.get(r.get("at"), [])) for r in all_rows if q in json.dumps(r, ensure_ascii=False).lower()]


def show(r, fixes=()):
    kind = r.get("kind", "?")
    if kind == "result":
        res = "; ".join(f"{k} {v}" for k, v in (r.get("result") or {}).items())
        code = r.get("code") or {}
        text = f"{r['at']}  result  {r.get('what')} | n={r.get('n')} | {r.get('sample')}\n" \
               f"{'':22}{res}\n{'':22}code {code.get('file')} ({code.get('sha256')})"
    elif kind == "correction":
        text = f"{r['at']}  CORRECTION of {r.get('corrects')}: {r.get('what')} -> {r.get('result')} ({r.get('why')})"
    else:
        rest = {k: v for k, v in r.items() if k not in ("at", "kind")}
        text = f"{r['at']}  {kind:<8}{json.dumps(rest, ensure_ascii=False)}"
    for fx in fixes:
        text += f"\n{'':22}! CORRECTED at {fx['at']}: read the correction, not this row"
    return text
