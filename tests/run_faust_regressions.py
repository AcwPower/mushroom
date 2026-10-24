"""Isolated functional regression runner; no production database or load test."""
import os,sys,unittest
os.environ.pop('DATABASE_URL',None)
os.environ['DB_ENGINE']='sqlite'
os.environ['APP_ENV']='development'
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import server

class Result(unittest.TextTestResult):
    def startTest(self,test):
        # Every test has independent accounts/sessions; reset the process-wide login bucket too.
        server.RATE_BUCKETS.clear()
        super().startTest(test)

names=sys.argv[1:] or ['test_dorr.DorrTests','test_faust.FaustTests','test_boss_scope.BossTests',
                      'test_member_activity.MemberActivityTests','test_ordinary_admin.OrdinaryAdminTests',
                      'test_view_optimizations.ViewOptimizationTests']
result=unittest.TextTestRunner(verbosity=1,resultclass=Result).run(unittest.defaultTestLoader.loadTestsFromNames(names))
sys.exit(not result.wasSuccessful())
