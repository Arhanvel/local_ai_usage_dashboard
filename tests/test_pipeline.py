"""End-to-end checks on synthetic transcripts: ingest -> stats -> insights -> exports.

Run with:  python -m unittest discover -s tests -v

No real ~/.claude data is touched: the tests build a throwaway Claude home
and data directory under a temp folder and point the package at them through
CLAUDE_CONFIG_DIR / AIDASH_DATA_DIR before importing it.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="aidash-test-")
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_TMP) / "home")
os.environ["AIDASH_DATA_DIR"] = str(Path(_TMP) / "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aidash import claude_ingest, config, db, insights, patterns, reports, stats  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"
T0 = "2026-08-01T09:00:{s:02d}.000Z"


def _rec(i, **kw):
    base = {"sessionId": SESSION, "cwd": "C:\\repo\\demo", "gitBranch": "feature/DEMO-42",
            "version": "2.1.0", "timestamp": T0.format(s=i), "uuid": f"u{i}"}
    base.update(kw)
    return base


def _assistant(i, text=None, tool=None, model="claude-sonnet-5", msg_id=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": tool["id"], "name": tool["name"], "input": tool["input"]})
    return _rec(i, type="assistant", requestId=f"req{msg_id or i}", message={
        "id": f"msg{msg_id or i}", "role": "assistant", "model": model, "stop_reason": "end_turn",
        "content": content,
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 1000,
                  "cache_read_input_tokens": 5000,
                  "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0}},
    })


def _user(i, text, source="typed"):
    return _rec(i, type="user", promptSource=source, message={"role": "user", "content": text})


def _tool_result(i, tool_id, payload, content="ok"):
    return _rec(i, type="user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content}]}, toolUseResult=payload)


TRANSCRIPT = [
    _rec(0, type="ai-title", aiTitle="DEMO-42 demo session"),
    _user(1, "Please fix the bug in DEMO-42 and run the tests"),
    _assistant(2, text="Looking.", tool={"id": "t1", "name": "Read", "input": {"file_path": "C:\\repo\\demo\\app.py"}}),
    # Same API response written as a second line: usage must count once.
    _assistant(3, text="Still looking.", msg_id=2),
    _tool_result(4, "t1", {"filePath": "C:\\repo\\demo\\app.py", "type": "text"}),
    _assistant(5, tool={"id": "t2", "name": "Edit", "input": {"file_path": "C:\\repo\\demo\\app.py"}}),
    _tool_result(6, "t2", {"filePath": "C:\\repo\\demo\\app.py",
                           "structuredPatch": [{"lines": ["+a", "+b", "-c"]}]}),
    _user(7, "commit changes"),
    _assistant(8, tool={"id": "t3", "name": "Bash", "input": {"command": "git commit -m 'DEMO-42 fix bug'"}}),
    _tool_result(9, "t3", {"stdout": "[feature/DEMO-42 abc1234] DEMO-42 fix bug\n 1 file changed, 2 insertions(+), 1 deletion(-)\n",
                           "stderr": "", "interrupted": False}),
    _user(10, "commit changes"),
    _user(11, "[Request interrupted by user]"),
    _assistant(12, text="Claude usage limit reached. Your limit will reset at 5pm."),
    _rec(13, type="system", subtype="turn_duration", durationMs=4200),
    _rec(14, type="system", subtype="compact_boundary"),
    _user(15, "<command-name>/commit</command-name><command-message>commit</command-message><command-args></command-args>"),
    _user(16, "internal sdk prompt", source="sdk"),
    _user(17, "<local-command-stdout>echo hi</local-command-stdout>", source=None),
]


def _write_home():
    home = config.claude_home()
    proj = home / "projects" / "C--repo-demo"
    proj.mkdir(parents=True, exist_ok=True)
    with open(proj / f"{SESSION}.jsonl", "w", encoding="utf-8") as fh:
        for rec in TRANSCRIPT:
            fh.write(json.dumps(rec) + "\n")
    # A second, older session in another project so multi-session logic has data.
    other = home / "projects" / "C--repo-other"
    other.mkdir(parents=True, exist_ok=True)
    sid2 = "aaaaaaaa-0000-0000-0000-000000000002"
    with open(other / f"{sid2}.jsonl", "w", encoding="utf-8") as fh:
        for i, rec in enumerate([
            _user(1, "commit changes"),
            _assistant(2, text="done", msg_id=902),  # message ids are global; keep them distinct
        ]):
            rec = dict(rec, sessionId=sid2, cwd="C:\\repo\\other", timestamp=f"2026-07-20T10:00:0{i}.000Z",
                       uuid=f"o{i}")
            fh.write(json.dumps(rec) + "\n")
    with open(home / "history.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"display": "hello", "timestamp": 1750000000000, "project": "C:\\repo\\gone",
                             "sessionId": "x", "pastedContents": {}}) + "\n")
        fh.write("not json\n")


class PatternTests(unittest.TestCase):
    def test_git_output(self):
        out = patterns.scan_git_output("[main abc1234] Fix thing\n 3 files changed, 10 insertions(+), 2 deletions(-)\n"
                                       "To github.com:x/y.git\nhttps://github.com/x/y/pull/12")
        self.assertEqual(out["commits"][0]["sha"], "abc1234")
        self.assertEqual((out["commits"][0]["files"], out["commits"][0]["insertions"], out["commits"][0]["deletions"]),
                         (3, 10, 2))
        self.assertEqual(out["pushes"], ["github.com:x/y.git"])
        self.assertEqual(out["prs"], ["https://github.com/x/y/pull/12"])

    def test_tickets_blocklist_and_shape(self):
        self.assertEqual(patterns.find_tickets("FORTH-123 UTF-8 HTTP-2 [A-Z0-9]-9 DPP-4567 X-1"),
                         {"FORTH-123", "DPP-4567"})

    def test_clean_prompt(self):
        self.assertEqual(patterns.clean_prompt("<command-name>/commit</command-name><command-args>-m x</command-args>"),
                         ("/commit -m x", "/commit"))
        self.assertEqual(patterns.clean_prompt("hi <system-reminder>secret</system-reminder> there"), ("hi  there", None))
        self.assertEqual(patterns.clean_prompt("<local-command-stdout>echo</local-command-stdout>"), ("", None))

    def test_human_prompt_rules(self):
        self.assertTrue(patterns.is_human_prompt("typed", None, "do it"))
        self.assertTrue(patterns.is_human_prompt(None, None, "do it"))
        self.assertFalse(patterns.is_human_prompt("sdk", None, "do it"))
        self.assertFalse(patterns.is_human_prompt(None, None, "Caveat: generated"))
        self.assertFalse(patterns.is_human_prompt("typed", None, "x", is_meta=True))
        self.assertTrue(patterns.is_human_prompt("sdk", "human", "override"))
        # No promptSource: only an interactive session can vouch for it.
        self.assertFalse(patterns.is_human_prompt(None, None, "do it", entrypoint="sdk-cli"))
        self.assertTrue(patterns.is_human_prompt(None, None, "do it", entrypoint="cli"))
        self.assertFalse(patterns.is_human_prompt(None, None, "<bash-stdout>x</bash-stdout>"))

    def test_intents_and_errors(self):
        intents = patterns.classify_intents("no, that's wrong - fix the bug then commit").split(",")
        self.assertIn("correction", intents)
        self.assertIn("bug/fix", intents)
        self.assertIn("commit/git", intents)
        self.assertEqual(patterns.classify_error("Claude usage limit reached."), "usage_limit")
        self.assertIsNone(patterns.classify_error("All good."))
        # A reply that merely discusses the topic, or is long, or used tools, is not a banner.
        self.assertIsNone(patterns.classify_error("To handle rate limits in this client, retry with backoff. " * 10))
        self.assertIsNone(patterns.classify_error("rate limit exceeded", tool_uses=2))
        self.assertEqual(patterns.classify_error("Error: rate limit exceeded (429)"), "rate_limit")

    def test_file_category(self):
        self.assertEqual(patterns.file_category(".py"), "code")
        self.assertEqual(patterns.file_category(".md"), "docs")
        self.assertEqual(patterns.file_category(".weird"), "other")

    def test_family_key_masks_digits(self):
        self.assertEqual(patterns.family_key("run batch 3"), patterns.family_key("run  batch 17"))


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _write_home()
        cls.con = db.connect()
        db.init(cls.con)
        cls.result = claude_ingest.run(cls.con, full=True, verbose=False)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def q(self, sql, *params):
        return self.con.execute(sql, params).fetchone()[0]

    def test_ingest_summary(self):
        self.assertTrue(self.result["ok"])
        self.assertEqual(self.result["files_read"], 2)
        self.assertEqual(self.result["history_lines"], 1)

    def test_usage_counted_once_per_response(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM cc_message WHERE session_id=?", SESSION), 5)
        self.assertEqual(self.q("SELECT SUM(CASE WHEN usage_dupe=0 THEN 1 ELSE 0 END) FROM cc_message WHERE session_id=?",
                                SESSION), 4)
        self.assertEqual(self.q("SELECT SUM(input_tokens) FROM cc_message WHERE session_id=?", SESSION), 400)

    def test_prompts_classified(self):
        rows = self.con.execute(
            "SELECT text, is_human, is_slash, slash_name, source FROM cc_prompt WHERE session_id=? ORDER BY ts",
            (SESSION,)).fetchall()
        texts = [r["text"] for r in rows]
        self.assertNotIn("[Request interrupted by user]", texts)
        self.assertEqual(sum(r["is_human"] for r in rows), 4)  # 3 typed + the slash command
        slash = [r for r in rows if r["is_slash"]][0]
        self.assertEqual(slash["slash_name"], "/commit")
        sdk = [r for r in rows if r["source"] == "sdk"][0]
        self.assertEqual(sdk["is_human"], 0)
        # Harness noise keeps a (non-human) row so automated counts stay honest.
        noise = [r for r in rows if r["source"] == "harness"]
        self.assertEqual(len(noise), 1)
        self.assertEqual(noise[0]["is_human"], 0)

    def test_events_and_errors(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM cc_event WHERE kind='interrupt'"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM cc_event WHERE kind='compact'"), 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM cc_message WHERE error_kind='usage_limit'"), 1)

    def test_git_and_tickets(self):
        row = self.con.execute("SELECT * FROM cc_git WHERE kind='commit'").fetchone()
        self.assertEqual((row["key"], row["branch"], row["insertions"], row["deletions"]), ("abc1234", "feature/DEMO-42", 2, 1))
        evidence = {r["evidence"] for r in self.con.execute("SELECT evidence FROM cc_ticket WHERE key='DEMO-42'")}
        self.assertTrue({"commit", "branch", "prompt", "title"} <= evidence, evidence)

    def test_lines_and_project_label(self):
        self.assertEqual(self.q("SELECT SUM(lines_added) FROM cc_tool_call"), 2)
        self.assertEqual(self.q("SELECT DISTINCT project FROM cc_message WHERE session_id=?", SESSION), "demo")

    def test_incremental_run_is_noop(self):
        res = claude_ingest.run(self.con, full=False, verbose=False)
        self.assertEqual(res["files_read"], 0)
        self.assertEqual(res["history_lines"], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM cc_message"), 6)
        self.assertEqual(self.q("SELECT COUNT(*) FROM cc_history"), 1)

    def test_branch_evidence_does_not_accumulate(self):
        # Re-reading an open session must not bump the branch count.
        self.con.execute("UPDATE ingest_state SET byte_offset=0, size=0 WHERE key LIKE ?", (f"%{SESSION}%",))
        self.con.commit()
        claude_ingest.run(self.con, full=False, verbose=False)
        self.assertEqual(self.q("SELECT n FROM cc_ticket WHERE key='DEMO-42' AND evidence='branch'"), 1)

    def test_payload_and_insights(self):
        payload = stats.build_payload(self.con, {}, with_insights=True)
        json.dumps(payload)  # must be serialisable
        ov = payload["claude"]["overview"]
        self.assertEqual(ov["prompts"], 5)
        self.assertNotIn("insights", stats.build_payload(self.con, {}))
        ins = payload["insights"]
        self.assertEqual(ins["delivery"]["commits"], 1)
        self.assertEqual(ins["delivery"]["tickets"][0]["status"], "delivered")
        self.assertEqual(ins["friction"]["errors"][0]["error_kind"], "usage_limit")
        self.assertEqual(ins["habits"]["interrupts"], 1)
        self.assertEqual(ins["habits"]["compactions"], 1)
        self.assertEqual(ins["history"]["lost_project_count"], 1)
        # The filter must not crash and must narrow the range.
        narrowed = stats.build_payload(self.con, {"since": "2026-08-01", "project": "demo"})
        self.assertEqual(narrowed["claude"]["overview"]["sessions"], 1)

    def test_skill_mining_finds_repeat(self):
        mining = insights.skill_mining(self.con, {})
        fams = [g for g in mining["families"] if g["key"] == "commit changes"]
        self.assertEqual(len(fams), 1)
        self.assertEqual((fams[0]["size"], fams[0]["sessions"]), (3, 2))
        pack = insights.evidence_pack(self.con, {}, mining)
        self.assertIn("commit changes", pack)

    def test_exports(self):
        payload = stats.build_payload(self.con, {}, with_insights=True)
        html = reports.report_html(payload, "T <x></script>")
        self.assertIn("window.__AIDASH_DATA__", html)
        self.assertIn("T &lt;x&gt;", html)
        boot = html.split("window.__AIDASH_REPORT__ = ")[1].split(";" + chr(10))[0]
        self.assertNotIn("</script>", boot)
        self.assertNotIn("</script>\"", html.split("window.__AIDASH_DATA__")[1].split("</script>")[0])
        corpus = reports.corpus_text(self.con, {})
        self.assertIn("SESSION 11111111", corpus)
        self.assertIn("commit changes", corpus)
        self.assertNotIn("internal sdk prompt", corpus)


if __name__ == "__main__":
    unittest.main()
