"""
Every controller's session, tracked, and the service that does not fall over.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import requests

from agentic_mcp import sessions
from agentic_mcp.queue import Queue
from agentic_mcp.session import Session


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.t = sessions.Tracker(os.path.join(self.dir, "sessions.json"))
        patch = mock.patch.object(sessions, "BREAKER", True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_controller_that_answers_is_up_and_its_answers_are_timed(self):
        for ms in (5, 7, 9):
            self.t.record("onos", 200, ms)
        got = self.t.view("onos")[0]
        self.assertEqual((got["state"], got["requests"], got["errors"], got["breaker"]), ("up", 3, 0, "closed"))
        self.assertEqual(got["answers_ms"]["median"], 7)

    def test_one_that_answers_with_errors_is_degraded_not_down(self):
        self.t.record("tfs", 500, 12, "HTTP 500: boom")
        got = self.t.view("tfs")[0]
        self.assertEqual((got["state"], got["errors"], got["last_error"]), ("degraded", 1, "HTTP 500: boom"))
        self.assertEqual(self.t.allow("tfs"), "")                 # it is there: it is still asked

    def test_one_that_does_not_answer_is_not_asked_again_for_a_while(self):
        for _ in range(sessions.OPEN_AFTER - 1):
            self.t.record("ryu", 0, 100, "ConnectionError: refused")
            self.assertEqual(self.t.allow("ryu"), "")             # one slow answer, or three, is not down
        self.t.record("ryu", 0, 100, "ConnectionError: refused")
        self.assertEqual(self.t.view("ryu")[0]["state"], "down")
        self.assertIn("did not answer 4 times in a row", self.t.allow("ryu"))
        self.assertEqual(self.t.view("ryu")[0]["breaker"], "open")

    def test_two_timeouts_in_a_row_open_the_breaker_where_refusals_need_four(self):
        # A timeout costs a whole timeout: one tool call making several requests
        # in turn held its first caller for each of them.
        self.t.record("tfs", 0, 3000, "ReadTimeout: slow")
        self.assertEqual(self.t.allow("tfs"), "")
        self.t.record("tfs", 0, 3000, "ConnectTimeout: no route")
        self.assertIn("did not answer", self.t.allow("tfs"))
        self.assertEqual(self.t.view("tfs")[0]["state"], "down")
        for _ in range(sessions.OPEN_AFTER - 1):
            self.t.record("ryu", 0, 5, "ConnectionError: refused")
            self.assertEqual(self.t.allow("ryu"), "")
        self.t.record("ryu", 0, 5, "ConnectionError: refused")
        self.assertIn("did not answer", self.t.allow("ryu"))

    def test_an_answer_between_timeouts_starts_the_count_again(self):
        self.t.record("tfs", 0, 3000, "ReadTimeout: slow")
        self.t.record("tfs", 200, 40)
        self.t.record("tfs", 0, 3000, "ReadTimeout: slow")
        self.assertEqual(self.t.allow("tfs"), "")

    def test_after_the_pause_one_request_is_let_through_and_its_answer_decides(self):
        for _ in range(sessions.OPEN_AFTER):
            self.t.record("ryu", 0, 100, "ConnectionError")
        self.t._s["ryu"]["open_until"] = 0.0                      # the pause has passed
        self.assertEqual(self.t.allow("ryu"), "")
        self.t.record("ryu", 200, 4)
        got = self.t.view("ryu")[0]
        self.assertEqual((got["state"], got["failures_in_a_row"], got["breaker"]), ("up", 0, "closed"))
        self.assertEqual([h["to"] for h in got["history"]], ["down", "up"])
        self.assertEqual(got["history"][-1]["why"], "answers again")

    def test_after_the_pause_only_one_request_goes_while_it_is_on_its_way(self):
        # Letting every waiting caller through at once held each of them for a whole timeout.
        for _ in range(sessions.OPEN_AFTER):
            self.t.record("ryu", 0, 100, "ConnectTimeout")
        self.t._s["ryu"]["open_until"] = 0.0
        self.assertEqual(self.t.allow("ryu"), "")                 # the probe
        self.assertIn("did not answer", self.t.allow("ryu"))       # the others are told at once
        self.assertIn("did not answer", self.t.allow("ryu"))
        self.t.record("ryu", 0, 3000, "ConnectTimeout")            # the probe failed: paused again
        self.assertIn("did not answer", self.t.allow("ryu"))
        self.assertGreater(self.t._s["ryu"]["pause_s"], sessions.PAUSE_S)

    def test_each_pause_is_longer_than_the_last_up_to_five_minutes(self):
        for _ in range(sessions.OPEN_AFTER):
            self.t.record("ryu", 0, 1, "x")
        first = self.t._s["ryu"]["pause_s"]
        self.t.record("ryu", 0, 1, "x")
        self.assertEqual(self.t._s["ryu"]["pause_s"], min(sessions.PAUSE_MAX_S, first * 2))

    def test_what_is_known_is_there_after_a_restart_and_the_breaker_is_not(self):
        self.t.record("onos", 200, 5)
        for _ in range(sessions.OPEN_AFTER):
            self.t.record("ryu", 0, 1, "ConnectionError")
        self.t._save()
        again = sessions.Tracker(self.t.path)
        got = {r["controller"]: r for r in again.view()}
        self.assertEqual((got["onos"]["state"], got["onos"]["requests"]), ("up", 1))
        self.assertEqual((got["ryu"]["state"], got["ryu"]["breaker"]), ("down", "closed"))   # it finds out for itself
        self.assertEqual(again.allow("ryu"), "")

    def test_a_command_run_once_records_and_never_holds_back(self):
        with mock.patch.object(sessions, "BREAKER", False):
            for _ in range(sessions.OPEN_AFTER + 2):
                self.t.record("ryu", 0, 1, "x")
            self.assertEqual(self.t.allow("ryu"), "")


class RequestTests(unittest.TestCase):
    """Every request to a controller passes one place, which tells the tracker."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.was = sessions.TRACKER
        sessions.TRACKER = sessions.Tracker(os.path.join(self.dir, "s.json"))
        self.addCleanup(setattr, sessions, "TRACKER", self.was)
        self.calls = []

    def session(self, answers):
        test = self

        class Http:
            def request(self, method, url, **kw):
                test.calls.append(method)
                got = answers.pop(0)
                if isinstance(got, Exception):
                    raise got

                class R:
                    status_code, headers, text = got, {"content-type": "application/json"}, "{}"
                return R()
        return Session({"key": "metro", "base_url": "http://m:1"}, env={}, http=Http(),
                       audit=os.path.join(self.dir, "audit.jsonl"))

    def test_a_read_that_got_no_answer_is_asked_once_more(self):
        s = self.session([requests.ConnectionError("reset"), 200])
        self.assertEqual(s.fetch("GET", "http://m:1/x")[0], 200)
        self.assertEqual(self.calls, ["GET", "GET"])
        self.assertEqual(sessions.TRACKER.view("metro")[0]["errors"], 0)

    def test_connecting_is_given_seconds_and_answering_the_whole_timeout(self):
        seen = []

        class Http:
            def request(self, method, url, **kw):
                seen.append(kw["timeout"])

                class R:
                    status_code, headers, text = 200, {}, "{}"
                return R()
        s = Session({"key": "metro", "base_url": "http://m:1"}, env={}, http=Http(),
                    audit=os.path.join(self.dir, "audit.jsonl"))
        s.fetch("GET", "http://m:1/x", timeout=20)
        from agentic_mcp import session as session_mod
        self.assertEqual(seen, [(session_mod.CONNECT_TIMEOUT, 20)])
        s.fetch("GET", "http://m:1/x", timeout=1)
        self.assertEqual(seen[-1], (1, 1))                         # never more than the whole timeout

    def test_the_probe_after_a_pause_is_not_given_the_whole_timeout(self):
        seen = []

        class Http:
            def request(self, method, url, **kw):
                seen.append(kw["timeout"][1])
                raise requests.ReadTimeout("slow")
        s = Session({"key": "metro", "base_url": "http://m:1"}, env={}, http=Http(),
                    audit=os.path.join(self.dir, "audit.jsonl"))
        with mock.patch.object(sessions, "BREAKER", True):
            s.fetch("GET", "http://m:1/x", timeout=20)
            s.fetch("GET", "http://m:1/x", timeout=20)                  # two timeouts: open
            self.assertIn("did not answer", s.fetch("GET", "http://m:1/x", timeout=20)[2])
            sessions.TRACKER._s["metro"]["open_until"] = 0.0             # the pause is over
            s.fetch("GET", "http://m:1/x", timeout=20)                  # the probe
        self.assertEqual(seen, [20, 20, sessions.PROBE_TIMEOUT_S])

    def test_a_write_is_never_sent_twice(self):
        s = self.session([requests.ConnectionError("reset"), 200])
        status, _, said = s.fetch("POST", "http://m:1/x", {"a": 1})
        self.assertEqual((status, self.calls), (0, ["POST"]))             # it may have arrived
        self.assertIn("ConnectionError", said)

    def test_a_read_that_timed_out_is_not_repeated(self):
        s = self.session([requests.Timeout("20 s"), 200])
        self.assertEqual(s.fetch("GET", "http://m:1/x")[0], 0)
        self.assertEqual(self.calls, ["GET"])                             # the caller already waited once

    def test_with_the_breaker_open_the_caller_is_told_at_once_and_nothing_is_sent(self):
        with mock.patch.object(sessions, "BREAKER", True):
            for _ in range(sessions.OPEN_AFTER):
                sessions.TRACKER.record("metro", 0, 1, "ConnectionError: refused")
            s = self.session([200])
            status, _, said = s.fetch("GET", "http://m:1/x")
        self.assertEqual((status, self.calls), (0, []))
        self.assertIn("it is asked again in", said)

    def test_something_no_controller_should_send_does_not_take_the_caller_down(self):
        s = self.session([ValueError("not http at all")])
        status, _, said = s.fetch("POST", "http://m:1/x", {})
        self.assertEqual(status, 0)
        self.assertIn("ValueError", said)


