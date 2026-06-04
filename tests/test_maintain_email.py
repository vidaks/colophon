"""maintain CLI email paths — no network, no SMTP, no writes. Run: python -m unittest -v

Covers the safety-critical guarantee: a crash before the summary is composed must
still send the heartbeat email (the `finally` in cmd_maintain). The happy and
aborted render paths are covered by the render smoke + live runs.
"""
import argparse
import unittest

from colophon import cli, maintain, oversight


def _args(**kw):
    d = {"limit": 20, "min_conf": 0.9, "apply": False, "force": False, "email": True}
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


if __name__ == "__main__":
    unittest.main()
