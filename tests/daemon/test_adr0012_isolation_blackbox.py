"""Subprocess-level proofs for ADR-0012's development/production boundary.

Every command found through PATH is a temporary fake.  These tests never probe,
signal, overwrite, or otherwise depend on the machine-global daemon.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _dev_link_script(fake_repo: Path) -> str:
    tasks = tomllib.loads((REPO / "mise.toml").read_text())["tasks"]
    return tasks["dev-link"]["run"].replace("{{config_root}}", str(fake_repo))


def _upgrade_global_script(config_root: Path = REPO) -> str:
    tasks = tomllib.loads((REPO / "mise.toml").read_text())["tasks"]
    return tasks["upgrade-global"]["run"].replace(
        "{{config_root}}", str(config_root))


def test_dev_link_preserves_global_names_and_writes_isolated_executable_wrappers(
        tmp_path):
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    fake_path = tmp_path / "fake-path"
    fake_repo = tmp_path / "checkout"
    dev_root = tmp_path / "dev-runtime"
    local_bin.mkdir(parents=True)

    # These represent real globally installed entrypoints.  dev-link must
    # leave their bytes and executable mode untouched.
    global_bins = {}
    for name in ("browserwright", "browserwright-daemon"):
        path = _executable(local_bin / name, f"#!/bin/sh\necho global-{name}\n")
        global_bins[name] = (path.read_bytes(), path.stat().st_mode)

    # The wrapper target is a fake checkout executable that exposes only the
    # environment and argv relevant to the isolation contract.
    target = """#!/bin/sh
printf '%s|%s|%s|%s|%s|%s|%s|%s\\n' \
  "$XDG_RUNTIME_DIR" "$TMPDIR" "$BS_HOME" \
  "$BD_EXTENSION_PORT" "$BD_FACADE_PORT" "$BD_CDP_PORT" \
  "$BW_DAEMON_URL" "$*"
"""
    for name in ("browserwright", "browserwright-daemon"):
        _executable(fake_repo / ".venv" / "bin" / name, target)

    # dev-link runs uv sync, but dependency installation is irrelevant to this
    # shell boundary and must not reach the network in a unit test.
    uv_log = tmp_path / "uv.log"
    _executable(
        fake_path / "uv",
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {uv_log!s}\nexit 0\n",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "BROWSERWRIGHT_DEV_ROOT": str(dev_root),
        "BROWSERWRIGHT_DEV_EXT_PORT": "43101",
        "BROWSERWRIGHT_DEV_FACADE_PORT": "43102",
        "BROWSERWRIGHT_DEV_CDP_PORT": "43103",
        "PATH": f"{fake_path}{os.pathsep}{os.environ['PATH']}",
    }

    proc = subprocess.run(
        ["bash"], input=_dev_link_script(fake_repo), text=True,
        capture_output=True, env=env, cwd=REPO, timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert uv_log.read_text().strip() == "sync --extra ux"

    for name, (before_bytes, before_mode) in global_bins.items():
        path = local_bin / name
        assert path.read_bytes() == before_bytes
        assert path.stat().st_mode == before_mode
        assert not path.is_symlink()

    expected_prefix = (
        f"{dev_root}/rt|{dev_root}/tmp|{dev_root}/home|"
        "43101|43102|43103|http://127.0.0.1:43102|"
    )
    for name in ("browserwright", "browserwright-daemon"):
        wrapper = local_bin / f"{name}-dev"
        assert wrapper.is_file() and os.access(wrapper, os.X_OK)
        called = subprocess.run(
            [str(wrapper), "probe", "--flag"], text=True,
            capture_output=True, env={**os.environ, "HOME": str(home)},
            timeout=5,
        )
        assert called.returncode == 0, called.stderr
        assert called.stdout.strip() == expected_prefix + "probe --flag"


def test_upgrade_global_busy_exits_four_before_install_or_restart(tmp_path):
    fake_path = tmp_path / "fake-path"
    fake_repo = tmp_path / "checkout"
    calls = tmp_path / "calls.log"
    daemon_pid = tmp_path / "daemon.pid"
    daemon_pid.write_text("4207\n")

    _executable(
        fake_repo / ".venv" / "bin" / "browserwright-daemon",
        f"""#!/bin/sh
