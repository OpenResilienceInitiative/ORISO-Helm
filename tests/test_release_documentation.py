"""Offline publication boundary tests against the actual locked Docs contract."""

import base64, copy, hashlib, importlib.util, json, os, pathlib, subprocess, sys, tempfile, types, unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = pathlib.Path(os.environ["ORISO_DOCS_SOURCE_ROOT"])
sys.path.insert(0, str(DOCS / "tools/understand-anything"))
from bundle import release_inputs as contract

spec = importlib.util.spec_from_file_location(
    "coordinator", ROOT / "scripts/release_documentation.py"
)
coordinator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinator)


def lock_fixture():
    sources = [
        dict(repository=name, ref="a" * 40, sourceSHA="a" * 40)
        for name in sorted(contract.required_repositories())
    ]
    for source in sources:
        if source["repository"] == "ORISO-Helm":
            source.update(ref="refs/tags/v2.0.9", sourceSHA="e" * 40)
        if source["repository"] == "ORISO-Docs":
            source.update(ref="d" * 40, sourceSHA="d" * 40)
    return dict(
        schemaVersion="oriso.platform-release/v1",
        version="v2.0.9",
        releaseUrl="https://github.com/OpenResilienceInitiative/ORISO-Helm/releases/tag/v2.0.9",
        documentationRevision="d" * 40,
        sources=sources,
    )


class GitHubFixture:
    def __init__(self, lock):
        self.lock = lock
        self.release = None
        self.calls = []
        self.wrong_commit = False

    def request(self, method, path, data=None, raw=None, upload=False, allow404=False):
        self.calls.append((method, path, data, raw))
        if path.endswith("/dispatches"):
            return None
        if "/commits/" in path:
            return dict(sha="0" * 40 if self.wrong_commit else path.rsplit("/", 1)[-1])
        if "/releases/tags/" in path:
            if self.release and not self.release["draft"]:
                return copy.deepcopy(self.release)
            if allow404:
                return None
            raise ValueError("published release lookup HTTP404")
        if method == "GET" and path.endswith("/releases/1"):
            return copy.deepcopy(self.release)
        if method == "GET" and "/releases?" in path:
            return [copy.deepcopy(self.release)] if self.release else []
        if method == "GET" and "/releases/" not in path:
            return dict(private=False, visibility="public")
        if method == "POST" and path.endswith("/releases"):
            self.release = dict(
                id=1,
                tag_name=self.lock["version"],
                draft=True,
                prerelease=False,
                html_url=self.lock["releaseUrl"],
                assets=[],
            )
            return copy.deepcopy(self.release)
        if upload:
            self.release["assets"].append(
                dict(
                    id=7,
                    name="platform-release.json",
                    state="uploaded",
                    size=len(raw),
                    digest="sha256:" + hashlib.sha256(raw).hexdigest(),
                )
            )
            return self.release["assets"][-1]
        if method == "PATCH":
            self.release.update(
                draft=False,
                published_at="2026-09-30T00:00:00Z",
                html_url=self.lock["releaseUrl"],
            )
            return copy.deepcopy(self.release)
        raise AssertionError((method, path))

    def get(self, path):
        return self.request("GET", path)


