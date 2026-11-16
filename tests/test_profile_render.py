import sys
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from security_notebook_tools import _profile_render as render
from security_notebook_tools import _render
from security_notebook_tools._profile import profile_dict
from security_notebook_tools._profile_render import (
    _Stats,
    fmt_bytes,
    fmt_count,
    fmt_seconds,
    show_batch_profile,
    show_profile,
    summary_rows,
    window_table,
)
from security_notebook_tools._render import PlotlyMissingError

SENTINEL = {
    "query": {
        "executionTime": 1.5,
        "resourceUsage": {
            "cache": {
                "memory": {"hits": 90, "misses": 10, "total": 100},
                "disk": {"hits": 0, "misses": 0, "total": 0},
                "shards": {
                    "hot": {"hitbytes": 3 * 1024, "missbytes": 1024, "retrievebytes": 0},
                    "bypassbytes": 2048,
                },
            },
            "cpu": {"user": "00:00:02", "kernel": "00:00:00.5", "total cpu": "00:00:02.5"},
            "memory": {"peakPerNode": 5 * 1024**2},
            "network": {"interClusterTotalBytes": 1024, "crossClusterTotalBytes": 0},
        },
        "inputDatasetStatistics": {
            "extents": {
                "total": 10,
                "scanned": 5,
                "scannedMinDatetime": "2026-01-01T00:00:00Z",
                "scannedMaxDatetime": "2026-01-02T00:00:00Z",
            },
            "rows": {"total": 1000000, "scanned": 250000},
            "rowstores": {"scannedRows": 3, "scannedValuesSize": 2048},
        },
        "datasetStatistics": [{"tableRowCount": 5, "tableSize": 1024}],
        "extra": {"odd": 1},
    }
}
ADX = {
    "ExecutionTime": 0.25,
    "resource_usage": {
        "cpu": {"total cpu": "00:00:01"},
        "memory": {"peak_per_node": 2048},
    },
    "input_dataset_statistics": {"rows": {"total": 10, "scanned": 5}},
    "dataset_statistics": [{"table_row_count": 7, "table_size": 512}],
}


def _by_metric(rows):
    return {(r.section, r.metric): r for r in rows}


def test_formatters():
    assert fmt_count(1234567) == "1 234 567"
    assert fmt_bytes(512) == "512 B"
    assert fmt_bytes(1536) == "1.5 KiB"
    assert fmt_bytes(5 * 1024**4) == "5.0 TiB"
    assert fmt_bytes(5000 * 1024**4) == "5000.0 TiB"
    assert fmt_bytes(1023.6) == "1.0 KiB"
    assert fmt_bytes(1024**2 - 1) == "1.0 MiB"
    assert fmt_seconds(0.0156) == "15.6 ms"
    assert fmt_seconds(1.2345) == "1.23 s"
    assert fmt_seconds(0.9996) == "1 s"
    assert fmt_seconds(1234) == "1 234 s"


def test_key_lookup_matches_sentinel_and_adx_spellings():
    sentinel = _Stats(profile_dict(SENTINEL, 1))
    adx = _Stats(profile_dict(ADX, 1))
    assert sentinel.number("resourceusage.memory.peakpernode") == 5 * 1024**2
    assert adx.number("resourceusage.memory.peakpernode") == 2048
    assert sentinel.seconds("resourceusage.cpu.totalcpu") == 2.5
    assert adx.seconds("resourceusage.cpu.totalcpu") == 1
    assert adx.seconds("executiontime") == 0.25