printf '%s\n' "$*" >> {calls!s}
case "$1" in
  activity)
    echo 'busy: session-7 is running code'
    exit 4
    ;;
  restart)
    echo 9999 > {daemon_pid!s}
    exit 0
    ;;
esac
exit 99
""",
    )
    _executable(
        fake_path / "uv",
        f"#!/bin/sh\nprintf 'uv %s\\n' \"$*\" >> {calls!s}\nexit 99\n",
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_path}{os.pathsep}{os.environ['PATH']}",
    }

    proc = subprocess.run(
        ["bash"], input=_upgrade_global_script(fake_repo), text=True,
        capture_output=True, env=env, cwd=REPO, timeout=20,
    )

    assert proc.returncode == 4
    assert "session-7" in proc.stderr
    assert calls.read_text().splitlines() == ["activity"]
    assert daemon_pid.read_text() == "4207\n"


def test_upgrade_global_second_activity_gate_stops_before_status_or_reload(
        tmp_path):
    fake_path = tmp_path / "fake-path"
    fake_repo = tmp_path / "checkout"
    home = tmp_path / "home"
    global_bin = home / ".local" / "bin"
    calls = tmp_path / "calls.log"
    home.mkdir(exist_ok=True)

    _executable(
        fake_repo / ".venv" / "bin" / "browserwright-daemon",
        f"#!/bin/sh\nprintf 'preflight %s\\n' \"$*\" >> {calls!s}\nexit 0\n",
    )
    _executable(
        global_bin / "browserwright",
        f"""#!/bin/sh
printf 'global-cli %s\n' "$*" >> {calls!s}
[ "$1" = version ] && echo 1.2.3 && exit 0
exit 99
""",
    )
    _executable(
        global_bin / "browserwright-daemon",
        f"""#!/bin/sh
printf 'global-daemon %s\n' "$*" >> {calls!s}
case "$1 $2" in
  'activity '*) echo 'busy: session-8 became active'; exit 4 ;;
  *) exit 99 ;;
esac
""",
    )
    _executable(
        fake_path / "uv",
        f"#!/bin/sh\nprintf 'uv %s\\n' \"$*\" >> {calls!s}\nexit 0\n",
    )
    _executable(fake_path / "uname", "#!/bin/sh\necho Linux\n")
    _executable(fake_path / "getconf", "#!/bin/sh\necho /global/tmp/\n")

    proc = subprocess.run(
        ["bash"], input=_upgrade_global_script(fake_repo), text=True,
        capture_output=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_path}:/usr/bin:/bin",
        },
        cwd=REPO, timeout=20,
    )

    assert proc.returncode == 4
    assert "session-8" in proc.stderr
    assert calls.read_text().splitlines() == [
        "preflight activity",
        "uv tool install browserwright --force --refresh",
        "global-cli version",
        "global-daemon activity",
    ]


def test_upgrade_global_rechecks_activity_before_same_version_extension_reload(
        tmp_path):
    fake_path = tmp_path / "fake-path"
    fake_repo = tmp_path / "checkout"
    home = tmp_path / "home"
    global_bin = home / ".local" / "bin"
    calls = tmp_path / "calls.log"
    activity_count = tmp_path / "activity-count"
    canonical_tmp = tmp_path / "canonical-global-tmp"
    canonical_tmp.mkdir()
    home.mkdir()

    _executable(
        fake_repo / ".venv" / "bin" / "browserwright-daemon",
        f"#!/bin/sh\nprintf 'preflight %s\\n' \"$*\" >> {calls!s}\nexit 0\n",
    )
    _executable(
        global_bin / "browserwright",
        f"""#!/bin/sh
printf 'global-cli %s\n' "$*" >> {calls!s}
[ "$1" = version ] && echo 1.2.3 && exit 0
exit 99
""",
    )
    _executable(
        global_bin / "browserwright-daemon",
        f"""#!/bin/sh
printf 'global-daemon %s\n' "$*" >> {calls!s}
case "$1 $2" in
  'activity '*)
    count=$(cat {activity_count!s} 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > {activity_count!s}
    if [ "$count" -eq 1 ]; then exit 0; fi
    echo 'busy: session-9 became active'
    exit 4
    ;;
  'status --json') echo '{{"alive": true, "version": "1.2.3"}}'; exit 0 ;;
  *) exit 99 ;;
