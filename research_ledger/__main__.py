"""research-ledger — an append-only log of results and decisions; preregistrations that freeze code and data.

    python3 -m research_ledger demo                                   # the whole workflow in a temporary folder
    python3 -m research_ledger record "what" --sample "..." --n 120 --result "mean=+1.2%" --code analysis.py
    python3 -m research_ledger find <words>                           # was this measured before? (corrections flagged)
    python3 -m research_ledger last [N]
    python3 -m research_ledger correct <at> "what" --result "..." --why "..."
    python3 -m research_ledger freeze PREREG.md analysis.py data.csv [--read-after 2026-11-01T12:00Z]
    python3 -m research_ledger verify PREREG.md
    python3 -m research_ledger gate PREREG.md [--again "reason"]      # exit 2 if the read is not allowed
    python3 -m research_ledger veto PREREG.md --x 1.8 --y 2.5 [--need "..."] [--why "..."]
    python3 -m research_ledger replace PREREG.md --by OTHER.md --why "..."
    python3 -m research_ledger note "what was decided" --why "..."

The ledger file is $RESEARCH_LEDGER or ./LEDGER.jsonl.
"""
import argparse
import datetime as dt
import os
import sys
import tempfile

from . import log, prereg


def _kv(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v.strip()
    return out


def demo():
    old_cwd, old_env = os.getcwd(), os.environ.get("RESEARCH_LEDGER")
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        os.environ["RESEARCH_LEDGER"] = os.path.join(d, "LEDGER.jsonl")
        try:
            _demo_steps()
        finally:
            os.chdir(old_cwd)
            if old_env is None:
                os.environ.pop("RESEARCH_LEDGER", None)
            else:
                os.environ["RESEARCH_LEDGER"] = old_env


def _demo_steps():
    t0 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)
    say = lambda s: print(f"\n# {s}")
    with open("PREREG-exit-rule.md", "w") as f:
        f.write("# Preregistration: does a trailing stop beat a fixed 30-minute hold?\n\nMetric: mean return per trade. "
                "Rule: adopt if the 97.5 % lower bound is above 0. One read.\n")
    with open("analysis.py", "w") as f:
        f.write("print('the analysis')\n")
    with open("trades.csv", "w") as f:
        f.write("id,ret\n1,0.12\n2,-0.30\n")

    say("freeze the analysis and the data before the outcome exists")
    r = prereg.freeze("PREREG-exit-rule.md", ["analysis.py", "trades.csv"], read_after="2026-11-01T12:00:00Z", now=t0)
    print(f"frozen {len(r['files'])} files, read after {r['read_after']}")

    say("the analysis starts too early")
    try:
        prereg.gate("PREREG-exit-rule.md", now=t0)
    except prereg.GateRefused as e:
        print("REFUSED:", e)

    say("someone 'fixes' the analysis after the freeze")
    with open("analysis.py", "a") as f:
        f.write("# drop the two worst days\n")
    try:
        prereg.gate("PREREG-exit-rule.md", now=t0 + dt.timedelta(hours=4))
    except prereg.GateRefused as e:
        print("REFUSED:", e)
    with open("analysis.py", "w") as f:
        f.write("print('the analysis')\n")

    say("the first read, on time and with the frozen code")
    r = prereg.gate("PREREG-exit-rule.md", now=t0 + dt.timedelta(hours=4))
    print("allowed, read logged at", r["at"])
    log.record("trailing stop vs 30-min hold", "paper trades 2026-10-01..10-31", 412,
               {"mean": "+0.8 %/trade", "97.5 % CI": "[-0.6; +2.1]", "verdict": "not adopted"},
               code_file="analysis.py", now=t0 + dt.timedelta(hours=4, minutes=1))
    print("result recorded")

    say("a second look, hoping for a better number")
    try:
        prereg.gate("PREREG-exit-rule.md", now=t0 + dt.timedelta(hours=5))
    except prereg.GateRefused as e:
        print("REFUSED:", e)

    say("a wrong number is corrected, never overwritten")
    first = log.find("trailing stop")[0][0]
    log.correct(first["at"], "trailing stop vs 30-min hold", {"mean": "+0.7 %/trade"}, "fee was counted once",
                now=t0 + dt.timedelta(hours=6))
    for row, fixes in log.find("trailing stop"):
        print(log.show(row, fixes))

    say("another idea: expected effect 1.5 pp, but the sample only resolves 4.0 pp")
    with open("PREREG-volume-filter.md", "w") as f:
        f.write("# Preregistration: skip pools with low first-hour volume\n")
    prereg.veto("PREREG-volume-filter.md", 1.5, 4.0, need="~2,900 trades, ~40 days", now=t0 + dt.timedelta(hours=7))
    with open("PREREG-volume-filter.md") as f:
        print(f.read().splitlines()[0][:110] + " ...")

    say("secrets do not reach the ledger")
    r = log.record("RPC latency", "https://rpc.example.com/?api-key=abcd1234", 10, {"p50": "120 ms"},
                   code_file="analysis.py", now=t0 + dt.timedelta(hours=8))
    print("sample stored as:", r["sample"])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="research_ledger", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    p = sub.add_parser("record")
    p.add_argument("what")
    p.add_argument("--sample", required=True)
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--result", action="append", help="key=value, repeatable")
    p.add_argument("--code", help="file of the code that measured it (hashed)")
    p = sub.add_parser("find")
    p.add_argument("words", nargs="+")
    p = sub.add_parser("last")
    p.add_argument("n", nargs="?", type=int, default=10)
    p = sub.add_parser("correct")
    p.add_argument("at")
    p.add_argument("what")
    p.add_argument("--result", action="append")
    p.add_argument("--why", required=True)
    p = sub.add_parser("freeze")
    p.add_argument("prereg")
    p.add_argument("files", nargs="+")
    p.add_argument("--read-after")
    p = sub.add_parser("verify")
    p.add_argument("prereg")
    p = sub.add_parser("gate")
    p.add_argument("prereg")
    p.add_argument("--again")
    p = sub.add_parser("veto")
    p.add_argument("prereg")
    p.add_argument("--x", type=float, required=True)
    p.add_argument("--y", type=float, required=True)
    p.add_argument("--need", default="")
    p.add_argument("--why", default="")
    p = sub.add_parser("replace")
    p.add_argument("prereg")
    p.add_argument("--by", required=True)
    p.add_argument("--why", required=True)
    p = sub.add_parser("note")
    p.add_argument("what")
    p.add_argument("--why", required=True)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "demo":
            demo()
        elif a.cmd == "record":
            r = log.record(a.what, a.sample, a.n, _kv(a.result), code_file=a.code)
            print(log.show(r))
        elif a.cmd == "find":
            hits = log.find(" ".join(a.words))
            for row, fixes in hits:
                print(log.show(row, fixes))
            print(f"-- {len(hits)} found in {log.path()}")
        elif a.cmd == "last":
            for row in log.rows()[-a.n:]:
                print(log.show(row))
        elif a.cmd == "correct":
            print(log.show(log.correct(a.at, a.what, _kv(a.result), a.why)))
        elif a.cmd == "freeze":
            r = prereg.freeze(a.prereg, a.files, a.read_after)
            print(f"frozen {len(r['files'])} file(s) in {a.prereg}" + (f", read after {a.read_after}" if a.read_after else ""))
        elif a.cmd == "verify":
            changed = prereg.verify(a.prereg)
            for f, (was, now) in changed.items():
                print(f"CHANGED {f}: {was} -> {now}")
            print("all frozen files unchanged" if not changed else f"{len(changed)} file(s) changed")
            return 1 if changed else 0
        elif a.cmd == "gate":
            r = prereg.gate(a.prereg, again=a.again)
            print(f"read allowed and logged at {r['at']}")
        elif a.cmd == "veto":
            r = prereg.veto(a.prereg, a.x, a.y, a.need, a.why)
            print(f"{r['outcome']}: X={a.x:g} vs Y={a.y:g}")
        elif a.cmd == "replace":
            prereg.replace(a.prereg, a.by, a.why)
            print(f"{a.prereg} marked as replaced by {a.by}")
        elif a.cmd == "note":
            print(log.show(prereg.note(a.what, a.why)))
    except prereg.GateRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
