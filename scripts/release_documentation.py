#!/usr/bin/env python3
"""Coordinate exact-source documentation after a successful Helm chart release."""
import argparse, base64, hashlib, importlib, json, os, pathlib, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

OWNER = "OpenResilienceInitiative"
HELM = "ORISO-Helm"
DOCS = "ORISO-Docs"
ASSET = "platform-release.json"
SHA = re.compile(r"[a-f0-9]{40}")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate release JSON field")
        result[key] = value
    return result


def parse_json(text):
    try:
        return json.loads(text, object_pairs_hook=unique_fields)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("release manifest JSON required") from error


def bootstrap(event, helm_sha, ref):
    inputs = event.get("inputs", {})
    version = inputs.get("version")
    require(
        inputs.get("create_tag", True) in [True, "true"],
        "documentation-coupled releases require create_tag=true",
    )
    require(
        isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version),
        "exact release version required",
    )
    require(
        ref == "main" and isinstance(helm_sha, str) and SHA.fullmatch(helm_sha),
        "release must use exact main revision",
    )
    lock = parse_json(inputs.get("release_manifest"))
    require(
        isinstance(lock, dict)
        and set(lock)
        == {
            "schemaVersion",
            "version",
            "releaseUrl",
            "documentationRevision",
            "sources",
        },
        "complete release manifest required",
    )
    require(
        lock["schemaVersion"] == "oriso.platform-release/v1"
        and lock["version"] == "v" + version,
        "manifest version differs from Helm release",
    )
    require(
        lock["releaseUrl"]
        == f"https://github.com/{OWNER}/{HELM}/releases/tag/v{version}",
        "Helm release URL required",
    )
    require(
        isinstance(lock["documentationRevision"], str)
        and SHA.fullmatch(lock["documentationRevision"]),
        "full Docs revision required",
    )
    require(isinstance(lock["sources"], list), "released sources required")
    helm = [
        s
        for s in lock["sources"]
        if isinstance(s, dict) and s.get("repository") == HELM
    ]
    require(
        len(helm) == 1
        and helm[0].get("sourceSHA") == helm_sha
        and helm[0].get("ref") in [helm_sha, "refs/tags/v" + version],
        "Helm release differs from exact checkout",
    )
    return lock


def require_dispatch_token(token):
    require(
        bool(token), "ORISO_DOCS_RELEASE_TOKEN binding required before release writes"
    )


def load_contract(root, revision):
    root = pathlib.Path(root).resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    require(actual == revision, "Docs validator checkout differs from locked revision")
    require(
        not subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip(),
        "Docs validator checkout is modified",
    )
    sys.path.insert(0, str(root / "tools/understand-anything"))
    contract = importlib.import_module("bundle.release_inputs")
    require(
        pathlib.Path(contract.__file__).resolve()
        == root / "tools/understand-anything/bundle/release_inputs.py",
        "different Docs validator already loaded",
    )
    require(
        getattr(contract, "HELM_RELEASE_CONTRACT", None) == "oriso.helm-release/v1",
        "locked Docs revision lacks Helm release/asset contract",
    )
    return contract


def verify_trusted_revision(revision, api):
    """Approve source history before importing Docs Python with release tokens."""
    require(isinstance(revision, str) and SHA.fullmatch(revision), "full Docs revision required")
    base = f"repos/{OWNER}/{DOCS}"
    main = api(base + "/branches/main").get("commit", {}).get("sha")
    require(isinstance(main, str) and SHA.fullmatch(main), "Docs main revision unavailable")
    # Pin the branch tip before comparing so a moving main cannot alter the check.
    comparison = api(base + f"/compare/{revision}...{main}")
    require(
        comparison.get("status") in ["ahead", "identical"]
        and comparison.get("base_commit", {}).get("sha") == revision
        and comparison.get("merge_base_commit", {}).get("sha") == revision,
        "Docs revision must be reachable from Docs main before importing its validator",
    )