esac
""",
    )
    _executable(
        fake_path / "uv",
        f"#!/bin/sh\nprintf 'uv %s\\n' \"$*\" >> {calls!s}\nexit 0\n",
    )
    _executable(fake_path / "uname", "#!/bin/sh\necho Linux\n")
    _executable(
        fake_path / "getconf",
        f"#!/bin/sh\necho {canonical_tmp!s}/\n",
    )
    script = _upgrade_global_script(fake_repo).replace(
        "ext_changed=0", "ext_changed=1", 1)

    proc = subprocess.run(
        ["bash"], input=script, text=True, capture_output=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_path}:/usr/bin:/bin",
        },
        cwd=REPO, timeout=20,
    )

    assert proc.returncode == 4
    assert "session-9" in proc.stderr
    assert calls.read_text().splitlines() == [
        "preflight activity",
        "uv tool install browserwright --force --refresh",
        "global-cli version",
        "global-daemon activity",
        "global-daemon status --json",
        "global-daemon activity",
    ]


@pytest.mark.parametrize(("running_version", "expects_restart", "pi_manifest", "expected_rc", "reported"), [
    ("1.2.3", False, '{"version":"1.2.3"}', 0, "1.2.3"),
    ("1.2.2", True, '{"version":"1.2.3"}', 0, "1.2.3"),
    ("1.2.3", False, '{"version":"1.2.2"}', 1, "1.2.2"),
    ("1.2.3", False, None, 1, "unknown"),
    ("1.2.3", False, "not json", 1, "unknown"),
])
def test_upgrade_global_restarts_only_when_running_version_differs(
        tmp_path, running_version, expects_restart, pi_manifest, expected_rc,
        reported):
    fake_path = tmp_path / "fake-path"
    fake_repo = tmp_path / "checkout"
    activated_bin = tmp_path / "activated-checkout" / ".venv" / "bin"
    home = tmp_path / "home"
    global_bin = home / ".local" / "bin"
    canonical_tmp = tmp_path / "canonical-global-tmp"
    canonical_tmp.mkdir()
    calls = tmp_path / "calls.log"
    observed_format = "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s"
    observed_args = " ".join([
        '"${BW_DAEMON_URL-unset}"', '"${BD_CONFIG-unset}"',
        '"${XDG_RUNTIME_DIR-unset}"', '"${TMPDIR-unset}"',
        '"${BS_HOME-unset}"', '"${BD_EXTENSION_PORT-unset}"',
        '"${BD_FACADE_PORT-unset}"', '"${BD_CDP_PORT-unset}"',
        '"${BD_FACADE_HOST-unset}"',
        '"${PNPM_CONFIG_MINIMUM_RELEASE_AGE_EXCLUDE-unset}"',
    ])
    daemon = f"""#!/bin/sh
printf 'global-daemon %s|{observed_format}\n' "$*" {observed_args} >> {calls!s}
case "$1 $2" in
  'activity '*) exit 0 ;;
  'status --json')
    echo '{{"alive": true, "version": "{running_version}"}}'
    exit 0
    ;;
  'restart '*)
    echo '{{"interrupted": []}}'
    exit 0
    ;;
  'extension reload') exit 0 ;;
  'version check') exit 0 ;;
esac
exit 99
"""
    _executable(
        fake_repo / ".venv" / "bin" / "browserwright-daemon",
        f"#!/bin/sh\nprintf 'preflight %s\\n' \"$*\" >> {calls!s}\nexit 0\n",
    )
    _executable(global_bin / "browserwright-daemon", daemon)
    _executable(
        global_bin / "browserwright",
        f"""#!/bin/sh
printf 'global-browserwright %s|{observed_format}\n' "$*" {observed_args} >> {calls!s}
case "$*" in
  version) echo 1.2.3 ; exit 0 ;;
  'version check --strict-daemon') exit 0 ;;
