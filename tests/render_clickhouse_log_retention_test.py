#!/usr/bin/env python3
"""Render the internal log retention policy on the real ClickHouseInstallation."""

import re
import xml.etree.ElementTree as ET

from render_clickhouse_system_logs_test import render

RETENTION_FILE = "config.d/98-oriso-system-log-retention.xml"
AGGREGATED_LOG = "aggregated_zookeeper_log"
LOG_TABLES = {
    "asynchronous_metric_log",
    "metric_log",
    "part_log",
    "processors_profile_log",
    "query_log",
    "trace_log",
    "zookeeper_log",
}


def configuration(documents):
    installation, = (doc for doc in documents if doc["kind"] == "ClickHouseInstallation")
    return installation["spec"]["configuration"]["files"]


def check_retention(documents):
    files = configuration(documents)
    assert RETENTION_FILE in files, "ClickHouseInstallation is missing the retention file"
    root = ET.fromstring(files[RETENTION_FILE])
    assert root.tag == "clickhouse" and not root.attrib
    expected_logs = LOG_TABLES | {AGGREGATED_LOG}
    assert sorted(log.tag for log in root) == sorted(expected_logs), "Expected the full measured log set"

    operator_config, = (
        doc["data"]
        for doc in documents
        if doc["kind"] == "ConfigMap"
        and doc["metadata"]["name"] == "caritas-clickhouse-operator-etc-configd-files"
    )
    for name in sorted(LOG_TABLES):
        log = root.find(name)
        engine, = log
        assert not log.attrib and engine.tag == "engine" and engine.attrib == {"replace": "1"}, log.tag
        upstream, = (xml for name, xml in operator_config.items() if name.endswith(f"-{log.tag}.xml"))
        upstream_engine = ET.fromstring(upstream).findtext(f"{log.tag}/engine")
        assert upstream_engine, log.tag
        expected, replacements = re.subn(
            r" TTL event_date \+ interval \d+ day$",
            " TTL event_date + interval 3 day",
            upstream_engine,
        )
        assert replacements == 1 and engine.text == expected, log.tag

    aggregate = root.find(AGGREGATED_LOG)
    ttl, = aggregate
    assert not aggregate.attrib and ttl.tag == "ttl" and ttl.attrib == {"replace": "1"}
    assert ttl.text == "event_date + INTERVAL 3 DAY"

    system_logs = ET.fromstring(files["config.d/system_log.xml"])
    assert system_logs.findtext("text_log/ttl") == "event_date + INTERVAL 3 DAY DELETE"


def main():
    check_retention(render("values-dev.yaml"))
    check_retention(render("values-dev.yaml", "values-pre-dev.yaml"))
    assert RETENTION_FILE not in configuration(render()), "The stock chart must keep its retention defaults"
    print("Dev and layered Pre-Dev ClickHouse log retention ok")


if __name__ == "__main__":
    main()