class QueueFileTests(unittest.TestCase):
    """What was approved, and what waits, is not lost to a damaged file."""

    def test_a_file_that_cannot_be_read_falls_back_to_the_last_good_one(self):
        path = os.path.join(tempfile.mkdtemp(), "queue.json")
        q = Queue(path)
        first = q.request("ryu", "ryu_map_vlan", "map", [{"method": "POST", "url": "u"}])
        q.request("ryu", "ryu_map_vlan", "map again", [{"method": "POST", "url": "u"}])
        with open(path, "w") as fh:
            fh.write('{"half a fi')                                       # a crash mid-write, a full disk
        again = Queue(path)
        self.assertIn(first["approval_id"], [i["id"] for i in again.pending("ryu")])   # one save back, not nothing
        self.assertTrue(os.path.exists(path + ".unreadable"))             # and the damaged one is kept to look at

    def test_no_file_at_all_is_an_empty_queue(self):
        self.assertEqual(Queue(os.path.join(tempfile.mkdtemp(), "queue.json")).pending("ryu"), [])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from agentic_mcp.desk import Desk
        from agentic_mcp.service import create_app
        self.data = tempfile.mkdtemp()
        self.was = sessions.TRACKER
        self.addCleanup(setattr, sessions, "TRACKER", self.was)
        self.desk = Desk(data=self.data, env={}, background=False)
        self.http = TestClient(create_app(self.desk), raise_server_exceptions=False)

    def test_an_error_inside_a_request_is_an_answer_with_a_reference_not_a_crash(self):
        with mock.patch.object(self.desk, "keys", side_effect=RuntimeError("the disk went away")):
            got = self.http.get("/controllers")
        self.assertEqual(got.status_code, 500)
        self.assertEqual((got.json()["ok"], got.json()["error"]), (False, "RuntimeError: the disk went away"))
        self.assertEqual(len(got.json()["ref"]), 8)
        self.assertEqual(self.http.get("/health").status_code, 200)       # and the service is still there

    def test_the_sessions_are_listed_for_whoever_asks(self):
        with open(os.path.join(self.data, "metro.manifest.json"), "w") as fh:
            json.dump({"key": "metro", "base_url": "http://127.0.0.1:9", "verbs": {}}, fh)
        sessions.TRACKER.record("metro", 200, 6.0)
        rows = self.http.get("/sessions").json()["sessions"]
        self.assertEqual([(r["controller"], r["state"], r["requests"]) for r in rows], [("metro", "up", 1)])
        self.assertIn("metro", self.http.get("/sessions", params={"text": "true"}).text)
        self.assertEqual(self.http.get("/health").json()["sessions"], {"metro": "up"})


