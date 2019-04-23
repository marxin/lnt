from collections import namedtuple
from lnt.server.reporting.analysis import REGRESSED, UNCHANGED_FAIL
from lnt.server.reporting.report import RunResult, RunResults, report_css_styles, is_user_machine_name
from lnt.util import multidict
import lnt.server.reporting.analysis
import lnt.server.ui.app
import sqlalchemy.sql
from sqlalchemy import or_
import urllib
import timeago

from itertools import groupby

from datetime import *
from dateutil.relativedelta import relativedelta
from lnt.server.reporting.analysis import *
from lnt.server.reporting.report import *

class MiniResult:
    def __init__(self, value, difference, hash, bigger_is_better):
        self.value = value
        self.delta = difference
        self.abs_delta = abs(difference)
        self.hash_rgb_color = hash
        self.hash = hash
        self.bigger_is_better = bigger_is_better

    def get_value_status(self, min_percentage_change):
        return IMPROVED if self.abs_delta >= min_percentage_change else UNCHANGED_PASS

class MiniComparisonResult:
    def __init__(self, values, hashes, bigger_is_better, all_changes):
        self.values = []
        self.min_sample = min(values)
        self.max_sample = max(values)
        self.all_changes = all_changes

        b = values[0]
        rgb_colors = get_rgb_colors_for_hashes(hashes)
        for i in range(len(values)):
            self.values.append(MiniResult(values[i], values[i] / b - 1, rgb_colors[i], bigger_is_better))

    def is_interesting(self, min_percentage_change):
        return any(map(lambda x: x.abs_delta > min_percentage_change, self.get_interesting_values()))

    def get_absolute_difference(self):
        return max([x.abs_delta for x in self.get_interesting_values()])

    def get_interesting_values(self):
        return self.values if self.all_changes else [self.values[-1]]

class LatestRunsReport(object):
    def __init__(self, ts, younger_in_days, older_in_days, all_changes, all_elf_detail_stats,
            revisions, min_percentage_change, ignore_user_branches):
        self.ts = ts
        self.younger_in_days = younger_in_days
        self.older_in_days = older_in_days
        self.all_changes = all_changes
        self.all_elf_detail_stats = all_elf_detail_stats
        self.revisions = revisions
        self.min_percentage_change = min_percentage_change
        self.hash_of_binary_field = self.ts.Sample.get_hash_of_binary_field()
        self.fields = list(ts.Sample.get_metric_fields())
        self.ignore_user_branches = ignore_user_branches

        # Computed values.
        self.result_table = None

    def build(self, session):
        ts = self.ts

        tests = session.query(ts.Test).all();
        machines = session.query(ts.Machine).all()
        runs = session.query(ts.Run).all()

        younger_than = datetime.now() - relativedelta(days = self.younger_in_days)
        older_than = datetime.now() - relativedelta(days = self.older_in_days)
        q = session.query(ts.Sample) \
            .join(ts.Run) \
            .join(ts.Test) \
            .order_by(ts.Run.machine_id, ts.Test.id, ts.Run.start_time)

        if not self.all_elf_detail_stats:
            q = q.filter(sqlalchemy.not_(ts.Test.name.contains('elf/')))

        filtered_revisions = [r.strip() for r in self.revisions.split(',') if r]

        if len(filtered_revisions) > 0:
            revisions_filters = [ts.Order.llvm_project_revision.contains(x) for x in filtered_revisions]
            q = q.join(ts.Order).filter(ts.Run.order_id == ts.Order.id)
            q = q.filter(or_(*revisions_filters))
        else:
            q = q.filter(ts.Sample.run_id == ts.Run.id) \
            .filter(ts.Sample.test_id == ts.Test.id) \
            .filter(ts.Run.start_time >= younger_than) \
            .filter(ts.Run.start_time <= older_than)

        samples = q.all()

        self.result_table = []
        for field in self.fields:
            field_results = []

            for machine, g in groupby(samples, lambda x: x.run.machine):
                machine_results = []
                machine_samples = list(g)
                revisions = None

                if not 'trunk' in machine.name:
                    continue

                if self.ignore_user_branches and is_user_machine_name(machine.name):
                    continue

                for test, g in groupby(machine_samples, lambda x: x.test):
                    test_samples = list(g)

                    if len(filtered_revisions) > 0:
                        test_samples = [next((t for t in test_samples if r in t.run.order.llvm_project_revision), None) for r in filtered_revisions]
                        machine_runs = [t for t in test_samples if t]

                    if len(test_samples) < 2:
                        continue

                    values = [x.get_field(field) for x in test_samples]
                    if any(map(lambda x: x == None, values)):
                        continue

                    cr = MiniComparisonResult(values, [x.get_field(self.hash_of_binary_field) for x in test_samples],
                            field.bigger_is_better, self.all_changes)
                    if cr.is_interesting(self.min_percentage_change):
                        machine_results.append((test, cr))
                    revisions = list(reversed([s.run.order.llvm_project_revision for s in test_samples]))
                    agos = list(reversed([timeago.format(s.run.start_time, datetime.now()).replace(' ago', '') for s in test_samples]))

                if len(machine_results) > 0:
                    machine_results.sort(key = lambda x: x[1].get_absolute_difference(), reverse = True)
                    field_results.append((machine, zip(revisions, agos), machine_results))
            field_results.sort(key = lambda x: x[0].name)
            self.result_table.append((field, field_results))
