"""metrics.disable() restores the query logger; the Prometheus output parses."""

import re

import pytest
from sqler.db.sqler_db import SQLerDB
from sqler.logging import query_logger
from sqler.metrics import MetricsCollector


@pytest.fixture(autouse=True)
def _restore_logger():
    saved = (query_logger._enabled, query_logger._log_to_logger, query_logger._log_level)
    yield
    query_logger._enabled, query_logger._log_to_logger, query_logger._log_level = saved
    query_logger.clear()


# --- logger state -----------------------------------------------------------


def test_disable_turns_the_logger_off_when_enable_turned_it_on():
    query_logger.disable()
    collector = MetricsCollector()
    collector.enable()
    assert query_logger.enabled
    collector.disable()
    assert not query_logger.enabled


def test_disable_leaves_the_logger_on_when_the_caller_had_it_on():
    query_logger.enable(log_to_logger=True)
    collector = MetricsCollector()
    collector.enable()
    collector.disable()
    assert query_logger.enabled
    assert query_logger._log_to_logger is True


def test_enable_twice_then_disable_still_restores():
    query_logger.disable()
    collector = MetricsCollector()
    collector.enable()
    collector.enable()
    collector.disable()
    assert not query_logger.enabled


# --- Prometheus text format, parsed from the format's rules -----------------

_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _parse_sample(line):
    """name{l="v",...} value  ->  (name, {l: v}, value); raises on any rule broken."""
    m = _NAME.match(line)
    assert m, f"bad metric name: {line!r}"
    name, i = m.group(0), m.end()
    labels = {}
    if i < len(line) and line[i] == "{":
        i += 1
        while True:
            if line[i] == "}":
                i += 1
                break
            m = _LABEL_NAME.match(line, i)
            assert m, f"bad label name at {i}: {line!r}"
            lname, i = m.group(0), m.end()
            assert line[i : i + 2] == '="', f'label needs =" at {i}: {line!r}'
            i += 2
            value = []
            while True:
                assert i < len(line), f"unterminated label value: {line!r}"
                c = line[i]
                if c == "\\":
                    nxt = line[i + 1 : i + 2]
                    assert nxt in ("\\", '"', "n"), f"bad escape \\{nxt}: {line!r}"
                    value.append({"\\": "\\", '"': '"', "n": "\n"}[nxt])
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    value.append(c)
                    i += 1
            assert lname not in labels, f"duplicate label {lname}: {line!r}"
            labels[lname] = "".join(value)
            if line[i] == ",":
                i += 1
            else:
                assert line[i] == "}", f"expected , or }} at {i}: {line!r}"
    assert line[i] == " ", f"space before value: {line!r}"
    value = line[i + 1 :]
    assert value in ("+Inf", "-Inf", "NaN") or float(value) is not None
    return name, labels, float(value)


def _parse_exposition(text):
    assert text.endswith("\n")
    types = {}
    samples = []
    for line in text[:-1].split("\n"):
        assert line != "", "blank line"
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            assert name not in types, f"TYPE for {name} twice"
            assert kind in ("counter", "gauge", "histogram", "summary", "untyped")
            types[name] = kind
        elif line.startswith("# HELP "):
            assert len(line.split(" ", 3)) == 4
        elif line.startswith("#"):
            continue
        else:
            name, labels, value = _parse_sample(line)
            family = name
            for suffix in ("_bucket", "_sum", "_count"):
                if name.endswith(suffix) and name[: -len(suffix)] in types:
                    family = name[: -len(suffix)]
            assert family in types, f"sample {name} has no TYPE declaration"
            if types[family] == "histogram":
                assert name != family, "histogram sample needs _bucket/_sum/_count"
            if name.endswith("_bucket") and types[family] == "histogram":
                assert "le" in labels
            samples.append((name, labels, value, family))
    return types, samples


HOSTILE = 'a"b\\c\nd'


def test_prometheus_output_parses_with_hostile_label_value():
    collector = MetricsCollector()
    collector.enable(labels={"env": HOSTILE})
    try:
        db = SQLerDB.in_memory(shared=False)
        db.adapter.execute('CREATE TABLE "a\\b" (x INTEGER);')
        db.adapter.execute('INSERT INTO "a\\b" VALUES (1);')
        db.adapter.execute('SELECT * FROM "a\\b" ;')
        text = collector.prometheus_export()
    finally:
        collector.disable()
    types, samples = _parse_exposition(text)
    assert types["sqler_query_duration_ms"] == "histogram"
    assert "sqler_query_duration_ms_bucket" not in types
    by_name = {}
    for name, labels, value, family in samples:
        by_name.setdefault(name, []).append((labels, value))
        if "env" in labels:
            assert labels["env"] == HOSTILE
    assert len(by_name["sqler_query_duration_ms_bucket"]) == 10
    assert by_name["sqler_query_duration_ms_count"][0][0] == {"env": HOSTILE}
    tables = {lab["table"] for lab, _ in by_name["sqler_table_operations_total"]}
    assert "a\\b" in tables


def test_prometheus_output_without_labels_parses():
    collector = MetricsCollector()
    types, samples = _parse_exposition(collector.prometheus_export())
    assert types["sqler_query_duration_ms"] == "histogram"
    assert {s[0] for s in samples} >= {
        "sqler_query_duration_ms_sum",
        "sqler_query_duration_ms_count",
    }


def test_label_names_are_made_valid():
    collector = MetricsCollector()
    collector.enable(labels={"my-label": "x"})
    try:
        _parse_exposition(collector.prometheus_export())
    finally:
        collector.disable()


@pytest.mark.parametrize(
    "labels",
    [
        {"le": "x"},
        {"table": "x"},
        {"operation": "x"},
        {"a-b": "1", "a_b": "2"},
        {"a.b": "1", "a b": "2"},
    ],
)
def test_a_label_name_that_would_repeat_on_a_sample_is_refused(labels):
    query_logger.disable()
    collector = MetricsCollector()
    with pytest.raises(ValueError, match="label"):
        collector.enable(labels=labels)
    # nothing was switched on by the refused call
    assert not query_logger.enabled
    assert not collector._enabled


def test_a_valid_custom_label_still_parses_beside_the_fixed_ones():
    collector = MetricsCollector()
    collector.enable(labels={"env": "prod"})
    try:
        _parse_exposition(collector.prometheus_export())
    finally:
        collector.disable()
