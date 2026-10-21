import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest

from research_ledger import log, prereg
from research_ledger.__main__ import main

T0 = dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.timezone.utc)
H = dt.timedelta(hours=1)


def read(p):
    with open(p) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self._cwd = os.getcwd()
        os.chdir(self.d)
        self._env = os.environ.get("RESEARCH_LEDGER")
        os.environ["RESEARCH_LEDGER"] = os.path.join(self.d, "L.jsonl")
        for name, text in (("P.md", "# prereg\n"), ("a.py", "x = 1\n"), ("data.csv", "1,2\n")):
            with open(name, "w") as f:
                f.write(text)

    def tearDown(self):
        os.chdir(self._cwd)
        if self._env is None:
            os.environ.pop("RESEARCH_LEDGER", None)
        else:
            os.environ["RESEARCH_LEDGER"] = self._env
        self._tmp.cleanup()

    def lines(self):
        with open(os.environ["RESEARCH_LEDGER"]) as f:
            return [json.loads(x) for x in f]


class LogTest(Base):
    def test_record_appends_with_code_hash(self):
        log.record("m1", "s", 3, {"x": 1}, code_file="a.py", now=T0)
        log.record("m2", "s", 4, {"x": 2}, code_file="a.py", now=T0 + H)
        rows = self.lines()
        self.assertEqual([r["what"] for r in rows], ["m1", "m2"])
        self.assertEqual(rows[0]["code"]["sha256"], log.sha256_file("a.py"))
        self.assertEqual(rows[0]["at"], "2026-11-01T09:00:00Z")

    def test_no_rewrite_api(self):
        for name in dir(log) + dir(prereg):
            self.assertFalse(any(w in name.lower() for w in ("rewrite", "delete", "remove", "overwrite")), name)

    def test_secrets_are_redacted(self):
        key = "".join("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"[i % 58] for i in range(88))
        r = log.record("x", "https://h/?api-key=SECRET123&a=1", 1,
                       {"api_key": "SECRET123", "wallet": key, "bytes": list(range(64)),
                        "id": "123e4567-e89b-12d3-a456-426614174000", "json": '{"password": "hunter2"}'},
                       code_file="a.py")
        text = json.dumps(r)
        for secret in ("SECRET123", key, "hunter2", "426614174000"):
            self.assertNotIn(secret, text)
        self.assertEqual(r["result"]["bytes"], "REDACTED-BYTES")

    def test_base58_can_be_kept(self):
        sig = "5" * 88
        r = log.append({"at": "t", "kind": "note", "sig": sig}, base58=False)
        self.assertEqual(r["sig"], sig)

    def test_seal_hides_what_redaction_missed(self):
        out = log.seal({"note": "API_KEY=abc"})
        self.assertEqual(out["note"], "API_KEY=REDACTED")
        sealed = log.seal({"a": {"b": "x api-key=still"}})
        self.assertNotIn("still", json.dumps(sealed))

    def test_correction_is_flagged_not_hidden(self):
        r = log.record("rate of misses", "s", 10, {"share": "15 %"}, code_file="a.py", now=T0)
        log.correct(r["at"], "rate of misses", {"share": "13 %"}, "double counted", now=T0 + H)
        hits = log.find("rate of misses")
        self.assertEqual(len(hits), 2)
        original, fixes = hits[0]
        self.assertEqual(original["result"]["share"], "15 %")
        self.assertEqual(fixes[0]["result"]["share"], "13 %")
        self.assertIn("CORRECTED", log.show(original, fixes))

    def test_broken_line_is_reported(self):
        log.record("ok", "s", 1, {}, code_file="a.py")
        with open(os.environ["RESEARCH_LEDGER"], "a") as f:
            f.write("{broken\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(len(log.rows()), 1)
        self.assertIn("line 2 is broken", err.getvalue())


class PreregTest(Base):
    def freeze(self, read_after="2026-11-01T12:00:00Z"):
        return prereg.freeze("P.md", ["a.py", "data.csv"], read_after=read_after, now=T0)

    def test_freeze_writes_table_and_row(self):
        self.freeze()
        text = read("P.md")
        self.assertIn("## Frozen 2026-11-01T09:00:00Z", text)
        self.assertIn(log.sha256_file("a.py", short=0), text)
        self.assertEqual(self.lines()[-1]["kind"], "freeze")

    def test_gate_refuses_unfrozen(self):
        with self.assertRaisesRegex(prereg.GateRefused, "never frozen"):
            prereg.gate("P.md", now=T0)

    def test_gate_refuses_too_early(self):
        self.freeze()
        with self.assertRaisesRegex(prereg.GateRefused, "too early"):
            prereg.gate("P.md", now=T0 + 2 * H)

    def test_gate_refuses_changed_code_and_data(self):
        self.freeze()
        with open("data.csv", "a") as f:
            f.write("3,4\n")
        with self.assertRaisesRegex(prereg.GateRefused, "data.csv"):
            prereg.gate("P.md", now=T0 + 4 * H)
        self.assertEqual(list(prereg.verify("P.md")), ["data.csv"])

    def test_gate_refuses_missing_file(self):
        self.freeze()
        os.remove("a.py")
        with self.assertRaisesRegex(prereg.GateRefused, "missing"):
            prereg.gate("P.md", now=T0 + 4 * H)

    def test_one_read_then_again_needs_a_reason(self):
        self.freeze()
        prereg.gate("P.md", now=T0 + 4 * H)
        with self.assertRaisesRegex(prereg.GateRefused, "already read"):
            prereg.gate("P.md", now=T0 + 5 * H)
        r = prereg.gate("P.md", again="reviewer asked to rerun", now=T0 + 6 * H)
        self.assertEqual(r["again"], "reviewer asked to rerun")
        self.assertEqual([x["kind"] for x in self.lines()], ["freeze", "read", "read"])

    def test_refreeze_uses_the_latest_hashes(self):
        self.freeze(read_after=None)
        with open("a.py", "a") as f:
            f.write("y = 2\n")
        prereg.freeze("P.md", ["a.py", "data.csv"], now=T0 + H)
        self.assertEqual(prereg.verify("P.md"), {})

    def test_veto_closes_mechanically(self):
        self.freeze(read_after=None)
        r = prereg.veto("P.md", 1.5, 4.0, need="~2,900 trades", now=T0)
        self.assertEqual(r["outcome"], "VETO")
        self.assertTrue(read("P.md").startswith("> **VETO"))
        with self.assertRaisesRegex(prereg.GateRefused, "closed"):
            prereg.gate("P.md", now=T0 + 4 * H)
        with self.assertRaisesRegex(prereg.GateRefused, "already closed"):
            prereg.veto("P.md", 1.0, 4.0)

    def test_veto_passed_appends(self):
        r = prereg.veto("P.md", 5.0, 4.0, now=T0)
        self.assertEqual(r["outcome"], "passed")
        text = read("P.md")
        self.assertTrue(text.startswith("# prereg"))
        self.assertIn("Veto check passed", text)

    def test_replace_and_note_need_reasons(self):
        with self.assertRaises(ValueError):
            prereg.replace("P.md", "Q.md", "")
        with self.assertRaises(ValueError):
            prereg.note("stop", "")
        prereg.replace("P.md", "Q.md", "instrument changed", now=T0)
        self.assertTrue(read("P.md").startswith("> **REPLACED"))


class CliTest(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_gate_exit_code(self):
        self.run_cli("freeze", "P.md", "a.py", "--read-after", "2999-01-01T00:00:00Z")
        code, _, err = self.run_cli("gate", "P.md")
        self.assertEqual(code, 2)
        self.assertIn("too early", err)

    def test_record_and_find(self):
        self.run_cli("record", "latency p50", "--sample", "rpc A", "--n", "10", "--result", "p50=120 ms",
                     "--code", "a.py")
        code, out, _ = self.run_cli("find", "latency")
        self.assertIn("p50 120 ms", out)
        self.assertIn("-- 1 found", out)

    def test_demo_runs_and_leaves_no_files(self):
        code, out, _ = self.run_cli("demo")
        self.assertEqual(code, 0)
        self.assertIn("REFUSED: PREREG-exit-rule.md: too early", out)
        self.assertIn("already read", out)
        self.assertIn("api-key=REDACTED", out)
        self.assertEqual(sorted(os.listdir(self.d)), ["P.md", "a.py", "data.csv"])


if __name__ == "__main__":
    unittest.main()
