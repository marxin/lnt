from collections import namedtuple
from lnt.server.reporting.analysis import REGRESSED, UNCHANGED_FAIL
from lnt.server.reporting.report import RunResult, RunResults, report_css_styles
from lnt.util import multidict
import lnt.server.reporting.analysis
import lnt.server.ui.app
import sqlalchemy.sql
import urllib

from itertools import groupby

from datetime import *
from dateutil.relativedelta import relativedelta

class MiniResult:
    def __init__(self, value, difference):
        self.value = value
        self.difference = difference

    def get_value_status(self, min_percentage_change):
        # TODO
        if self.difference < -min_percentage_change:
            return lnt.server.reporting.analysis.IMPROVED
        elif self.difference > min_percentage_change:
            return lnt.server.reporting.analysis.REGRESSED
        else:
            return lnt.server.reporting.analysis.UNCHANGED_PASS

class MiniComparisonResult:
    def __init__(self, values):
        b = values[0]
        self.values = [MiniResult(x, x / b - 1) for x in values]

    def is_interesting(self, min_percentage_change):
        return any(map(lambda x: abs(x.difference) > min_percentage_change, self.values))
        return abs(self.values[-1].difference) > min_percentage_change

class LatestRunsReport(object):
    def __init__(self, ts, run_count, all_changes, all_elf_detail_stats, revisions, min_percentage_change):
        self.ts = ts
        self.run_count = run_count
        self.all_changes = all_changes
        self.all_elf_detail_stats = all_elf_detail_stats
        self.revisions = revisions
        self.min_percentage_change = min_percentage_change
        self.hash_of_binary_field = self.ts.Sample.get_hash_of_binary_field()
        self.fields = list(ts.Sample.get_metric_fields())

        # Computed values.
        self.result_table = None

    def build(self, session):
        ts = self.ts

        tests = session.query(ts.Test).all();
        machines = session.query(ts.Machine).all()
        runs = session.query(ts.Run).all()

        time_limit = datetime.now() - relativedelta(days = self.run_count)
        samples = session.query(ts.Sample) \
            .join(ts.Run) \
            .join(ts.Test) \
            .filter(ts.Sample.run_id == ts.Run.id) \
            .filter(ts.Sample.test_id == ts.Test.id) \
            .filter(ts.Run.start_time >= time_limit) \
            .order_by(ts.Run.machine_id, ts.Test.id, ts.Run.start_time) \
            .all()

        self.result_table = []
        for field in self.fields:
            field_results = []

            for machine, g in groupby(samples, lambda x: x.run.machine):
                machine_results = []
                machine_samples = list(g)
                revisions = None

                for test, g in groupby(machine_samples, lambda x: x.test):
                    test_samples = list(g)
                    if len(test_samples) < 2:
                        continue
                    values = [x.get_field(field) for x in test_samples]
                    if any(map(lambda x: x == None, values)):
                        continue

                    cr = MiniComparisonResult(values)
                    if cr.is_interesting(self.min_percentage_change):
                        machine_results.append((test, cr.values))
                    revisions = list(reversed([s.run.order.llvm_project_revision for s in test_samples]))

                if len(machine_results) > 0:
                    field_results.append((machine, revisions, machine_results))
            self.result_table.append((field, field_results))

    def render(self, ts_url, only_html_body=True):
        # Strip any trailing slash on the testsuite URL.
        if ts_url.endswith('/'):
            ts_url = ts_url[:-1]

        env = lnt.server.ui.app.create_jinja_environment()
        template = env.get_template('reporting/latest_runs_report.html')

        return template.render(
            report=self, styles=report_css_styles, analysis=lnt.server.reporting.analysis,
            ts_url=ts_url, only_html_body=only_html_body)
