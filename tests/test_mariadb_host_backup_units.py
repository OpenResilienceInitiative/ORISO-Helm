"""Keep the host MariaDB backup units aligned with the script and the rendered StatefulSet."""

import configparser
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

from render_signoz_dev_observability_test import by_key, render


ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "runbooks" / "systemd"
SCRIPT = ROOT / "scripts" / "backup_mariadb.py"
INSTALLED_SCRIPT = "/opt/oriso/mariadb-backup/backup_mariadb.py"


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(UNITS / name)
    return parser


class MariaDbHostBackupUnitsTest(unittest.TestCase):
    def test_script_pod_matches_the_rendered_dev_statefulset(self):
        # The legacy host job pinned "oriso-mariadb-0" and failed every night
        # once the chart named the StatefulSet "mariadb".
        pod = re.search(r'^POD = "pod/([^"]+)"$', SCRIPT.read_text(), re.M)[1]
        statefulset = by_key(render("values-dev.yaml"))[("StatefulSet", "mariadb")]
        self.assertEqual(pod, statefulset["metadata"]["name"] + "-0")

    def test_service_checks_the_target_before_the_backup(self):
        service = unit("oriso-mariadb-backup.service")["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(service["UMask"], "0077")
        pre, run = shlex.split(service["ExecStartPre"]), shlex.split(service["ExecStart"])
        for command in (pre, run):
            self.assertEqual(command[:3], ["/usr/bin/python3", "-I", INSTALLED_SCRIPT])
            self.assertEqual(command[3:6], ["/etc/rancher/k3s/k3s.yaml", "default", "caritas"])
        self.assertEqual(pre[6:], ["--check-target"])
        self.assertEqual(len(run), 7)
        self.assertTrue(run[6].startswith("/root/db-backups/"))

    def test_timer_runs_nightly_and_catches_up_after_downtime(self):
        timer = unit("oriso-mariadb-backup.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*-*-* 02:00:00 UTC")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")



class MariaDbScheduleCutoverTest(unittest.TestCase):
    def test_switch_preserves_unrelated_entries_and_restores_cron_on_failure(self):
        runbook = (ROOT / "runbooks" / "mariadb-host-backup.md").read_text()
        section = runbook.split("### Switch schedules\n", 1)[1]
        snippet = re.search(r"```bash\n(.*?)```", section, re.S)[1]
        snapshot_template = "/root/db-backups/mariadb-scheduler-cutover.XXXXXX"
        self.assertIn(snapshot_template, snippet)
        snippet = snippet.replace(snapshot_template, '"$FIXTURE/mariadb-scheduler-cutover.XXXXXX"')
        fake_commands = '''
crontab() {
  if [[ "$1" == -l ]]; then
    cat "$FIXTURE/cron"
  else
    cat "$1" > "$FIXTURE/cron"
    [[ "$SCENARIO" != cron-install-failed || "$1" != */after ]]
  fi
}
systemctl() {
  case "$1" in
    show)
      case "$4" in
        ActiveState) printf '%s\\n' "$INITIAL_TIMER" ;;
        UnitFileState) printf 'disabled\\n' ;;
        *) return 70 ;;
      esac ;;
    enable)
      [[ "$*" == 'enable --now oriso-mariadb-backup.timer' ]] || return 70
      printf 'enabled' > "$FIXTURE/timer"
      [[ "$SCENARIO" != enable-failed ]] ;;
    disable)
      [[ "$*" == 'disable --now oriso-mariadb-backup.timer' ]] || return 70
      printf 'disabled' > "$FIXTURE/timer" ;;
    is-active) [[ "$SCENARIO" != inactive-after-enable && $(cat "$FIXTURE/timer") == enabled ]] ;;
    list-timers) [[ $(cat "$FIXTURE/timer") == enabled ]] ;;
    *) return 70 ;;
  esac
}
'''
        legacy = "0 2 * * * /root/db-backups/backup-mariadb.sh >> /tmp/synthetic.log 2>&1\n"
        other = "# see /root/db-backups/backup-mariadb.sh\n30 3 * * * /usr/bin/true\n"
        scenarios = (
            ("only-entry", legacy, "inactive", 0, "", "enabled"),
            ("other-entries", other + legacy, "inactive", 0, other, "enabled"),
            ("enable-failed", other + legacy, "inactive", 1, other + legacy, "disabled"),
            ("inactive-after-enable", legacy, "inactive", 1, legacy, "disabled"),
            ("cron-install-failed", legacy, "inactive", 1, legacy, "disabled"),
            ("ambiguous", legacy + legacy, "inactive", 1, legacy + legacy, "disabled"),
            ("missing-entry", other, "inactive", 1, other, "disabled"),
            ("already-active", legacy, "active", 1, legacy, "disabled"),
        )
        for scenario, original, initial, code, expected, timer in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory)
                (fixture / "cron").write_text(original)
                (fixture / "timer").write_text("disabled")
                result = subprocess.run(
                    ["bash", "-c", fake_commands + snippet],
                    env={**os.environ, "FIXTURE": directory, "SCENARIO": scenario, "INITIAL_TIMER": initial},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual((fixture / "cron").read_text(), expected)
                self.assertEqual((fixture / "timer").read_text(), timer)
                snapshots = list(fixture.glob("mariadb-scheduler-cutover.*/before"))
                if initial == "active":
                    self.assertEqual(snapshots, [])
                else:
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(snapshots[0].read_text(), original)
                    self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)

if __name__ == "__main__":
    unittest.main()
