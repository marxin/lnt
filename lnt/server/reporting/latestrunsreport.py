from collections import namedtuple
from lnt.server.reporting.analysis import REGRESSED, UNCHANGED_FAIL
from lnt.util import multidict
import colorsys
import datetime
import lnt.server.reporting.analysis
import lnt.server.ui.app
import re
import sqlalchemy.sql
import urllib

OrderAndHistory = namedtuple('OrderAndHistory', ['max_order', 'recent_orders'])


def _pairs(list):
    return zip(list[:-1], list[1:])


# The hash color palette avoids green and red as these colours are already used
# in quite a few places to indicate "good" or "bad".
_hash_color_palette = (
    colorsys.hsv_to_rgb(h=45. / 360, s=0.3, v=0.9999),  # warm yellow
    colorsys.hsv_to_rgb(h=210. / 360, s=0.3, v=0.9999),  # blue cyan
    colorsys.hsv_to_rgb(h=300. / 360, s=0.3, v=0.9999),  # mid magenta
    colorsys.hsv_to_rgb(h=150. / 360, s=0.3, v=0.9999),  # green cyan
    colorsys.hsv_to_rgb(h=225. / 360, s=0.3, v=0.9999),  # cool blue
    colorsys.hsv_to_rgb(h=180. / 360, s=0.3, v=0.9999),  # mid cyan
)


def _clamp(v, minVal, maxVal):
    return min(max(v, minVal), maxVal)


def _toColorString(col):
    r, g, b = [_clamp(int(v * 255), 0, 255)
               for v in col]
    return "#%02x%02x%02x" % (r, g, b)


def _get_rgb_colors_for_hashes(hash_strings):
    hash2color = {}
    unique_hash_counter = 0
    for hash_string in hash_strings:
        if hash_string is not None:
            if hash_string in hash2color:
                continue
            hash2color[hash_string] = _hash_color_palette[unique_hash_counter]
            unique_hash_counter += 1
            if unique_hash_counter >= len(_hash_color_palette):
                break
    result = []
    for hash_string in hash_strings:
        if hash_string is None:
            result.append(None)
        else:
            # If not one of the first N hashes, return rgb value 0,0,0 which is
            # white.
            rgb = hash2color.get(hash_string, (0.999, 0.999, 0.999))
            result.append(_toColorString(rgb))
    return result


# Helper classes to make the sparkline chart construction easier in the jinja
# template.
class RunResult:
    def __init__(self, comparisonResult):
        self.cr = comparisonResult
        self.hash = self.cr.cur_hash
        self.samples = self.cr.samples
        if self.samples is None:
            self.samples = []


class RunResults:
    """
    RunResults contains pre-processed data to easily construct the HTML for
    a single row in the results table, showing how one test on one board
    evolved over a number of runs/days.
    """
    def __init__(self):
        self.results = []
        self._complete = False
        self.min_sample = None
        self.max_sample = None

    def __getitem__(self, i):
        return self.results[i]

    def __len__(self):
        return len(self.results)

    def append(self, result):
        assert not self._complete
        self.results.append(result)

    def complete(self):
        """
        complete() needs to be called after all appends to this object, but
        before the data is used the jinja template.
        """
        self._complete = True
        all_samples = []
        for dr in self.results:
            if dr is None:
                continue
            if dr.cr.samples is not None and not dr.cr.failed:
                all_samples.extend(dr.cr.samples)
        if len(all_samples) > 0:
            self.min_sample = min(all_samples)
            self.max_sample = max(all_samples)
        hashes = []
        for dr in self.results:
            if dr is None:
                hashes.append(None)
            else:
                hashes.append(dr.hash)
        rgb_colors = _get_rgb_colors_for_hashes(hashes)
        for i, dr in enumerate(self.results):
            if dr is not None:
                dr.hash_rgb_color = rgb_colors[i]

class LatestRunsReport(object):
    def __init__(self, ts, run_count):
        self.ts = ts
        self.run_count = run_count
        self.hash_of_binary_field = self.ts.Sample.get_hash_of_binary_field()
        self.fields = list(ts.Sample.get_metric_fields())

        # Computed values.
        self.result_table = None

    def build(self, session):
        ts = self.ts

        machines = session.query(ts.Machine).all()

        self.result_table = []
        for field in self.fields:
            field_results = []
            for machine in machines:
                machine_results = []
                machine_runs = list(reversed(session.query(ts.Run)
                    .filter(ts.Run.machine_id == machine.id)
                    .order_by(ts.Run.start_time.desc())
                    .limit(self.run_count)
                    .all()))

                if len(machine_runs) < 2:
                    continue

                machine_runs_ids = [r.id for r in machine_runs]

                # take all tests from latest run and do a comparison
                oldest_run = machine_runs[0]

                run_tests = session.query(ts.Test) \
                        .join(ts.Sample) \
                        .join(ts.Run) \
                        .filter(ts.Sample.run_id == oldest_run.id) \
                        .filter(ts.Sample.test_id == ts.Test.id) \
                        .all()

                # Create a run info object.
                sri = lnt.server.reporting.analysis.RunInfo(session, ts, machine_runs_ids)

                # Build the result table of tests with interesting results.
                def compute_visible_results_priority(visible_results):
                    # We just use an ad hoc priority that favors showing tests with
                    # failures and large changes. We do this by computing the priority
                    # as tuple of whether or not there are any failures, and then sum
                    # of the mean percentage changes.
                    test, results = visible_results
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
                            [machine_runs[-1]], [oldest_run], test.id, field,
                            self.hash_of_binary_field)

                    # If the result is not "interesting", ignore it.
                    if not cr.is_result_interesting():
                        continue

                    # For all previous runs, analyze comparison results
                    test_results = RunResults()

                    for run in machine_runs:
                        cr = sri.get_comparison_result(
                                [run], [oldest_run], test.id, field,
                                self.hash_of_binary_field)
                        test_results.append(RunResult(cr))

                    test_results.complete()

                    machine_results.append((test, test_results))

                machine_results.sort(key=compute_visible_results_priority)

                # If there are visible results for this test, append it to the
                # view.
                if machine_results:
                    field_results.append((machine, len(machine_runs), machine_results))

            field_results.sort(key = lambda x: x[0].name)
            self.result_table.append((field, field_results))

    def render(self, ts_url, only_html_body=True):
        # Strip any trailing slash on the testsuite URL.
        if ts_url.endswith('/'):
            ts_url = ts_url[:-1]

        env = lnt.server.ui.app.create_jinja_environment()
        template = env.get_template('reporting/latest_runs_report.html')

        # Compute static CSS styles for elements. We use the style directly on
        # elements instead of via a stylesheet to support major email clients
        # (like Gmail) which can't deal with embedded style sheets.
        #
        # These are derived from the static style.css file we use elsewhere.
        styles = {
            "body": ("color:#000000; background-color:#ffffff; "
                     "font-family: Helvetica, sans-serif; font-size:9pt"),
            "table": ("font-size:9pt; border-spacing: 0px; "
                      "border: 1px solid black"),
            "th": (
                "background-color:#eee; color:#666666; font-weight: bold; "
                "cursor: default; text-align:center; font-weight: bold; "
                "font-family: Verdana; padding:5px; padding-left:8px"),
            "td": "padding:5px; padding-left:8px",
        }

        return template.render(
            report=self, styles=styles, analysis=lnt.server.reporting.analysis,
            ts_url=ts_url, only_html_body=only_html_body)
