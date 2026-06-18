"""maintain CLI email paths — no network, no SMTP, no writes. Run: python -m unittest -v

Covers the safety-critical guarantee: a crash before the summary is composed must
still send the heartbeat email (the `finally` in cmd_maintain). The happy and
aborted render paths are covered by the render smoke + live runs.
"""
import argparse
import unittest

from colophon import cli, maintain, oversight


def _args(**kw):
    d = {"limit": 20, "min_conf": 0.9, "apply": False, "force": False,
         "email": True, "notify": False}
    d.update(kw)
    return argparse.Namespace(**d)


def _boom(*a, **k):
    raise RuntimeError("grimmory unreachable")


class CrashStillEmails(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._send, self._run = oversight.send_email, maintain.run_maintain
        oversight.send_email = lambda subject, body: (self.sent.append((subject, body)), (True, "stub"))[1]

    def tearDown(self):
        oversight.send_email, maintain.run_maintain = self._send, self._run

    def test_crash_before_summary_still_emails(self):
        # run_maintain itself swallows phase failures, but an unexpected throw must
        # not eat the heartbeat: the finally sends a [CRASH] notice, then re-raises.
        maintain.run_maintain = _boom
        with self.assertRaises(RuntimeError):
            cli.cmd_maintain(_args(email=True), None, None)
        self.assertEqual(len(self.sent), 1)
        subject, body = self.sent[0]
        self.assertIn("[CRASH]", subject)
        self.assertIn("journalctl", body)

    def test_crash_without_email_flag_sends_nothing(self):
        maintain.run_maintain = _boom
        with self.assertRaises(RuntimeError):
            cli.cmd_maintain(_args(email=False), None, None)
        self.assertEqual(self.sent, [])


class IsNoteworthy(unittest.TestCase):
    """Only a run that changed something / errored / has a new stuck book pushes;
    a clean no-op stays silent so a frequent schedule does not spam."""

    def _res(self, **kw):
        d = {"ok": True, "backfill": {"healed": 0}, "resolve": {"proposals": []},
             "series": {"healed": 0, "regrouped": 0}, "stuck": [], "manual": []}
        d.update(kw)
        return d

    def test_clean_noop_is_silent(self):
        self.assertFalse(maintain.is_noteworthy(self._res()))

    def test_crash_and_phase_failure_surface(self):
        self.assertTrue(maintain.is_noteworthy(None))
        self.assertTrue(maintain.is_noteworthy(self._res(ok=False)))

    def test_routine_changes_stay_silent(self):
        # Heals/regroups are trusted — no push (you notice a wrong change yourself).
        self.assertFalse(maintain.is_noteworthy(self._res(series={"healed": 5, "regrouped": 3})))
        self.assertFalse(maintain.is_noteworthy(self._res(backfill={"healed": 2})))

    def test_unfixable_or_stuck_surfaces(self):
        self.assertTrue(maintain.is_noteworthy(self._res(manual=[{"book_id": 1}])))
        self.assertTrue(maintain.is_noteworthy(self._res(stuck=[{"book_id": 1}])))


class ConditionalNotify(unittest.TestCase):
    """--notify routes through push() and only fires on a noteworthy run."""

    def setUp(self):
        self.pushed = []
        self._push, self._run = maintain.push, maintain.run_maintain
        maintain.push = lambda subject, body: (self.pushed.append((subject, body)), (True, "stub"))[1]

    def tearDown(self):
        maintain.push, maintain.run_maintain = self._push, self._run

    def test_notify_pushes_on_a_crash(self):
        # A crash is noteworthy; --notify must push (and not silently swallow it).
        maintain.run_maintain = _boom
        with self.assertRaises(RuntimeError):
            cli.cmd_maintain(_args(notify=True, email=False), None, None)
        self.assertEqual(len(self.pushed), 1)
        self.assertIn("[CRASH]", self.pushed[0][0])

    def test_no_flags_pushes_nothing(self):
        maintain.run_maintain = _boom
        with self.assertRaises(RuntimeError):
            cli.cmd_maintain(_args(notify=False, email=False), None, None)
        self.assertEqual(self.pushed, [])


if __name__ == "__main__":
    unittest.main()
