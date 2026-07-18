from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_signoz_0133_uses_the_exact_upstream_image_pair_without_clickhouse_drift():
    values = yaml.safe_load((ROOT / "values.yaml.default").read_text())["signoz"]

    assert values["image"]["tag"] == "v0.133.0"
    assert values["otelCollector"]["image"]["tag"] == "v0.144.6"
    assert values["clickhouse"]["image"]["tag"] == "25.12.5"


def test_upgrade_guard_remains_operator_initiated_not_a_helm_hook():
    rendered_templates = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "templates" / "signoz" / "templates").glob("*.yaml"))
    )

    assert "signoz_upgrade_guard.py" not in rendered_templates
