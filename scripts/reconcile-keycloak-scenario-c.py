#!/usr/bin/env python3
"""Compatibility entry point; production implementation is packaged with the chart."""
import runpy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
globals().update(runpy.run_module("scenario_c_reconcile", run_name=__name__))
