"""AC1 smoke tests: dependencies import and the console script runs."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import mcusd


def _console_script() -> str:
    """Locate ``mc-usd-export`` next to the running interpreter, else on PATH."""
    bindir = Path(sys.executable).parent
    for name in ("mc-usd-export", "mc-usd-export.exe"):
        candidate = bindir / name
        if candidate.exists():
            return str(candidate)
    found = shutil.which("mc-usd-export")
    assert found, "mc-usd-export console script is not installed"
    return found


def test_dependencies_import():
    from pxr import Sdf, Usd, UsdGeom, Vt  # noqa: F401
    import anvil
    import numpy

    assert hasattr(anvil, "Chunk")
    assert int(numpy.__version__.split(".")[0]) >= 2
    assert Usd.GetVersion() >= (0, 26, 8)


def test_console_script_help_exits_zero():
    result = subprocess.run(
        [_console_script(), "--help"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "mc-usd-export" in result.stdout


def test_console_script_version():
    result = subprocess.run(
        [_console_script(), "--version"], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert mcusd.__version__ in result.stdout
    assert mcusd.__version__ != "0.0.0+unknown"


def test_python_dash_m_alias():
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "mcusd", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
