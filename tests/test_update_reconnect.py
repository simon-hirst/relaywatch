"""Monitor-only release update must retain and reattach managed agent sessions."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine import atomic_write_json
from runner import session_name
from watchdog import reconnect_existing_monitor
from console_ui import DashboardUI


class UpdateReconnectTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        (self.root/'.agent-watchdog').mkdir()
        self.config={'auto_open_terminal_window':True}
        self.worker=self.root/'.agent-watchdog'/'worker.json'

    def save(self,agent='claude',session=None):
        atomic_write_json(self.worker,{'type':'tmux','outcome':'launched',
            'session':session or session_name(self.root),'window':'@4',
            'agent':agent,'startup_status':'unverified'})

    def test_reconnect_reuses_existing_tmux_and_never_launches_agent(self):
        self.save()
        with patch('watchdog.tmux_exists',return_value=True) as exists, \
             patch('watchdog.show_agent_window',
                   return_value='existing terminal already attached') as show:
            text=reconnect_existing_monitor(self.root,self.config)
        self.assertIn('already attached',text)
        exists.assert_called_once()
        show.assert_called_once_with(self.root,self.config,'claude')

    def test_unavailable_or_unowned_session_does_not_open_anything(self):
        self.save(session='agent-handoff-unknown')
        with patch('watchdog.tmux_exists',side_effect=AssertionError('wrong session')), \
             patch('watchdog.show_agent_window') as show:
            self.assertIsNone(reconnect_existing_monitor(self.root,self.config))
            show.assert_not_called()
        self.save()
        with patch('watchdog.tmux_exists',return_value=False), \
             patch('watchdog.show_agent_window') as show:
            self.assertIsNone(reconnect_existing_monitor(self.root,self.config))
            show.assert_not_called()
        self.config['auto_open_terminal_window']=False
        with patch('watchdog.tmux_exists',side_effect=AssertionError('disabled')), \
             patch('watchdog.show_agent_window') as show:
            self.assertIsNone(reconnect_existing_monitor(self.root,self.config))
            show.assert_not_called()

    def test_dashboard_calls_legacy_worker_unverified_not_failed(self):
        self.save()
        ui=DashboardUI(self.root)
        text,color=ui._pending()
        self.assertIn('session unverified',text)
        self.assertIn('attach',text)
        self.assertEqual(color,'yellow')


if __name__=='__main__':
    unittest.main()
