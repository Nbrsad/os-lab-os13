"""OS-13 apt update/upgrade safe wrapper: offline behaviour tests.

The wrapper is driven through a stub ``apt-get`` placed on PATH, so these tests
never touch the host package database, the network or a real upgrade. What they
pin down is the contract the task is about:

* dry-run is the default and changes nothing,
* ``apt-get update`` always runs before ``apt-get upgrade``,
* a failed or partial index refresh stops the upgrade before it starts,
* an empty plan skips the upgrade instead of running it for nothing.

Run from the repository root::

    python3 -m unittest discover -s linux-lab/tests -q
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "linux-lab" / "scripts" / "os-13-apt-safe-wrapper.sh"
BASH = shutil.which("bash")

STUB = """#!/usr/bin/env bash
# Stand-in for apt-get: records the call and answers like apt on Ubuntu.
printf '%s\\n' "$*" >> "$OS13_STUB_LOG"

if [ "${1:-}" = "--version" ]; then
  printf 'apt 2.7.14 (amd64)\\n'
  exit 0
fi

sim=0
for a in "$@"; do
  case "$a" in
    -s|--simulate) sim=1 ;;
  esac
done

if [ "$*" = "update" ]; then
  if [ -n "${OS13_STUB_UPDATE_FAIL:-}" ]; then
    printf 'Err:1 http://mirror.invalid/ubuntu noble InRelease\\n'
    printf '  Could not connect to mirror.invalid:80\\n'
    printf 'E: Failed to fetch http://mirror.invalid/ubuntu/dists/noble/InRelease  Could not connect\\n'
    exit 100
  fi
  printf 'Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\\n'
  printf 'Reading package lists...\\n'
  if [ -n "${OS13_STUB_UPDATE_PARTIAL:-}" ]; then
    printf 'W: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/noble/universe/binary-amd64/Packages  404  Not Found [IP: 91.189.91.82 80]\\n'
    printf 'W: Some index files failed to download. They have been ignored, or old ones used instead.\\n'
  fi
  exit 0
fi

if [ "$sim" -eq 1 ]; then
  if [ -n "${OS13_STUB_SIM_EMPTY:-}" ]; then
    printf 'Reading package lists...\\n'
    printf '0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\\n'
    exit 0
  fi
  printf 'Reading package lists...\\n'
  printf 'Building dependency tree...\\n'
  printf 'The following packages will be upgraded:\\n  bash curl\\n'
  printf '2 upgraded, 0 newly installed, 0 to remove and 3 not upgraded.\\n'
  printf 'Inst bash [5.2.21-2] (5.2.21-3 Ubuntu:24.04/noble-updates [amd64])\\n'
  printf 'Inst curl [8.5.0-2] (8.5.0-2ubuntu10.1 Ubuntu:24.04/noble-updates [amd64])\\n'
  exit 0
fi

printf 'Unpacking bash (5.2.21-3) ...\\n'
printf 'Setting up bash (5.2.21-3) ...\\n'
exit 0
"""


@unittest.skipUnless(BASH, "bash is required (Linux, macOS, WSL or Git Bash)")
class AptSafeWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.stub = self.tmp / "stub-apt-get"
        self.stub.write_text(STUB, encoding="utf-8", newline="\n")
        self.stub.chmod(0o755)
        self.calls_file = self.tmp / "calls.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def run_wrapper(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OS13_")}
        env.update(
            {
                "APT_GET": self.stub.as_posix(),
                "SUDO": "",
                "OS13_LOG": (self.tmp / "apt-last.log").as_posix(),
                "OS13_STUB_LOG": self.calls_file.as_posix(),
                "TMPDIR": self.tmp.as_posix(),
            }
        )
        env.update(env_extra)
        return subprocess.run(
            [BASH, SCRIPT.as_posix(), *args],
            env=env,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=180,
        )

    def calls(self) -> list:
        if not self.calls_file.exists():
            return []
        return [line.strip() for line in self.calls_file.read_text().splitlines() if line.strip()]

    # -- contract --------------------------------------------------------
    def test_script_parses(self) -> None:
        result = subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_exits_zero_and_calls_nothing(self) -> None:
        result = self.run_wrapper("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_unknown_argument_is_a_usage_error(self) -> None:
        result = self.run_wrapper("--nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    def test_dry_run_is_default_and_runs_no_change(self) -> None:
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run", result.stdout)
        self.assertIn("nothing was changed", result.stdout)
        # Only the version probe and the simulation: no update, no upgrade.
        self.assertEqual(self.calls(), ["--version", "-s upgrade"])

    def test_failed_update_stops_before_upgrade(self) -> None:
        result = self.run_wrapper("--apply", "--yes", OS13_STUB_UPDATE_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("GUARD", result.stdout + result.stderr)
        self.assertEqual(self.calls(), ["--version", "update"])

    def test_order_is_update_then_simulate_then_upgrade(self) -> None:
        result = self.run_wrapper("--apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls()[:4], ["--version", "update", "-s upgrade", "-y upgrade"])
        self.assertNotIn("GUARD", result.stdout + result.stderr)

    def test_partial_index_failure_is_a_guard(self) -> None:
        result = self.run_wrapper("--apply", "--yes", OS13_STUB_UPDATE_PARTIAL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("GUARD", result.stdout + result.stderr)
        self.assertNotIn("-y upgrade", self.calls())

    def test_allow_partial_continues_on_request(self) -> None:
        result = self.run_wrapper("--apply", "--yes", "--allow-partial", OS13_STUB_UPDATE_PARTIAL="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("-y upgrade", self.calls())

    def test_empty_plan_skips_the_upgrade(self) -> None:
        result = self.run_wrapper("--apply", "--yes", OS13_STUB_SIM_EMPTY="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to upgrade", result.stdout)
        self.assertNotIn("-y upgrade", self.calls())

    def test_dist_upgrade_selection_is_honoured(self) -> None:
        result = self.run_wrapper("--apply", "--yes", "--dist-upgrade")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("-s dist-upgrade", calls)
        self.assertIn("-y dist-upgrade", calls)

    def test_noninteractive_keeps_local_configs(self) -> None:
        result = self.run_wrapper("--apply", "--yes", "--noninteractive")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        upgrade_calls = [call for call in self.calls() if call.startswith("-y")]
        self.assertEqual(upgrade_calls, ["-y -o Dpkg::Options::=--force-confold upgrade"])


if __name__ == "__main__":
    unittest.main()
