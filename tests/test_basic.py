"""Minimal tests — no external deps."""
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "smart-clone.py"


def test_syntax():
    ast.parse(SCRIPT.read_text())


def test_help():
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert "Intelligent repository cloner" in r.stdout


def test_invalid_repo():
    r = subprocess.run([sys.executable, str(SCRIPT), "bad-format", "--dry-run"],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode != 0


if __name__ == "__main__":
    test_syntax()
    test_help()
    test_invalid_repo()
    print("all tests passed")