def verify_receiver(api):
    import yaml

    repository = api(f"repos/{OWNER}/{DOCS}")
    branch = repository.get("default_branch")
    require(isinstance(branch, str) and bool(branch), "Docs default branch unavailable")
    for filename in [
        "docs-publication.yml",
        "ua-public-site.yml",
        "ua-graph-refresh.yml",
    ]:
        state = api(f"repos/{OWNER}/{DOCS}/actions/workflows/{filename}")
        require(state.get("state") == "active", "Docs release workflow is not active")
        value = api(
            f"repos/{OWNER}/{DOCS}/contents/.github/workflows/{filename}?ref="
            + urllib.parse.quote(branch, safe="")
        )
        require(
            value.get("encoding") == "base64" and isinstance(value.get("content"), str),
            "Docs default-branch workflow unavailable",
        )
        workflow = yaml.safe_load(base64.b64decode(value["content"]).decode())
        require(isinstance(workflow, dict), "Docs workflow invalid")
        events = workflow.get("on", workflow.get(True, {}))
        dispatch = (
            events.get("repository_dispatch", {}) if isinstance(events, dict) else {}
        )
        require(
            isinstance(dispatch, dict)
            and "platform-release-published" in dispatch.get("types", []),
            "Docs default branch lacks release event receiver",
        )


def verify_sources(lock, contract, api, tag_sha):
    contract.validate_lock(lock, lock["documentationRevision"])
    for source in lock["sources"]:
        name = source["repository"]
        repository = api(f"repos/{OWNER}/{name}")
        require(
            repository.get("private") is False
            and repository.get("visibility") == "public",
            "release source repository is not public",
        )
        require(
            api(f'repos/{OWNER}/{name}/commits/{source["sourceSHA"]}').get("sha")
            == source["sourceSHA"],
            "released source commit does not exist",
        )
        # Helm's tag is created later by the existing release job. Every other tag
        # must already resolve to the exact declared source before release writes.
        if name != HELM and source["ref"].startswith("refs/tags/"):
            require(
                tag_sha(name, source["ref"]) == source["sourceSHA"],
                "service release tag differs from source SHA",
            )


def check_asset(asset, content):
    require(
        asset.get("state") == "uploaded"
        and type(asset.get("size")) is int
        and asset["size"] == len(content)
        and asset.get("digest") == "sha256:" + hashlib.sha256(content).hexdigest(),
        "published manifest asset differs; refusing overwrite",
    )


def wait_for_asset(client, base, release_id, asset, content):
    """Retry absent digests only; never accept or overwrite conflicting bytes."""
    asset_id = asset.get("id")
    for attempt in range(6):
        require(
            asset.get("state") == "uploaded"
            and type(asset.get("size")) is int
            and asset["size"] == len(content),
            "published manifest asset differs; refusing overwrite",
        )
        if asset.get("digest") not in [None, ""]:
            check_asset(asset, content)
            return
        if attempt == 5:
            raise ValueError("manifest asset digest still unavailable; retry the documentation job")
        require(type(asset_id) is int, "manifest asset identity unavailable")
        time.sleep(2 ** attempt)
        # Read the embedded release metadata used by the canonical receiver too.
        release = client.request("GET", base + f"/releases/{release_id}")
        assets = [a for a in release.get("assets", []) if a.get("name") == ASSET]
        require(
            len(assets) == 1 and assets[0].get("id") == asset_id,
            "manifest asset identity changed; refusing overwrite",
        )
        asset = assets[0]


def find_release(client, base, tag):
    # The by-tag endpoint only returns published releases. Authenticated lists
    # also include drafts retained after an interrupted upload/publication.
    matches = []
    for page in range(1, 11):
        releases = client.request("GET", base + f"/releases?per_page=100&page={page}")
        require(isinstance(releases, list), "release inventory unavailable")
        matches.extend(
            release for release in releases if release.get("tag_name") == tag
        )
        require(
            len(matches) <= 1,
            "multiple releases use the same tag; refusing modification",
        )
        if len(releases) < 100:
            return matches[0] if matches else None
    raise ValueError("release inventory exceeds bounded lookup; refusing modification")


