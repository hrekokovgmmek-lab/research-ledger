"""Preregistration discipline: freeze the code and data before the outcome exists, read the outcome once.

A preregistration is a markdown file written before the data: the rule, the metric, the decision criterion. This
module adds what a text file cannot enforce by itself:

- freeze  — sha256 of the analysis code and inputs, appended to the file and to the ledger;
- gate    — the first call of an analysis script: refuses to run before the agreed time, after any frozen file
            changed, on a closed experiment, or a second time without a stated reason;
- veto    — closes an experiment whose expected effect X is below the resolution Y of the instrument, mechanically;
- replace — closes an experiment that was superseded by another preregistration.
"""
import datetime as dt
import os

from . import log

CLOSED = ("> **VETO", "> **REPLACED")


class GateRefused(RuntimeError):
    pass


def _parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def closed_banner(prereg):
    with open(prereg, encoding="utf-8") as f:
        for line in f.read().splitlines()[:12]:
            if line.startswith(CLOSED):
                return line
    return None


def freeze(prereg, files, read_after=None, now=None):
    """Hash `files` and append the table to the preregistration and a `freeze` row to the ledger."""
    if not os.path.isfile(prereg):
        raise FileNotFoundError(prereg)
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    hashes = {f: log.sha256_file(f, short=0) for f in files}
    stamp = log.now_iso(now)
    lines = [f"\n\n## Frozen {stamp} (research-ledger)\n", "| file | sha256 |", "|---|---|"]
    lines += [f"| `{f}` | `{h}` |" for f, h in hashes.items()]
    if read_after:
        lines.append(f"\nRead the outcome not before **{read_after}**, once.")
    with open(prereg, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return log.append({"at": stamp, "kind": "freeze", "prereg": os.path.basename(prereg), "files": hashes,
                       "read_after": read_after}, base58=False)


def _last_freeze(prereg):
    name = os.path.basename(prereg)
    fr = [r for r in log.rows() if r.get("kind") == "freeze" and r.get("prereg") == name]
    return fr[-1] if fr else None


def verify(prereg):
    """{file: (frozen, now)} for every frozen file whose content changed since the last freeze."""
    fr = _last_freeze(prereg)
    if not fr:
        raise GateRefused(f"{os.path.basename(prereg)} was never frozen")
    changed = {}
    for f, h in fr["files"].items():
        cur = log.sha256_file(f, short=0)
        if cur != h:
            changed[f] = (h[:12], cur[:12] if cur != "?" else "missing")
    return changed


def gate(prereg, again=None, now=None):
    """Call first in the analysis that reads the outcome. Raises GateRefused instead of letting it run when:
    the experiment is closed, the code or data changed since the freeze, it is too early, or the outcome was
    already read (unless `again` says why it is read again — that reason goes into the ledger)."""
    name = os.path.basename(prereg)
    banner = closed_banner(prereg)
    if banner:
        raise GateRefused(f"{name} is closed: {banner}")
    changed = verify(prereg)
    if changed:
        raise GateRefused(f"{name}: frozen files changed: " +
                          ", ".join(f"{f} {a} -> {b}" for f, (a, b) in changed.items()))
    fr = _last_freeze(prereg)
    t = now or dt.datetime.now(dt.timezone.utc)
    ra = _parse_time(fr.get("read_after"))
    if ra and t < ra:
        raise GateRefused(f"{name}: too early, read not before {fr['read_after']}")
    reads = [r for r in log.rows() if r.get("kind") == "read" and r.get("prereg") == name]
    if reads and not again:
        raise GateRefused(f"{name}: the outcome was already read at {reads[-1]['at']}; "
                          "a second read needs a stated reason (again=...)")
    return log.append({"at": log.now_iso(t), "kind": "read", "prereg": name, "files": fr["files"],
                       "again": again}, base58=False)


def _prepend(prereg, banner):
    old = closed_banner(prereg)
    if old:
        raise GateRefused(f"{os.path.basename(prereg)} is already closed: {old}")
    with open(prereg, encoding="utf-8") as f:
        text = f.read()
    tmp = prereg + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(banner + "\n\n" + text)
    os.replace(tmp, prereg)


def veto(prereg, x, y, need="", why="", now=None):
    """X = the effect you expect, Y = the smallest effect this sample can tell from zero. X < Y: the question
    cannot be answered with this instrument — a banner goes on top of the file and the experiment is closed.
    The number decides, not the person who calls it."""
    name, stamp = os.path.basename(prereg), log.now_iso(now)
    if x < y:
        banner = (f"> **VETO {stamp} (research-ledger).** Expected effect X={x:g} is below the resolution "
                  f"Y={y:g}." + (f" Needed: {need}." if need else "") + (f" {why}" if why else "") +
                  "\n> **This instrument cannot answer the question.** Any number read from it is a description, "
                  "not a verdict.")
        _prepend(prereg, banner)
        return log.append({"at": stamp, "kind": "decision", "what": f"veto: {name}", "x": x, "y": y,
                           "outcome": "VETO", "need": need, "why": why})
    with open(prereg, "a", encoding="utf-8") as f:
        f.write(f"\n\n> Veto check passed {stamp}: X={x:g} >= Y={y:g}" + (f"; needed {need}" if need else "") + ".\n")
    return log.append({"at": stamp, "kind": "decision", "what": f"veto passed: {name}", "x": x, "y": y,
                       "outcome": "passed", "need": need})


def replace(prereg, by, why, now=None):
    if not why:
        raise ValueError("a replacement needs a reason")
    name, stamp = os.path.basename(prereg), log.now_iso(now)
    _prepend(prereg, f"> **REPLACED {stamp} (research-ledger)** by `{by}`. Reason: {why}\n"
                     "> Its outcome is not a verdict.")
    return log.append({"at": stamp, "kind": "decision", "what": f"replaced: {name}", "by": by, "why": why})


def note(what, why, now=None):
    """A decision that is not tied to one file (stop, continue, change the plan) — with its reason, or not at all."""
    if not why:
        raise ValueError("a decision needs a reason")
    return log.append({"at": log.now_iso(now), "kind": "decision", "what": what, "why": why})
