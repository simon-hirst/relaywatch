"""Strictly acknowledged active-turn steering with queued fallback."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from codex_steer import steer, UncertainDelivery, _active_turn
from watchdog import queue_codex_if_safe
from engine import atomic_write_json, load_json


class FakeConnection:
    def __init__(self, path, responses):
        self.responses = responses
        self.calls=[]
        self.closed=False
    def request(self, number, method, params):
        self.calls.append((number,method,params))
        reply=self.responses[method]
        if isinstance(reply, Exception):raise reply
        return reply
    def send(self, message):
        self.calls.append(('notification',message))
    def close(self): self.closed=True


class CodexSteeringTests(unittest.TestCase):
    def test_unavailable_socket_does_not_steer(self):
        self.assertFalse(steer('known-thread', 'Stop', socket_path=None,
            client_factory=lambda _: (_ for _ in ()).throw(OSError('no socket'))))

    def test_only_a_unique_running_turn_qualifies(self):
        self.assertEqual(_active_turn({'result':{'data':[{'id':'finished','status':'completed'},
                                  {'id':'running','status':'inProgress'}]}}),'running')
        with self.assertRaises(Exception):
            _active_turn({'result':{'data':[{'id':'finished','status':'completed'}]}})
        with self.assertRaises(Exception):
            _active_turn({'result':{'data':[{'id':'a','status':'inProgress'},
                                           {'id':'b','status':'inProgress'}]}})

    def test_steer_acknowledgement_is_explicit_and_same_turn(self):
        fake=FakeConnection('socket', {
            'initialize':{'result':{'userAgent':'codex'}},
            'thread/turns/list':{'result':{'data':[{'id':'turn-42','status':'inProgress'}]}},
            'turn/steer':{'result':{'turnId':'turn-42'}},
        })
        self.assertTrue(steer('owned-thread','stop now',socket_path='socket',client_factory=lambda _:fake))
        self.assertEqual(fake.calls[-1][1],'turn/steer')
        self.assertEqual(fake.calls[-1][2]['expectedTurnId'],'turn-42')
        self.assertEqual(fake.calls[-1][2]['threadId'],'owned-thread')
        self.assertTrue(fake.closed)

    def test_timeout_after_steer_does_not_queue_duplicate(self):
        fake=FakeConnection('socket', {
            'initialize':{'result':{}},
            'thread/turns/list':{'result':{'data':[{'id':'turn-42','status':'inProgress'}]}},
            'turn/steer':TimeoutError('lost response'),
        })
        with self.assertRaises(UncertainDelivery):
            steer('owned-thread','stop now',socket_path='socket',client_factory=lambda _:fake)
        self.assertTrue(fake.closed)

    def test_idle_turn_falls_back_without_a_steer(self):
        fake=FakeConnection('socket', {
            'initialize':{'result':{}},
            'thread/turns/list':{'result':{'data':[{'id':'turn-42','status':'completed'}]}},
        })
        self.assertFalse(steer('owned-thread','next',socket_path='socket',client_factory=lambda _:fake))
        self.assertFalse(any(call[1]=='turn/steer' for call in fake.calls if len(call)>1))

    def test_watchdog_reports_actual_steer_vs_queue(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            wd=root/'.agent-watchdog';wd.mkdir()
            atomic_write_json(wd/'active-codex.json',{'known-thread':1050})
            managed={'agent':'codex','type':'tmux','outcome':'launched','session':'w'}
            usage={'stage':'handoff','stale':False,'triggered_at':1010,'prep_at':1000}
            config={'enable_codex_queue':True,'active_session_seconds':120}
            def fake_run(*a,**kw):return SimpleNamespace(returncode=0,stderr='',stdout='')
            with patch('watchdog.latest_worker',return_value=managed), \
                 patch('watchdog.tmux_exists',return_value=True), \
                 patch('watchdog.steer_codex',return_value=True), \
                 patch('watchdog.pending_question',return_value={}), \
                 patch('watchdog.codex_question_open',return_value=False), \
                 patch('watchdog.find_cli',side_effect=AssertionError('queue must not be attempted')):
                msg=queue_codex_if_safe(root,config,usage,now=1070,run=fake_run)
            self.assertIn('MESSAGE STEERED',msg)
            self.assertEqual(load_json(wd/'codex-queued.json')['delivery'],'steered')
            # Same threshold must never resend an acknowledged steering message.
            self.assertIsNone(queue_codex_if_safe(root,config,usage,now=1071))

    def test_uncertain_steer_never_falls_back_or_retries(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); wd=root/'.agent-watchdog';wd.mkdir()
            atomic_write_json(wd/'active-codex.json',{'known-thread':1050})
            usage={'stage':'prepare','stale':False,'prep_at':1010}
            managed={'agent':'codex','type':'tmux','outcome':'launched','session':'w'}
            with patch('watchdog.latest_worker',return_value=managed), \
                 patch('watchdog.tmux_exists',return_value=True), \
                 patch('watchdog.steer_codex',side_effect=UncertainDelivery('unconfirmed')), \
                 patch('watchdog.pending_question',return_value={}), \
                 patch('watchdog.codex_question_open',return_value=False), \
                 patch('watchdog.find_cli',side_effect=AssertionError('no duplicate')):
                result=queue_codex_if_safe(root,{'enable_codex_queue':True},usage,now=1070)
                self.assertIn('DELIVERY UNCONFIRMED',result)
                self.assertIsNone(queue_codex_if_safe(root,{'enable_codex_queue':True},usage,now=1300))

    def test_approval_blocks_proactive_steering(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); (root/'.agent-watchdog').mkdir()
            with patch('watchdog.pending_question',return_value={'question':'Can I deploy?'}):
                self.assertIsNone(queue_codex_if_safe(root,{'enable_codex_queue':True},
                                     {'stage':'handoff','triggered_at':1000,'stale':False}))

if __name__=='__main__':unittest.main()
