from collections import namedtuple
from lnt.server.reporting.analysis import REGRESSED, UNCHANGED_FAIL
from lnt.server.reporting.report import RunResult, RunResults, report_css_styles
from lnt.util import multidict
from itertools import groupby

from lnt.server.ui.globals import v4_url_for

import lnt.server.reporting.analysis
import lnt.server.ui.app
import sqlalchemy.sql
import urllib

class MachineName:
    def __init__(self, name):
        parts = name.split('.')
        self.machine = parts[0]
        self.suite = parts[1]
        self.compiler = parts[2]
        self.options = parts[3]

    def __str__(self):
        return '.'.join([self.machine, self.suite, self.compiler, self.options])

    def equal_apart_options(self, other):
        return (self.machine == other.machine
            and self.suite == other.suite
            and self.compiler == other.compiler)

    @staticmethod
    def get_groups_by_tuning(machines):
        generic_machines = [m for m in machines if m.mname.options.endswith('generic')]
        for g in generic_machines:
            for m in machines:
                if g.mname.equal_apart_options(m.mname) and g.mname.options.replace('generic', 'native') == m.mname.options:
                    g.title_name = 'generic'
                    m.title_name = 'native'
                    yield (str(g.machine.name).replace('generic', '*'), [g, m])
                    break

    @staticmethod
    def get_groups_by_options(machines):
        bases = ['O2_native', 'Ofast_native']
        for b in bases:
            keyfunc = lambda x: (x.mname.machine, x.mname.suite, x.mname.compiler)
            for k, g in groupby(sorted(machines, key = keyfunc), keyfunc):
                values = list(sorted(g, key = lambda x: (len(x.mname.options), 'lto' in x.mname.options)))

                for v in values:
                    v.title_name = v.mname.options

                yield ('.'.join(k) + '.*', [m for m in values if m.mname.options.startswith(b)])

    @staticmethod
    def get_groups_by_branches(machines):
        bases = ['O2_native', 'Ofast_native']
        keyfunc = lambda x: (x.mname.machine, x.mname.suite, x.mname.options)
        for k, g in groupby(sorted(machines, key = keyfunc), keyfunc):
            values = list(sorted(g, key = lambda x:x.mname.compiler))
            mname = values[0].mname
            group_name = '.'.join([mname.machine, mname.suite, '*', mname.options])

            for v in values:
                v.title_name = v.mname.compiler
            yield (group_name, values)

class MachineTuple:
    def __init__(self, machine):
        self.machine = machine
        self.mname = MachineName(machine.name)
        self.title_name = None
        self.run = None

class MachineGroup:
    def __init__(self, title_name, machines):
        self.title_name = title_name
        self.machines = machines

class SPECReport(object):
    def __init__(self, ts, report_type):
        self.ts = ts
        self.hash_of_binary_field = self.ts.Sample.get_hash_of_binary_field()
        self.fields = list(ts.Sample.get_metric_fields())
        self.report_type = report_type

        # Computed values.
        self.result_table = None

    def build(self, session):
        ts = self.ts

        # TODO: fix properly
        machines = [m for m in session.query(ts.Machine).all() if not 'honza' in m.name]
        machine_tuples = [MachineTuple(m) for m in machines]

        # take only groups with at least 2 members
        group_fn = None
        if self.report_type == 'branch':
            group_fn = MachineName.get_groups_by_branches
        elif self.report_type == 'tuning':
            group_fn = MachineName.get_groups_by_tuning
        elif self.report_type == 'options':
            group_fn = MachineName.get_groups_by_options

        machine_groups = [MachineGroup(x[0], x[1]) for x in group_fn(machine_tuples) if len(x[1]) >= 2]

        self.result_table = []
        for field in self.fields:
            field_results = []
            for machine_group in machine_groups:
                machine_group_results = []

                machine_runs = []
                for m in machine_group.machines:
                    runs = (session.query(ts.Run)
                        .filter(ts.Run.machine_id == m.machine.id)
                        .order_by(ts.Run.start_time.desc())
                        .limit(1)
                        .all())
                    # TODO
                    if len(runs) == 1:
                        m.run = runs[0]

                # TODO: latest branch must have a run, maybe a limitation
                for m in machine_group.machines:
                    if m.run == None:
                        continue

                group_runs_ids = [m.run.id for m in machine_group.machines if m.run != None]

                # take all tests from latest run and do a comparison
                latest_branch_run = machine_group.machines[-1].run
                oldest_branch_run = machine_group.machines[0].run

                run_tests = session.query(ts.Test) \
                        .join(ts.Sample) \
                        .join(ts.Run) \
                        .filter(ts.Sample.run_id == latest_branch_run.id) \
                        .filter(ts.Sample.test_id == ts.Test.id) \
                        .all()

                # Create a run info object.
                sri = lnt.server.reporting.analysis.RunInfo(session, ts, group_runs_ids)

                # Build the result table of tests with interesting results.
                def compute_visible_results_priority(visible_results):
                    # We just use an ad hoc priority that favors showing tests with
                    # failures and large changes. We do this by computing the priority
                    # as tuple of whether or not there are any failures, and then sum
                    # of the mean percentage changes.
                    test, graph, results = visible_results
                    had_failures = False
                    sum_abs_deltas = 0.
                    for result in results:
                        test_status = result.cr.get_test_status()

                        if (test_status == REGRESSED or test_status == UNCHANGED_FAIL):
                            had_failures = True
                        elif result.cr.pct_delta is not None:
                            sum_abs_deltas += abs(result.cr.pct_delta)
                    return (field.name, -int(had_failures), -sum_abs_deltas, test.name)

                for test in run_tests:
                    cr = sri.get_comparison_result(
                            [latest_branch_run], [oldest_branch_run], test.id, field,
                            self.hash_of_binary_field)

                    # For all previous runs, analyze comparison results
                    test_results = []

                    for m in machine_group.machines:
                        cr = sri.get_comparison_result(
                                [m.run], [oldest_branch_run], test.id, field,
                                self.hash_of_binary_field)
                        test_results.append(RunResult(cr))

                    # If all results are not "interesting", ignore them.
                    if all([not tr.cr.is_result_interesting() for tr in test_results]):
                        continue

                    graph_url = v4_url_for(".v4_graph") + '?'
                    for i, m in enumerate(machine_group.machines):
                        p = 'plot.%d=%d.%d.%d' % (i, m.machine.id, test.id, self.ts.get_field_index(field))
                        graph_url += p + '&'

                    machine_group_results.append((test, graph_url, test_results))

                machine_group_results.sort(key=compute_visible_results_priority)

                # If there are visible results for this test, append it to the
                # view.
                if machine_group_results:
                    field_results.append((machine_group, len(machine_group.machines), machine_group_results))

            field_results.sort(key=lambda x: x[0].title_name)
            self.result_table.append((field, field_results))

    def render(self, ts_url, only_html_body=True):
        # Strip any trailing slash on the testsuite URL.
        if ts_url.endswith('/'):
            ts_url = ts_url[:-1]

        env = lnt.server.ui.app.create_jinja_environment()
        template = env.get_template('reporting/spec_report.html')

        return template.render(
            report=self, styles=report_css_styles, analysis=lnt.server.reporting.analysis,
            ts_url=ts_url, only_html_body=only_html_body)