def test_sentinel_summary_sections_and_shares():
    rows = summary_rows(_Stats(profile_dict(SENTINEL, 2), (4, 2)))
    assert list(dict.fromkeys(r.section for r in rows)) == [
        "Time",
        "CPU",
        "Memory",
        "Data scanned",
        "Cache",
        "Network",
        "Result",
        "Tables",
    ]
    by = _by_metric(rows)
    assert (
        by["Time", "Wall time (client)"].value == "2 s"
        or "2" in by["Time", "Wall time (client)"].value
    )
    assert by["CPU", "Total"].value == "2.5 s"
    assert by["Memory", "Peak per node"].value == "5.0 MiB"
    rows_scanned = by["Data scanned", "Rows scanned"]
    assert rows_scanned.value == "250 000 of 1 000 000"
    assert rows_scanned.share == 25
    assert by["Data scanned", "Scanned time range"].value.count("→") == 1
    assert by["Cache", "Memory cache hits"].share == 90
    assert ("Cache", "Disk cache hits") not in by
    assert by["Cache", "Hot shard hit/miss"].share == 75
    assert "Cold shard hit/miss" not in {r.metric for r in rows}
    assert by["Result", "Rows returned"].value == "4"
    assert by["Result", "Columns returned"].value == "2"
    assert by["Tables", "Table 1"].value == "5 rows · 1.0 KiB"


def test_adx_summary_leaves_out_sections_without_data():
    rows = summary_rows(_Stats(profile_dict(ADX, 1)))
    assert list(dict.fromkeys(r.section for r in rows)) == [
        "Time",
        "CPU",
        "Memory",
        "Data scanned",
        "Tables",
    ]


def test_non_finite_values_are_skipped():
    stats = _Stats(
        {
            "query.inputDatasetStatistics.rows.scanned": 5,
            "query.inputDatasetStatistics.rows.total": float("nan"),
        }
    )
    assert stats.number("inputdatasetstatistics.rows.total") is None
    assert "Rows scanned" not in {r.metric for r in summary_rows(stats)}


def test_summary_without_statistics_has_a_note():
    rows = summary_rows(_Stats(profile_dict(None, 1), (1, 1)))
    assert [r.section for r in rows] == ["Time", "Result", "Result", "Note"]
    assert "no statistics" in rows[-1].value


def test_unused_keys_are_listed_in_all_statistics():
    profile = profile_dict(SENTINEL, 1)
    stats = _Stats(profile)
    summary_rows(stats)
    html = render._unused_html(profile, stats)
    assert "All statistics (4)" in html
    assert "query.extra.odd" in html
    assert "retrievebytes" in html
    assert "peakPerNode" not in html


def test_all_statistics_omitted_when_everything_is_used():
    profile = {"client_elapsed_seconds": 1}
    stats = _Stats(profile)
    summary_rows(stats)
    assert render._unused_html(profile, stats) is None


def _window(day, seconds, hot=None, cpu=None):
    stats = {"query": {"executionTime": seconds, "resourceUsage": {}}}
    if hot:
        stats["query"]["resourceUsage"]["cache"] = {"shards": {"hot": hot}}
    if cpu is not None:
        stats["query"]["resourceUsage"]["cpu"] = {"totalCpu": cpu}
    return {
        "window_start": datetime(2026, 1, day),
        "window_end": datetime(2026, 1, day + 1),
        **profile_dict(stats, 1),
    }


def test_window_table_rows_and_numeric_values():
    frame, caption = window_table(
        [_window(1, 2.0, {"hitbytes": 3, "missbytes": 1}), _window(2, 4.0)]
    )
    assert caption == "Windows"
    assert list(frame["Window"]) == ["2026-01-01 → 2026-01-02", "2026-01-02 → 2026-01-03"]
    assert list(frame["Execution time"]) == [2.0, 4.0]
    assert frame["Hot cache hit"].iloc[0] == 75
    assert frame["Hot cache hit"].isna().iloc[1]


def test_window_table_caps_at_the_slowest_rows():
    windows = [_window(1, float(i)) for i in range(250)]
    frame, caption = window_table(windows)
    assert len(frame) == 200
    assert frame["Execution time"].iloc[0] == 249
    assert "200 slowest of 250" in caption


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(render, "_in_ipython", lambda: False)


def test_single_query_text_on_stderr(plain, capsys):
    show_profile(profile_dict(SENTINEL, 1), (3, 2))
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines[0] == "Query profile"
    assert lines[1].split() == ["Section", "Metric", "Value", "Share"]
    assert any(line.startswith("Data scanned") and "25.0%" in line for line in lines)
    assert lines[-1] == "All values: df.attrs['profile']"
    assert "All statistics" not in err