esac
exit 99
""",
    )
    for name in ("browserwright", "browserwright-daemon"):
        _executable(
            activated_bin / name,
            f"#!/bin/sh\nprintf 'PATH-POLLUTION-{name} %s\\n' \"$*\" >> {calls!s}\nexit 88\n",
        )
    _executable(
        fake_path / "uv",
        f"#!/bin/sh\nprintf 'uv %s\\n' \"$*\" >> {calls!s}\nexit 0\n",
    )
    _executable(
        fake_path / "pi",
        f"#!/bin/sh\nprintf 'pi %s|age-exclude=%s\\n' \"$*\" "
        f"\"${{PNPM_CONFIG_MINIMUM_RELEASE_AGE_EXCLUDE-unset}}\" >> {calls!s}\n",
    )
    _executable(fake_path / "uname", "#!/bin/sh\necho Linux\n")
    _executable(
        fake_path / "getconf",
        "#!/bin/sh\n[ \"$1\" = DARWIN_USER_TEMP_DIR ] || exit 2\n"
        f"echo {canonical_tmp!s}/\n",
    )
    # Keep this black-box test focused on orchestration.  The production task
    # uses Python for JSON projection and a bounded relay-reconnect poll; a
    # deterministic stand-in avoids turning that intentional 20-second poll
    # into the subprocess test's own 20-second timeout on Linux CI.
    _executable(
        fake_path / "python3",
        f"#!/bin/sh\n"
        f"[ \"${{1-}}\" = -c ] && exec /usr/bin/python3 \"$@\"\n"
        f"cat >/dev/null\n"
        f"case \"${{2-}}\" in *version*) echo {running_version} ;; esac\n",
    )
    home.mkdir(exist_ok=True)
    pi_settings = home / ".pi" / "agent" / "settings.json"
    pi_settings.parent.mkdir(parents=True)
    pi_settings.write_text('{"packages":["npm:@browserwright/pi"]}')
    pi_package = (home / ".pi" / "agent" / "npm" / "node_modules" /
                  "@browserwright" / "pi" / "package.json")
    if pi_manifest is not None:
        pi_package.parent.mkdir(parents=True)
        pi_package.write_text(pi_manifest)
    polluted = {
        "BW_DAEMON_URL": "http://127.0.0.1:43102",
        "BD_CONFIG": "/dev/config.toml",
        "XDG_RUNTIME_DIR": "/dev/rt",
        "TMPDIR": "/dev/tmp",
        "BS_HOME": "/dev/home",
        "BD_EXTENSION_PORT": "43101",
        "BD_FACADE_PORT": "43102",
        "BD_CDP_PORT": "43103",
        "BD_FACADE_HOST": "dev.invalid",
    }
    env = {
        **os.environ,
        **polluted,
        "HOME": str(home),
        "PATH": f"{activated_bin}:{fake_path}:/usr/bin:/bin",
    }
    # Force the post-install reload branch without needing a real release zip;
    # Linux plus an explicit false-to-true test substitution keeps all other
    # filesystem/network work out of this subprocess.
    script = _upgrade_global_script(fake_repo).replace(
        "ext_changed=0", "ext_changed=1", 1)

    proc = subprocess.run(
        ["bash"], input=script, text=True, capture_output=True,
        env=env, cwd=REPO, timeout=20,
    )

    assert proc.returncode == expected_rc, proc.stderr
    lines = calls.read_text().splitlines()
    assert lines.count("preflight activity") == 1
    assert sum(line.startswith("global-daemon activity|") for line in lines) == 2
    assert any(line.startswith("global-daemon restart|") for line in lines) is expects_restart
    assert any(line.startswith("global-daemon status --json|") for line in lines)
    assert any(line.startswith("global-daemon extension reload|") for line in lines)
    assert "pi update npm:@browserwright/pi|age-exclude=@browserwright/pi" in lines
    assert any(line.startswith("global-browserwright version check --strict-daemon|")
               for line in lines)
    assert any(line.startswith("global-daemon version check --strict-daemon|")
               for line in lines)
    assert not any(line.startswith("PATH-POLLUTION-") for line in lines)
    for line in lines:
        if line.startswith(("global-daemon ", "global-browserwright ")):
            assert line.endswith(
                f"unset|unset|unset|{canonical_tmp!s}/|"
                "unset|unset|unset|unset|unset|unset")
    if expected_rc:
        assert f"Pi extension version {reported} does not match" in proc.stderr


def _e2e_fake_environment(tmp_path: Path, *, daemon_rc: int) -> tuple[dict, Path, Path]:
    fake_path = tmp_path / "fake-path"
    uv_log = tmp_path / "uv.log"
    daemon_log = tmp_path / "daemon.log"
    ports = " ".join([
        "TEST_EXT_PORT=44101", "TEST_CDP_PORT=44102",
        "TEST_FACADE_L1_PORT=44103", "TEST_FACADE_L1_EXT_PORT=44104",
        "TEST_FACADE_EXT_PORT=44105", "TEST_FACADE_CDP_PORT=44106",
        "TEST_FACADE_AUTOFACADE_PORT=44107",
    ])
    _executable(
        fake_path / "uv",
        f"""#!/bin/sh
