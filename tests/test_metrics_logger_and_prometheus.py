"""metrics.disable() restores the query logger; capture() gives the statements of its block;
the Prometheus output parses."""

import re
from contextlib import ExitStack

import pytest
from sqler.db.sqler_db import SQLerDB
from sqler.logging import QueryLogger, query_logger
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


def test_a_collector_enabled_inside_a_capture_block_still_counts_after_it():
    """Inside capture() queries are logged, but nobody called enable(): the collector does."""
    query_logger.disable()
    collector = MetricsCollector()
    db = SQLerDB.in_memory(shared=False)
    try:
        with query_logger.capture():
            collector.enable()
        db.adapter.execute("SELECT 1")
        assert collector.get_metrics()["queries"]["total_queries"] == 1
        collector.disable()
        assert not query_logger.enabled
    finally:
        collector.disable()
        db.close()


# --- capture() gives the statements of its block -----------------------------


def _fill(logger, count):
    """Put ``count`` entries named old-0, old-1, ... into the log."""
    logger.enable(log_to_logger=False)
    for n in range(count):
        logger.log(f"old-{n}", [], 0.1)
    logger.disable()


@pytest.mark.parametrize("already", [0, 995, 1000])
def test_capture_gives_every_statement_of_its_block_however_full_the_log_is(already):
    logger = QueryLogger()  # keeps 1000 entries
    _fill(logger, already)
    with logger.capture() as sent:
        for n in range(10):
            logger.log(f"new-{n}", [n], 0.1)
    assert [entry.sql for entry in sent] == [f"new-{n}" for n in range(10)]
    assert [entry.params for entry in sent] == [[n] for n in range(10)]


def test_capture_on_the_full_global_log_gives_the_statement_a_database_sent():
    _fill(query_logger, 1000)
    db = SQLerDB.in_memory(shared=False)
    try:
        with query_logger.capture() as sent:
            db.adapter.execute("SELECT 41 + 1")
    finally:
        db.close()
    assert [entry.sql for entry in sent] == ["SELECT 41 + 1"]


def test_capture_on_a_logger_that_keeps_no_entries_still_gives_its_block():
    logger = QueryLogger(max_logs=0)
    with logger.capture() as sent:
        logger.log("a", [], 0.1)
        logger.log("b", [], 0.1)
    assert [entry.sql for entry in sent] == ["a", "b"]
    assert logger.logs == []


def test_a_capture_inside_a_capture_gives_each_block_its_own_statements():
    logger = QueryLogger()
    with logger.capture() as outer:
        logger.log("a", [], 0.1)
        with logger.capture() as inner:
            logger.log("b", [], 0.1)
        logger.log("c", [], 0.1)
    assert [entry.sql for entry in outer] == ["a", "b", "c"]
    assert [entry.sql for entry in inner] == ["b"]


def test_a_capture_that_ends_first_does_not_end_one_that_is_still_open():
    """Two blocks that overlap without nesting, as two threads' blocks do."""
    logger = QueryLogger()
    first, second = ExitStack(), ExitStack()
    a = first.enter_context(logger.capture())
    b = second.enter_context(logger.capture())
    logger.log("both", [], 0.1)
    first.close()
    logger.log("second only", [], 0.1)
    second.close()
    assert [entry.sql for entry in a] == ["both"]
    assert [entry.sql for entry in b] == ["both", "second only"]
    assert not logger.enabled


def test_disable_inside_a_capture_does_not_end_the_capture():
    logger = QueryLogger()
    with logger.capture() as sent:
        logger.disable()
        assert logger.enabled  # the adapters ask this before they time a statement
        logger.log("a", [], 0.1)
    assert [entry.sql for entry in sent] == ["a"]
    assert not logger.enabled


def test_enable_inside_a_capture_stays_on_after_it():
    logger = QueryLogger()
    with logger.capture() as sent:
        logger.enable(log_to_logger=False)
        logger.log("a", [], 0.1)
    logger.log("after", [], 0.1)
    assert [entry.sql for entry in sent] == ["a"]
    assert logger.enabled
    assert [entry.sql for entry in logger.logs] == ["a", "after"]


def test_a_statement_after_the_block_is_not_captured_and_the_logger_is_off_again():
    logger = QueryLogger()
    with logger.capture() as sent:
        logger.log("inside", [], 0.1)
    logger.log("after", [], 0.1)
    assert [entry.sql for entry in sent] == ["inside"]
    assert not logger.enabled
    assert [entry.sql for entry in logger.logs] == ["inside"]


def test_a_block_that_raises_ends_its_capture():
    logger = QueryLogger()
    with pytest.raises(RuntimeError, match="stop"):
        with logger.capture() as sent:
            logger.log("inside", [], 0.1)
            raise RuntimeError("stop")
    logger.log("after", [], 0.1)
    assert [entry.sql for entry in sent] == ["inside"]
    assert not logger.enabled


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