def publish(lock, contract, client, tag_sha):
    verify_sources(lock, contract, client.get, tag_sha)
    tag = lock["version"]
    base = f"repos/{OWNER}/{HELM}"
    helm = next(s for s in lock["sources"] if s["repository"] == HELM)
    require(
        tag_sha(HELM, "refs/tags/" + tag) == helm["sourceSHA"],
        "published Helm tag differs from source SHA",
    )
    release = find_release(client, base, tag)
    if release is None:
        release = client.request(
            "POST",
            base + "/releases",
            data=dict(
                tag_name=tag,
                target_commitish=helm["sourceSHA"],
                name="ORISO Platform " + tag,
                draft=True,
                prerelease=False,
                body="Published Helm chart and exact reviewed source list for documentation. Technical, operator and legal acceptance remain separate.",
            ),
        )
    require(
        release.get("tag_name") == tag
        and release.get("prerelease") is False
        and type(release.get("draft")) is bool
        and type(release.get("id")) is int,
        "existing release identity differs",
    )
    # Draft release URLs can use GitHub's temporary untagged address. The final
    # public URL is checked after publication by the canonical Docs verifier.
    if release["draft"] is False:
        require(
            release.get("html_url") == lock["releaseUrl"],
            "published release URL differs",
        )
    content = contract.canonical_bytes(lock)
    assets = [a for a in release.get("assets", []) if a.get("name") == ASSET]
    require(len(assets) <= 1, "duplicate manifest assets; refusing release")
    if assets:
        asset = assets[0]
    else:
        require(
            release["draft"] is True,
            "published release lacks locked source asset; refusing modification",
        )
        asset = client.request(
            "POST",
            base + f'/releases/{release["id"]}/assets?name=' + ASSET,
            raw=content,
            upload=True,
        )
    wait_for_asset(client, base, release["id"], asset, content)
    if release["draft"]:
        client.request(
            "PATCH",
            base + f'/releases/{release["id"]}',
            # This coordination release must not change the repository's Latest selection.
            data=dict(draft=False, make_latest="false"),
        )
    # Exact published identity, full source vector and asset hash are re-read by
    # the receiver's own contract before any event is sent.
    return contract.verify_release(lock, api=client.get, tag_sha=tag_sha)


def dispatch(lock, contract, reader, sender, tag_sha):
    contract.verify_release(lock, api=reader.get, tag_sha=tag_sha)
    sender.request(
        "POST",
        f"repos/{OWNER}/{DOCS}/dispatches",
        data=dict(
            event_type="platform-release-published",
            client_payload=dict(release_manifest=lock),
        ),
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("GitHub API redirect refused")


class GitHub:
    def __init__(self, token):
        require(bool(token), "GitHub credential binding required")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, data=None, raw=None, upload=False, allow404=False):
        host = "uploads.github.com" if upload else "api.github.com"
        url = "https://" + host + "/" + path
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "oriso-helm-release-coordinator",
        }
        body = (
            raw
            if raw is not None
            else (json.dumps(data).encode() if data is not None else None)
        )
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(
                urllib.request.Request(url, data=body, headers=headers, method=method),
                timeout=60,
            ) as response:
                result = response.read()
                return json.loads(result) if result else None
        except urllib.error.HTTPError as error:
            if allow404 and error.code == 404:
                return None
            raise ValueError(
                f"GitHub {method} request failed with HTTP{error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("GitHub release verification unavailable") from None

    def get(self, path):
        return self.request("GET", path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["prepare", "verify", "publish-and-dispatch"]
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--docs-root")
    args = parser.parse_args()
    require_dispatch_token(os.environ.get("ORISO_DOCS_RELEASE_TOKEN"))
    if args.command == "prepare":
        event = parse_json(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        lock = bootstrap(event, os.environ["GITHUB_SHA"], os.environ["GITHUB_REF_NAME"])
        pathlib.Path(args.manifest).write_bytes(
            json.dumps(
                lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(
                "documentation-revision=" + lock["documentationRevision"] + "\n"
            )
        return
    lock = bootstrap(
        dict(
            inputs=dict(
                version=os.environ["RELEASE_VERSION"],
                release_manifest=pathlib.Path(args.manifest).read_text(),
            )
        ),
        os.environ["GITHUB_SHA"],
        os.environ["GITHUB_REF_NAME"],
    )
    reader = GitHub(os.environ.get("GITHUB_TOKEN"))
    verify_trusted_revision(lock["documentationRevision"], reader.get)
    contract = load_contract(args.docs_root, lock["documentationRevision"])
    sender = GitHub(os.environ.get("ORISO_DOCS_RELEASE_TOKEN"))
    if args.command == "verify":
        verify_sources(lock, contract, reader.get, contract.remote_tag_sha)
        verify_receiver(sender.get)
        print(
            "Exact public release sources and default-branch receivers verified; no release write performed"
        )
    else:
        publish(lock, contract, reader, contract.remote_tag_sha)
        dispatch(lock, contract, reader, sender, contract.remote_tag_sha)
        print(
            "Helm release source asset verified and documentation event accepted; downstream publication remains separately checked"
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