if __name__ == "__main__":
    unittest.main()


class SideBySideTests(unittest.TestCase):
    """Any number of controllers, each on its own: one that hangs or fails
    costs the others nothing."""

    def test_one_slow_controller_does_not_hold_up_the_others(self):
        import time
        from agentic_mcp import sessions

        def ask(key):
            if key == "stuck":
                time.sleep(3)
            if key == "broken":
                raise RuntimeError("it fell over")
            return key.upper()
        keys = ["stuck", "broken"] + [f"c{i}" for i in range(40)]
        started = time.monotonic()
        got = sessions.each(keys, ask, wait_s=0.5)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(list(got), keys)                                  # in the order asked
        self.assertEqual([got[f"c{i}"]["value"] for i in range(40)], [f"C{i}" for i in range(40)])
        self.assertTrue(got["stuck"]["late"])
        self.assertIn("it fell over", got["broken"]["error"])

    def test_a_controller_has_a_share_of_its_own_and_no_more(self):
        from unittest import mock
        from agentic_mcp import sessions
        t = sessions.Tracker()
        with mock.patch.object(sessions, "BREAKER", True), mock.patch.object(sessions, "SLOTS", 3):
            self.assertEqual([t.enter("slow") for _ in range(3)], ["", "", ""])
            self.assertIn("all it is given", t.enter("slow"))             # told at once, not queued
            self.assertEqual(t.enter("other"), "")                         # and nobody else is affected
            t.leave("slow")
            self.assertEqual(t.enter("slow"), "")
            self.assertEqual(t._busy["slow"], 3)