def test_batch_text_on_stderr(plain, capsys):
    windows = [_window(1, 2.0), _window(2, 4.0)]
    show_batch_profile(windows, {"windows": 2, "client_elapsed_seconds": 3}, (2, 1))
    err = capsys.readouterr().err
    assert "Query profile (2 windows)" in err
    assert "Windows\nWindow" in err
    assert "2026-01-01 → 2026-01-02" in err


def test_ipython_displays_summary_and_details(monkeypatch):
    monkeypatch.setattr(render, "_in_ipython", lambda: True)
    display = mock.Mock()
    monkeypatch.setitem(sys.modules, "IPython.display", SimpleNamespace(display=display))
    show_profile(profile_dict(SENTINEL, 1), (1, 1))
    assert display.call_count == 2
    assert "Query profile" in display.call_args_list[0].args[0].to_html()
    assert "All statistics" in display.call_args_list[1].args[0]._repr_html_()


def test_ipython_batch_displays_totals_and_windows(monkeypatch):
    monkeypatch.setattr(render, "_in_ipython", lambda: True)
    display = mock.Mock()
    monkeypatch.setitem(sys.modules, "IPython.display", SimpleNamespace(display=display))
    show_batch_profile([_window(1, 2.0)], {"windows": 1, "client_elapsed_seconds": 1}, None)
    assert display.call_count == 2
    assert "Windows" in display.call_args_list[1].args[0].to_html()


def test_show_never_raises(monkeypatch):
    monkeypatch.setattr(render, "_in_ipython", lambda: True)
    monkeypatch.setitem(
        sys.modules, "IPython.display", SimpleNamespace(display=mock.Mock(side_effect=OSError))
    )
    show_profile(profile_dict(SENTINEL, 1))
    show_batch_profile([_window(1, 2.0)], {"windows": 1}, None)
    show_batch_profile([{"bad": 1}], {}, None)


def _batch_display(monkeypatch, windows):
    monkeypatch.setattr(render, "_in_ipython", lambda: True)
    display = mock.Mock()
    monkeypatch.setitem(sys.modules, "IPython.display", SimpleNamespace(display=display))
    show_batch_profile(windows, {"windows": len(windows), "client_elapsed_seconds": 1}, None)
    return display


def test_ipython_batch_shows_chart_of_execution_and_cpu_time(monkeypatch):
    windows = [_window(1, 2.0, cpu="00:00:01"), _window(2, 5.0, cpu="00:00:03")]
    display = _batch_display(monkeypatch, windows)
    assert display.call_count == 3
    execution, cpu = display.call_args_list[2].args[0].data
    assert list(execution.y) == [2.0, 5.0]
    assert list(cpu.y) == [1.0, 3.0]
    assert [pd.Timestamp(x).day for x in execution.x] == [1, 2]


def test_chart_leaves_out_cpu_when_no_window_reports_it(monkeypatch):
    display = _batch_display(monkeypatch, [_window(1, 2.0), _window(2, 5.0)])
    assert len(display.call_args_list[2].args[0].data) == 1


def test_no_chart_with_one_window(monkeypatch):
    assert _batch_display(monkeypatch, [_window(1, 2.0)]).call_count == 2


def test_no_chart_and_no_error_without_plotly(monkeypatch):
    def missing():
        raise PlotlyMissingError("no plotly")

    monkeypatch.setattr(_render, "_load_plotly", missing)
    assert _batch_display(monkeypatch, [_window(1, 2.0), _window(2, 5.0)]).call_count == 2


def test_batch_text_has_no_chart(plain, capsys):
    show_batch_profile(
        [_window(1, 2.0), _window(2, 5.0)], {"windows": 2, "client_elapsed_seconds": 3}, (2, 1)
    )
    assert "CPU time per window" not in capsys.readouterr().err