printf '%s\\n' "$*" >> {uv_log!s}
case "$*" in
  *tests/daemon/e2e/_e2e_ports.py*) echo '{ports}' ; exit 0 ;;
  *'-m pytest'*) exit 23 ;;
esac
exit 99
""",
    )
    _executable(
        fake_path / "browserwright-daemon",
        f"""#!/bin/sh
printf '%s|XDG=%s|TMP=%s|HOMEVAR=%s|URL=%s\\n' \
  "$*" "${{XDG_RUNTIME_DIR-unset}}" "${{TMPDIR-unset}}" \
  "${{BS_HOME-unset}}" "${{BW_DAEMON_URL-unset}}" >> {daemon_log!s}
echo 'executor(s) running code right now: session-7'
exit {daemon_rc}
""",
    )
    # Prevent the runner's stale-port scan from consulting real sockets.
    _executable(fake_path / "lsof", "#!/bin/sh\nexit 1\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_path}{os.pathsep}{os.environ['PATH']}",
        # The gate promises to remove all of these before invoking the global
        # command.  The fake records whether that actually happened.
        "XDG_RUNTIME_DIR": "/must-not-leak/xdg",
        "TMPDIR": "/must-not-leak/tmp",
        "BS_HOME": "/must-not-leak/home",
        "BW_DAEMON_URL": "http://must-not-leak.invalid:9",
    }
    return env, uv_log, daemon_log


def test_e2e_activity_exit_four_refuses_before_pytest(tmp_path):
    env, uv_log, daemon_log = _e2e_fake_environment(tmp_path, daemon_rc=4)

    proc = subprocess.run(
        ["bash", str(REPO / "tests/daemon/e2e/run.sh"), "chosen_test.py", "-q"],
        text=True, capture_output=True, env=env, cwd=REPO, timeout=20,
    )

    assert proc.returncode == 4
    assert "refusing to start e2e" in proc.stderr
    assert "session-7" in proc.stderr
    invocations = uv_log.read_text().splitlines()
    assert len(invocations) == 1
    assert "_e2e_ports.py" in invocations[0]
    line = daemon_log.read_text().strip()
    assert line.startswith("activity|")
    assert "XDG=unset|TMP=unset|HOMEVAR=unset|URL=unset" in line


def test_e2e_force_bypasses_activity_and_reaches_pytest(tmp_path):
    env, uv_log, daemon_log = _e2e_fake_environment(tmp_path, daemon_rc=4)
    env["E2E_FORCE"] = "1"

    proc = subprocess.run(
        ["bash", str(REPO / "tests/daemon/e2e/run.sh"), "chosen_test.py", "-q"],
        text=True, capture_output=True, env=env, cwd=REPO, timeout=20,
    )

    # The fake uv's distinctive final code proves run.sh reached its pytest
    # exec instead of silently treating force as success.
    assert proc.returncode == 23
    assert not daemon_log.exists()
    invocations = uv_log.read_text().splitlines()
    assert len(invocations) == 2
    assert "_e2e_ports.py" in invocations[0]
    assert invocations[1] == "run python -m pytest chosen_test.py -q"


def test_e2e_flags_only_still_selects_the_e2e_suite(tmp_path):
    # `run.sh -v` (what `mise run test:e2e` runs) must select tests/daemon/e2e:
    # the e2e conftest skips every real_chrome test unless a path under it is
    # given, so passing flags straight through ran the unit suite green with
    # all of e2e skipped.
    env, uv_log, _ = _e2e_fake_environment(tmp_path, daemon_rc=0)

    proc = subprocess.run(
        ["bash", str(REPO / "tests/daemon/e2e/run.sh"), "-q", "-k", "relay"],
        text=True, capture_output=True, env=env, cwd=REPO, timeout=20,
    )

    assert proc.returncode == 23
    assert uv_log.read_text().splitlines()[1] == (
        "run python -m pytest -q -k relay tests/daemon/e2e")
