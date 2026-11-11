#!/usr/bin/env python3
"""Exercise the retention reader through native startup, reload and restart."""

import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

from signoz_java_signal_clickhouse import GATE

LINUX_BUILD = platform.system() == "Linux" and platform.machine() == "x86_64"
REMOVABLE = ("metric_log", "trace_log")
MEASURED = ("part_log", "zookeeper_log", "aggregated_zookeeper_log", "processors_profile_log")


class RetentionClickHouseContract(unittest.TestCase):
    def setUp(self) -> None:
        path = shutil.which(os.environ.get("CLICKHOUSE_BINARY", "clickhouse"))
        self.assertIsNotNone(path, "Set CLICKHOUSE_BINARY to the pinned ClickHouse executable")
        self.binary = Path(path).resolve()
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="signoz-retention-clickhouse-"))
        self.root = Path(temp)
        self.data = self.root / "data"
        self.config = self.root / "config.xml"
        self.fragments = self.root / "config.d"
        self.fragments.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]

        self.env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}")
        (self.root / "clickhouse").symlink_to(self.binary)
        self.server_binary = self.root / "clickhouse-server"
        self.server_binary.symlink_to(self.binary)
        client = self.root / "clickhouse-client"
        client.write_text(
            f'#!/bin/sh\nexec "$(dirname "$0")/clickhouse" client --port {self.port} "$@"\n',
            encoding="utf-8",
        )
        client.chmod(0o700)
        writers = "".join(
            f"<{name}><ttl>event_date + INTERVAL {7 if name in REMOVABLE else 3} DAY</ttl></{name}>"
            for name in GATE.CLICKHOUSE_LOG_TABLES
        )
        self.config.write_text(f"""<clickhouse>
          <logger><level>warning</level><console>1</console></logger>
          <listen_host>127.0.0.1</listen_host><tcp_port>{self.port}</tcp_port>
          <path>{escape(str(self.data))}/</path><users_config>users.xml</users_config>
          <prepare_system_log_tables_on_startup>1</prepare_system_log_tables_on_startup>
          <max_server_memory_usage>805306368</max_server_memory_usage>
          <max_thread_pool_size>128</max_thread_pool_size>
          <background_pool_size>4</background_pool_size>
          <background_schedule_pool_size>8</background_schedule_pool_size>
          <background_buffer_flush_schedule_pool_size>2</background_buffer_flush_schedule_pool_size>
          <background_message_broker_schedule_pool_size>2</background_message_broker_schedule_pool_size>
          <background_distributed_schedule_pool_size>2</background_distributed_schedule_pool_size>
          <merge_tree>
            <number_of_free_entries_in_pool_to_execute_mutation>4</number_of_free_entries_in_pool_to_execute_mutation>
            <number_of_free_entries_in_pool_to_lower_max_size_of_merge>4</number_of_free_entries_in_pool_to_lower_max_size_of_merge>
            <number_of_free_entries_in_pool_to_execute_optimize_entire_partition>4</number_of_free_entries_in_pool_to_execute_optimize_entire_partition>
          </merge_tree>
          {writers}
        </clickhouse>
        """, encoding="utf-8")
        (self.root / "users.xml").write_text("""<clickhouse>
          <profiles><default><log_queries>0</log_queries><max_threads>2</max_threads></default></profiles>
          <users><default><password></password><networks><ip>127.0.0.1</ip></networks>
            <profile>default</profile><quota>default</quota></default></users>
          <quotas><default/></quotas>
        </clickhouse>
        """, encoding="utf-8")

    def sql(self, query: str) -> str:
        return subprocess.check_output(
            ["clickhouse-client", "--host", "127.0.0.1", "--query", query],
            env=self.env, stderr=subprocess.PIPE, text=True, timeout=15,
        ).strip()

    @contextmanager
    def server(self):
        log_path = self.root / "server.log"
        with log_path.open("w", encoding="utf-8") as output:
            server = subprocess.Popen(
                [self.server_binary, f"--config-file={self.config}"],
                stdout=output, stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(60):
                    if server.poll() is not None:
                        break
                    try:
                        self.assertEqual(self.sql("SELECT version()"), "25.12.5.44")
                        break
                    except subprocess.CalledProcessError:
                        time.sleep(0.15)
                else:
                    self.fail("ClickHouse startup timed out")
                if server.poll() is not None:
                    self.fail(log_path.read_text(encoding="utf-8")[-3500:])
                yield
            finally:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)

    def run_kubectl(self, command: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        if command[7:11] == ["-i", "--", "sh", "-s"]:
            args = ["sh", "-s", "--", self.data, self.config]
        else:
            self.assertEqual(command[7:9], ["--", "clickhouse-client"])
            args = command[8:]
        return subprocess.run(
            args, env=self.env, input=input_text, capture_output=True, text=True, check=True, timeout=15,
        )

    def readback(self) -> dict:
        runner = mock.Mock(spec=GATE.Runner)
        runner.run.side_effect = self.run_kubectl
        return GATE._read_clickhouse_retention(runner, "fixture", "fixture")

    def test_startup_removal_differs_from_mounted_config_reload(self) -> None:
        unknown = dict.fromkeys(REMOVABLE, "unverified")
        legacy = dict.fromkeys(REMOVABLE, 7)
        with self.server():
            for name in (*REMOVABLE, *MEASURED):
                self.sql(f"INSERT INTO system.{name} (event_date, event_time) VALUES (today(), now())")
            for name in ("metric_log_4", "zookeeper_log_7", "trace_log_foreign"):
                self.sql(f"CREATE TABLE system.{name} (event_date Date) ENGINE=MergeTree ORDER BY event_date")
                self.sql(f"INSERT INTO system.{name} VALUES (today())")

            active = self.readback()
            self.assertEqual(active["logWriters"], dict.fromkeys(REMOVABLE, "active") if LINUX_BUILD else unknown)
            self.assertEqual({name: active["deletionTtlDays"][name] for name in REMOVABLE}, legacy)
            for name in (*MEASURED, "zookeeper_log_7", "metric_log_4"):
                self.assertGreater(active["activeLogBytesByTable"][name], 0)
            self.assertNotIn("trace_log_foreign", active["activeLogBytesByTable"])

            (self.fragments / "99-remove.xml").write_text(
                '<clickhouse><metric_log remove="1"/><trace_log remove="1"/></clickhouse>', encoding="utf-8",
            )
            self.sql("SYSTEM RELOAD CONFIG")
            merged = ET.parse(self.data / "preprocessed_configs" / "config.xml").getroot()
            for name in REMOVABLE:
                self.assertIsNone(merged.find(name))
            reload = self.readback()
            self.assertEqual(reload["logWriters"], unknown)
            self.assertEqual(reload["retainedDisabledLogBytes"], 0)
            self.assertTrue(all(any(name in failure for failure in reload["failures"]) for name in REMOVABLE))

        with self.server():
            restarted = self.readback()
            self.assertEqual(restarted["logWriters"], dict.fromkeys(REMOVABLE, "disabled") if LINUX_BUILD else unknown)
            self.assertEqual({name: restarted["deletionTtlDays"][name] for name in REMOVABLE}, legacy)
            parts = restarted["activeLogBytesByTable"]
            retained = parts["metric_log"] + parts["metric_log_4"] + parts["trace_log"]
            self.assertGreater(retained, 0)
            self.assertEqual(restarted["retainedDisabledLogBytes"], retained if LINUX_BUILD else 0)
            self.assertEqual(restarted["trackedLogBytes"], sum(parts.values()))
            self.assertEqual(restarted["rotatedLogBytes"], parts["metric_log_4"] + parts["zookeeper_log_7"])
            if LINUX_BUILD:
                self.assertFalse(any(any(name in failure for name in REMOVABLE) for failure in restarted["failures"]))
                (self.fragments / "99-malformed.xml").write_text("<clickhouse><broken", encoding="utf-8")
                self.assertEqual(self.readback()["logWriters"], unknown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
