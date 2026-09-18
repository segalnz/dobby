from pathlib import Path
import tempfile
import time
import unittest
from automation import AutomationStore, AutomationRunner

class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'jobs.db'
        self.store = AutomationStore(self.path)
        self.sent = []
        self.runner = AutomationRunner(self.store, lambda *args: self.sent.append(args) or True)
    def tearDown(self): self.tmp.cleanup()

    def test_due_value_persists_and_executes_once(self):
        job = self.store.add({'device':'lamp','action':'set_level','value':0.5}, due=time.time()-1)
        restarted = AutomationStore(self.path, overdue_policy='run')
        runner = AutomationRunner(restarted, lambda *args: self.sent.append(args) or True)
        runner.tick(); runner.tick(); runner.execute(job)
        self.assertEqual(self.sent, [('lamp','set_level',0.5)])
        self.assertEqual(restarted.pending(), [])

    def test_restart_skips_overdue_but_keeps_future(self):
        self.store.add({'device':'lamp','action':'off'}, due=time.time()-1)
        future = self.store.add({'device':'lamp','action':'on'}, due=time.time()+100)
        restarted = AutomationStore(self.path, overdue_policy='skip')
        self.assertEqual([j['id'] for j in restarted.pending()], [future])
        self.assertIn('overdue', restarted.reports()[0][1])

    def test_interrupted_publication_is_not_replayed(self):
        job = self.store.add({'device':'lamp','action':'on'}, due=0)
        self.assertIsNotNone(self.store.claim(job))
        restarted = AutomationStore(self.path, overdue_policy='run')
        runner = AutomationRunner(restarted, lambda *args: self.sent.append(args) or True)
        runner.tick(); runner.execute(job)
        self.assertEqual(self.sent, [])
        self.assertIn('interrupted', restarted.reports()[0][1])

    def test_condition_expiry_and_duplicate_triggers(self):
        expired = self.store.add({'device':'lamp','action':'on','topic':'sensor'}, expires=time.time()-1)
        live = self.store.add({'device':'lamp','action':'set_level','value':0.3,'topic':'sensor'}, expires=time.time()+60)
        for job in (expired, live, live): self.runner.trigger(job)
        self.runner.tick()
        self.assertEqual(self.sent, [('lamp','set_level',0.3)])
        self.assertIn('expired', self.store.reports()[0][1])

    def test_failures_and_reports_survive_restart(self):
        self.store.add({'device':'lamp','action':'off'}, due=0)
        def fail(*args): raise OSError('broker down')
        AutomationRunner(self.store, fail).tick()
        restarted = AutomationStore(self.path)
        reports = restarted.reports()
        self.assertEqual(len(reports), 1)
        self.assertIn('could not confirm', reports[0][1])
        restarted.acknowledge_report(reports[0][0])
        self.assertEqual(AutomationStore(self.path).reports(), [])
