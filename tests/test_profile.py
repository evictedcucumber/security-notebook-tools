import json
from types import SimpleNamespace
from unittest import mock

import pytest

from security_notebook_tools._profile import (
    adx_statistics,
    profile_dict,
    total_profile,
)

STATS = {
    "query": {
        "executionTime": 0.0156,
        "resourceUsage": {
            "cache": {"shards": {"hot": {"hitbytes": 10}}},
            "cpu": {"user": "00:00:00"},
        },
        "datasetStatistics": [{"tableRowCount": 5}, {"tableRowCount": 7}],
    }
}


def _response(*rows, kind="QueryCompletionInformation"):
    table = SimpleNamespace(table_kind=kind, rows=list(rows))
    return SimpleNamespace(tables=[table])


def test_profile_dict_flattens_with_native_values():
    profile = profile_dict(STATS, 1.23456)
    assert list(profile)[0] == "client_elapsed_seconds"
    assert profile["client_elapsed_seconds"] == 1.235
    assert profile["query.executionTime"] == 0.0156
    assert profile["query.resourceUsage.cache.shards.hot.hitbytes"] == 10
    assert profile["query.datasetStatistics[1].tableRowCount"] == 7


@pytest.mark.parametrize("stats", [None, {}])
def test_profile_dict_without_statistics(stats):
    assert profile_dict(stats, 2) == {"client_elapsed_seconds": 2}


def test_total_profile():
    windows = [
        {
            "window_start": 1,
            "client_elapsed_seconds": 1.0,
            "rows": 3,
            "cpu": "00:00:01.5000000",
            "mem.peakPerNode": 10,
            "note": "text",
            "flag": True,
        },
        {"rows": 4, "cpu": "1.00:00:00.5", "mem.peakPerNode": 30, "note": "more", "flag": True},
    ]
    total = total_profile(windows, 9.87654)
    assert total == {
        "windows": 2,
        "client_elapsed_seconds": 9.877,
        "rows": 7,
        "cpu": "1.00:00:02",
        "mem.peakPerNode": 30,
    }


def test_adx_statistics_parsed():
    payload = json.dumps({"ExecutionTime": 1.5, "resource_usage": {"cpu": {"total": "x"}}})
    rows = [
        {"EventTypeName": "Other", "Payload": "{}"},
        {"EventTypeName": "QueryResourceConsumption", "Payload": payload},
    ]
    assert adx_statistics(_response(*rows))["ExecutionTime"] == 1.5


def test_adx_statistics_missing():
    assert adx_statistics(_response(kind="QueryProperties")) is None
    assert adx_statistics(_response()) is None


def test_adx_statistics_bad_payload_or_mock():
    bad = {"EventTypeName": "QueryResourceConsumption", "Payload": "{not json"}
    assert adx_statistics(_response(bad)) is None
    assert adx_statistics(mock.Mock()) is None
