"""Lifecycle tests for the isolated Synapse recovery fixture."""

import subprocess
import unittest
from unittest.mock import patch

from scripts import rehearse_synapse_restore as rehearsal


class FixtureNetworkTest(unittest.TestCase):
    def test_failed_removal_makes_the_rehearsal_fail(self):
        with patch.object(rehearsal.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0, stdout="true\n"),
            subprocess.CompletedProcess([], 1),
        ]) as docker:
            with self.assertRaisesRegex(RuntimeError, "Remove fixture network"):
                with rehearsal.internal_network(["docker"]) as name:
                    self.assertTrue(name.startswith("oriso-synapse-restore-"))

        self.assertEqual(docker.call_args.args[0], ["docker", "network", "rm", name])

    def test_cleanup_does_not_hide_a_failed_creation(self):
        with patch.object(rehearsal.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 2),
            subprocess.CompletedProcess([], 1),
        ]) as docker:
            with self.assertRaisesRegex(RuntimeError, "Create internal fixture network failed"):
                with rehearsal.internal_network(["docker"]):
                    self.fail("The fixture never entered its network context")

        self.assertEqual(docker.call_args.args[0][1:3], ["network", "rm"])