class CoordinatorTests(unittest.TestCase):
    def test_documentation_rebuilds_original_input_without_chart_artifact(self):
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/release-helm-chart.yml").read_text()
        )
        release = workflow["jobs"]["release"]["steps"]
        documentation = workflow["jobs"]["documentation"]["steps"]
        self.assertFalse(any(
            step.get("uses", "").startswith("actions/upload-artifact@")
            for step in release
        ), "no artifact upload may fail after the chart publishes")
        self.assertFalse(any(
            step.get("uses", "").startswith("actions/download-artifact@")
            for step in documentation
        ))
        prepares = [step for step in documentation if
                    "release_documentation.py prepare" in step.get("run", "")]
        self.assertEqual(len(prepares), 1)
        self.assertEqual(prepares[0]["id"], "release-input")
        fetch = next(step for step in documentation if
                     step.get("name") == "Fetch the same released Docs validator")
        self.assertEqual(fetch["env"]["DOCS_RELEASE_REVISION"],
                         "${{ steps.release-input.outputs.documentation-revision }}")

    def test_docs_revision_must_be_ancestor_of_pinned_main(self):
        self.assertTrue(hasattr(coordinator, "verify_trusted_revision"))
        calls = []

        def api(path):
            calls.append(path)
            if path.endswith("/branches/main"):
                return dict(commit=dict(sha="b" * 40))
            return dict(status="ahead", base_commit=dict(sha="d" * 40),
                        merge_base_commit=dict(sha="d" * 40))

        coordinator.verify_trusted_revision("d" * 40, api)
        self.assertEqual(calls, [
            "repos/OpenResilienceInitiative/ORISO-Docs/branches/main",
            "repos/OpenResilienceInitiative/ORISO-Docs/compare/" +
            "d" * 40 + "..." + "b" * 40,
        ])
        for status, merge_base in [("behind", "b" * 40),
                                   ("diverged", "a" * 40),
                                   ("ahead", "a" * 40)]:
            def untrusted(path):
                if path.endswith("/branches/main"):
                    return dict(commit=dict(sha="b" * 40))
                return dict(status=status, base_commit=dict(sha="d" * 40),
                            merge_base_commit=dict(sha=merge_base))

            with self.subTest(status=status, merge_base=merge_base):
                with self.assertRaisesRegex(ValueError, "Docs main"):
                    coordinator.verify_trusted_revision("d" * 40, untrusted)

    def test_exact_main_revision_is_trusted(self):
        self.assertTrue(hasattr(coordinator, "verify_trusted_revision"))

        def api(path):
            if path.endswith("/branches/main"):
                return dict(commit=dict(sha="d" * 40))
            return dict(status="identical", base_commit=dict(sha="d" * 40),
                        merge_base_commit=dict(sha="d" * 40))

        coordinator.verify_trusted_revision("d" * 40, api)

    def test_untrusted_revision_is_rejected_before_import_with_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "platform-release.json"
            manifest.write_text(json.dumps(self.lock))
            env = dict(os.environ, RELEASE_VERSION="2.0.9", GITHUB_SHA="e" * 40,
                       GITHUB_REF_NAME="main", GITHUB_TOKEN="synthetic-helm",
                       ORISO_DOCS_RELEASE_TOKEN="synthetic-docs")
            def api(path):
                if path.endswith("/branches/main"):
                    return dict(commit=dict(sha="b" * 40))
                return dict(status="diverged", base_commit=dict(sha="d" * 40),
                            merge_base_commit=dict(sha="a" * 40))

            with patch.dict(os.environ, env), patch.object(sys, "argv", [
                "coordinator", "verify", "--manifest", str(manifest),
                "--docs-root", str(DOCS),
            ]), patch.object(coordinator.GitHub, "get", side_effect=api), \
                    patch.object(coordinator, "load_contract") as load:
                with self.assertRaisesRegex(ValueError, "Docs main"):
                    coordinator.main()
                load.assert_not_called()

    def test_untrusted_revision_is_rejected_before_publish_and_dispatch_import(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "platform-release.json"
            manifest.write_text(json.dumps(self.lock))
            env = dict(os.environ, RELEASE_VERSION="2.0.9", GITHUB_SHA="e" * 40,
                       GITHUB_REF_NAME="main", GITHUB_TOKEN="synthetic-helm",
                       ORISO_DOCS_RELEASE_TOKEN="synthetic-docs")

            def api(path):
                if path.endswith("/branches/main"):
                    return dict(commit=dict(sha="b" * 40))
                return dict(status="diverged", base_commit=dict(sha="d" * 40),
                            merge_base_commit=dict(sha="a" * 40))

            with patch.dict(os.environ, env), patch.object(sys, "argv", [
                "coordinator", "publish-and-dispatch", "--manifest", str(manifest),
                "--docs-root", str(DOCS),
            ]), patch.object(coordinator.GitHub, "get", side_effect=api), \
                    patch.object(coordinator, "load_contract") as load:
                with self.assertRaisesRegex(ValueError, "Docs main"):
                    coordinator.main()
                load.assert_not_called()

    def test_delayed_asset_digest_retries_metadata_without_second_upload(self):
        original = self.api.request
        reads = []

        def delayed(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if kwargs.get("upload"):
                result = dict(result, digest="")
            elif method == "GET" and path.endswith("/releases/1"):
                reads.append(path)
                if len(reads) == 1:
                    result["assets"][0]["digest"] = None
            return result

        self.api.request = delayed
        with patch("time.sleep"):
            coordinator.publish(self.lock, contract, self.api, self.tag)
            coordinator.dispatch(self.lock, contract, self.api, self.api, self.tag)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len([call for call in self.api.calls if call[3] is not None]), 1)
        self.assertFalse(self.api.release["draft"])

    def test_missing_digest_times_out_and_retry_reuses_draft_asset(self):
        original = self.api.request

        def pending(method, path, **kwargs):
            result = original(method, path, **kwargs)
            if kwargs.get("upload"):
                self.api.release["assets"][0]["digest"] = None
                result = copy.deepcopy(self.api.release["assets"][0])
            return result

        self.api.request = pending
        with patch("time.sleep"):
            with self.assertRaisesRegex(ValueError, "digest.*unavailable"):
                coordinator.publish(self.lock, contract, self.api, self.tag)
        self.assertTrue(self.api.release["draft"])
        self.assertFalse(any(call[1].endswith("/dispatches") for call in self.api.calls))
        self.api.release["assets"][0]["digest"] = (
            "sha256:" + hashlib.sha256(contract.canonical_bytes(self.lock)).hexdigest()
        )
        self.api.request = original
        coordinator.publish(self.lock, contract, self.api, self.tag)
        self.assertEqual(len([call for call in self.api.calls if call[3] is not None]), 1)

    def test_mismatched_digest_is_rejected_without_polling_or_publication(self):
        original = self.api.request

        def mismatch(method, path, **kwargs):
            result = original(method, path, **kwargs)
            return dict(result, digest="sha256:" + "0" * 64) if kwargs.get("upload") else result

        self.api.request = mismatch
        with patch("time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                coordinator.publish(self.lock, contract, self.api, self.tag)
            sleep.assert_not_called()
        self.assertTrue(self.api.release["draft"])

    def test_conflicting_metadata_during_digest_polling_never_publishes(self):
        changes = ["id", "duplicate", "missing", "state", "size", "digest"]
        for change in changes:
            with self.subTest(change=change):
                api = GitHubFixture(self.lock)
                original = api.request

                def changed(method, path, **kwargs):
                    result = original(method, path, **kwargs)
                    if kwargs.get("upload"):
                        return dict(result, digest=None)
                    if method == "GET" and path.endswith("/releases/1"):
                        asset = result["assets"][0]
                        if change == "id":
                            asset["id"] = 8
                        elif change == "duplicate":
                            result["assets"].append(dict(asset, id=8))
                        elif change == "missing":
                            result["assets"] = []
                        elif change == "state":
                            asset["state"] = "starter"
                        elif change == "size":
                            asset["size"] += 1
                        elif change == "digest":
                            asset["digest"] = "sha256:" + "0" * 64
                    return result

                api.request = changed
                with patch("time.sleep"):
                    with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                        coordinator.publish(self.lock, contract, api, self.tag)
                self.assertTrue(api.release["draft"])
                self.assertEqual(len([call for call in api.calls if call[3] is not None]), 1)
                self.assertFalse(any(call[0] == "PATCH" or call[1].endswith("/dispatches")
                                     for call in api.calls))

    def setUp(self):
        self.lock = lock_fixture()
        self.api = GitHubFixture(self.lock)
        self.tag = lambda repo, ref: next(
            s["sourceSHA"] for s in self.lock["sources"] if s["repository"] == repo
        )

    def event(self):
        return dict(
            inputs=dict(version="2.0.9", release_manifest=json.dumps(self.lock))
        )

    def test_release_is_bound_to_main_version_and_full_helm_revision(self):
        self.assertEqual(
            coordinator.bootstrap(self.event(), "e" * 40, "main"), self.lock
        )
        for sha, ref in [("f" * 40, "main"), ("e" * 40, "dev")]:
            with self.assertRaises(ValueError):
                coordinator.bootstrap(self.event(), sha, ref)
        event = self.event()
        event["inputs"]["version"] = "2.0.8"
        with self.assertRaises(ValueError):
            coordinator.bootstrap(event, "e" * 40, "main")
        for value in [False, "false"]:
            event = self.event()
            event["inputs"]["create_tag"] = value
            with self.assertRaisesRegex(ValueError, "create_tag=true"):
                coordinator.bootstrap(event, "e" * 40, "main")

    def test_manifest_cannot_be_missing_or_duplicate_json(self):
        with self.assertRaises(ValueError):
            coordinator.bootstrap(dict(inputs=dict(version="2.0.9")), "e" * 40, "main")
        event = self.event()
        event["inputs"]["release_manifest"] = '{"version":"v2.0.9","version":"v2.0.8"}'
        with self.assertRaises(ValueError):
            coordinator.bootstrap(event, "e" * 40, "main")

    def test_all_public_sources_exist_at_exact_commits_before_release_writes(self):
        coordinator.verify_sources(self.lock, contract, self.api.get, self.tag)
        self.assertEqual(len([c for c in self.api.calls if "/commits/" in c[1]]), 16)
        self.assertFalse(any(c[0] != "GET" for c in self.api.calls))
        self.api.wrong_commit = True
        with self.assertRaises(ValueError):
            coordinator.verify_sources(self.lock, contract, self.api.get, self.tag)

    def test_branch_tip_missing_source_and_retargeted_tag_fail_before_writes(self):
        for change in ["branch", "missing", "tag"]:
            lock = copy.deepcopy(self.lock)
            if change == "branch":
                lock["sources"][0]["ref"] = "refs/heads/dev"
            if change == "missing":
                lock["sources"].pop(0)
            if change == "tag":
                lock["sources"][0]["ref"] = "refs/tags/v2.0.9"
            with self.assertRaises(ValueError):
                coordinator.verify_sources(
                    lock, contract, self.api.get, lambda repo, ref: "0" * 40
                )
        self.assertFalse(any(c[0] != "GET" for c in self.api.calls))

    def test_asset_is_canonical_and_release_published_before_dispatch(self):
        coordinator.publish(self.lock, contract, self.api, self.tag)
        coordinator.dispatch(self.lock, contract, self.api, self.api, self.tag)
        upload = next(c for c in self.api.calls if c[3] is not None)
        self.assertEqual(upload[3], contract.canonical_bytes(self.lock))
        event = next(c[2] for c in self.api.calls if c[1].endswith("/dispatches"))
        self.assertEqual(
            event,
            dict(
                event_type="platform-release-published",
                client_payload=dict(release_manifest=self.lock),
            ),
        )
        patch = next(i for i, c in enumerate(self.api.calls) if c[0] == "PATCH")
        send = next(
            i for i, c in enumerate(self.api.calls) if c[1].endswith("/dispatches")
        )
        self.assertLess(patch, send)

    def test_retry_reuses_identical_asset_without_overwrite_or_republish(self):
        coordinator.publish(self.lock, contract, self.api, self.tag)
        self.api.calls = []
        coordinator.publish(self.lock, contract, self.api, self.tag)
        coordinator.dispatch(self.lock, contract, self.api, self.api, self.tag)
        self.assertEqual(
            [c[1] for c in self.api.calls if c[0] != "GET"],
            ["repos/OpenResilienceInitiative/ORISO-Docs/dispatches"],
        )

    def test_draft_temporary_url_is_checked_as_final_identity_after_publication(self):
        self.api.release = dict(
            id=1,
            tag_name=self.lock["version"],
            draft=True,
            prerelease=False,
            html_url="https://github.com/OpenResilienceInitiative/ORISO-Helm/releases/tag/untagged-fixture",
            assets=[],
        )
        coordinator.publish(self.lock, contract, self.api, self.tag)
        self.assertEqual(self.api.release["html_url"], self.lock["releaseUrl"])

    def test_interrupted_draft_upload_or_publication_retries_same_release(self):
        for failed_stage in ["upload", "publish"]:
            with self.subTest(failed_stage=failed_stage):
                api = GitHubFixture(self.lock)
                original = api.request

                def interrupted(method, path, **kwargs):
                    if (failed_stage == "upload" and kwargs.get("upload")) or (
                        failed_stage == "publish" and method == "PATCH"
                    ):
                        raise ValueError("interrupted request")
                    return original(method, path, **kwargs)

                api.request = interrupted
                with self.assertRaisesRegex(ValueError, "interrupted"):
                    coordinator.publish(self.lock, contract, api, self.tag)
                self.assertTrue(api.release["draft"])
                self.assertIsNone(
                    original(
                        "GET",
                        "repos/OpenResilienceInitiative/ORISO-Helm/releases/tags/v2.0.9",
                        allow404=True,
                    )
                )
                api.request = original
                coordinator.publish(self.lock, contract, api, self.tag)
                coordinator.dispatch(self.lock, contract, api, api, self.tag)
                self.assertEqual(
                    len(
                        [
                            c
                            for c in api.calls
                            if c[0] == "POST" and c[1].endswith("/releases")
                        ]
                    ),
                    1,
                )
                self.assertEqual(len([c for c in api.calls if c[3] is not None]), 1)

    def test_duplicate_drafts_are_refused_without_writes(self):
        release = dict(id=1, tag_name=self.lock["version"], draft=True)
        original = self.api.request

        def duplicates(method, path, **kwargs):
            if "/releases?" in path:
                return [release, dict(release, id=2)]
            return original(method, path, **kwargs)

        self.api.request = duplicates
        with self.assertRaisesRegex(ValueError, "multiple releases"):
            coordinator.publish(self.lock, contract, self.api, self.tag)
        self.assertFalse(any(c[0] != "GET" for c in self.api.calls))

    def test_draft_lookup_checks_later_pages(self):
        calls = []
        release = dict(id=42, tag_name=self.lock["version"], draft=True)

        def request(method, path, **kwargs):
            calls.append(path)
            return (
                [dict(id=i, tag_name="other") for i in range(100)]
                if path.endswith("page=1")
                else [release]
            )

        client = types.SimpleNamespace(request=request)
        self.assertEqual(
            coordinator.find_release(
                client,
                "repos/OpenResilienceInitiative/ORISO-Helm",
                self.lock["version"],
            ),
            release,
        )
        self.assertEqual(len(calls), 2)

    def test_modified_asset_is_never_overwritten_or_dispatched(self):
        coordinator.publish(self.lock, contract, self.api, self.tag)
        self.api.release["assets"][0]["digest"] = "sha256:" + "0" * 64
        self.api.calls = []
        with self.assertRaises(ValueError):
            coordinator.publish(self.lock, contract, self.api, self.tag)
        with self.assertRaises(ValueError):
            coordinator.dispatch(self.lock, contract, self.api, self.api, self.tag)
        self.assertFalse(any(c[0] != "GET" for c in self.api.calls))

    def test_draft_or_unpublished_release_cannot_dispatch(self):
        coordinator.publish(self.lock, contract, self.api, self.tag)
        self.api.release["draft"] = True
        self.api.calls = []
        with self.assertRaises(ValueError):
            coordinator.dispatch(self.lock, contract, self.api, self.api, self.tag)
        self.assertFalse(any(c[1].endswith("/dispatches") for c in self.api.calls))

    def test_missing_dispatch_binding_fails_before_prepare(self):
        with self.assertRaises(ValueError):
            coordinator.require_dispatch_token("")

    def test_native_prepare_persists_exact_canonical_lock_and_safe_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            event = root / "event.json"
            output = root / "output"
            manifest = root / "platform-release.json"
            event.write_text(json.dumps(self.event()))
            env = dict(
                os.environ,
                GITHUB_EVENT_PATH=str(event),
                GITHUB_OUTPUT=str(output),
                GITHUB_SHA="e" * 40,
                GITHUB_REF_NAME="main",
                ORISO_DOCS_RELEASE_TOKEN="synthetic-unused",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/release_documentation.py"),
                    "prepare",
                    "--manifest",
                    str(manifest),
                ],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(manifest.read_bytes(), contract.canonical_bytes(self.lock))
            self.assertEqual(
                output.read_text(), "documentation-revision=" + "d" * 40 + "\n"
            )
            self.assertNotIn("synthetic-unused", result.stdout + result.stderr)

    def test_actual_clean_pinned_docs_contract_loads(self):
        revision = subprocess.check_output(
            ["git", "-C", str(DOCS), "rev-parse", "HEAD"], text=True
        ).strip()
        self.assertIs(coordinator.load_contract(DOCS, revision), contract)

    def test_default_branch_receiver_must_accept_release_event(self):
        paths = []

        def api(path):
            paths.append(path)
            if "/actions/" in path:
                return dict(state="active")
            if "/contents/" not in path:
                return dict(default_branch="main")
            return dict(
                encoding="base64",
                content=base64.b64encode(
                    b"on:\n  repository_dispatch:\n    types: [platform-release-published]\n"
                ).decode(),
            )

        coordinator.verify_receiver(api)
        self.assertEqual(len(paths), 7)
        self.assertTrue(all("?ref=main" in p for p in paths if "/contents/" in p))

        def wrong(path):
            if "/actions/" in path:
                return dict(state="active")
            if "/contents/" not in path:
                return dict(default_branch="main")
            return dict(
                encoding="base64",
                content=base64.b64encode(
                    b"on:\n  push:\n    branches: [dev]\n"
                ).decode(),
            )

        with self.assertRaises(ValueError):
            coordinator.verify_receiver(wrong)

        def disabled(path):
            return dict(state="disabled_manually") if "/actions/" in path else api(path)

        with self.assertRaisesRegex(ValueError, "not active"):
            coordinator.verify_receiver(disabled)

    def test_validator_requires_exact_clean_checkout_and_receiver_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "tools/understand-anything/bundle/release_inputs.py"
            source.parent.mkdir(parents=True)
            source.write_text("legacy validator")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            sha = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            with self.assertRaisesRegex(ValueError, "checkout differs"):
                coordinator.load_contract(root, "0" * 40)
            module = types.SimpleNamespace(__file__=str(source))
            with patch.object(
                coordinator.importlib, "import_module", return_value=module
            ), self.assertRaisesRegex(ValueError, "lacks Helm"):
                coordinator.load_contract(root, sha)
            source.write_text("modified")
            with self.assertRaisesRegex(ValueError, "is modified"):
                coordinator.load_contract(root, sha)

    def test_changed_sources_fail_before_github_release_creation(self):
        self.api.wrong_commit = True
        with self.assertRaises(ValueError):
            coordinator.publish(self.lock, contract, self.api, self.tag)
        self.assertFalse(any(c[0] != "GET" for c in self.api.calls))

    def test_workflow_documentation_waits_for_successful_chart_job_and_has_no_schedule(
        self,
    ):
        import yaml

        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/release-helm-chart.yml").read_text()
        )
        self.assertEqual(workflow["jobs"]["documentation"]["needs"], "release")
        self.assertNotIn("schedule", workflow.get("on", workflow.get(True, {})))
        release = workflow["jobs"]["release"]["steps"]
        names = [s.get("name", "") for s in release]
        self.assertLess(
            names.index("Verify exact public release sources"),
            names.index("Create and push release tag"),
        )
        documentation = workflow["jobs"]["documentation"]["steps"]
        self.assertTrue(any(
            "release_documentation.py prepare" in s.get("run", "")
            for s in documentation
        ))
        self.assertFalse(
            any(
                "helm push" in s.get("run", "") or "git tag" in s.get("run", "")
                for s in documentation
            )
        )


if __name__ == "__main__":
    unittest.main()
