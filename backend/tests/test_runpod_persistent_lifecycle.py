"""Focused shell checks for the systemd-free persistent Pod lifecycle."""

from __future__ import annotations

import json
import importlib.util
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "prereview_runpod_worker" / "scripts"
LIFECYCLE = SCRIPTS / "persistent_lifecycle.sh"
TAILSCALE = SCRIPTS / "tailscale_userspace.sh"
LOCK_WRAPPER = SCRIPTS / "lifecycle_lock_wrapper.py"


def _shell(program: str, *, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    return subprocess.run(
        ["bash", "-c", program, "--", str(LIFECYCLE), str(TAILSCALE)],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def test_lifecycle_scripts_have_valid_bash_syntax() -> None:
    for script in (LIFECYCLE, TAILSCALE):
        result = subprocess.run(
            ["bash", "-n", str(script)], check=False, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr


def test_tailscale_cli_dispatcher_maps_hyphenated_verbs_to_functions(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    for verb, function in (("serve-on", "serve_on"), ("serve-off", "serve_off"), ("serve-status", "serve_status")):
        result = _shell(
            f'source "$2"; {function}() {{ touch "$TEST_MARKER"; }}; dispatch {verb}',
            environment={"TEST_MARKER": str(marker)},
        )
        assert result.returncode == 0, result.stderr
        assert marker.exists()
        marker.unlink()


def test_start_hands_the_published_supervisor_pid_to_ready_wait(tmp_path: Path) -> None:
    run_dir = tmp_path / "pids"
    run_dir.mkdir()
    waited = tmp_path / "waited"
    result = _shell(
        'source "$1"; '
        'resolve_worker_layout() { PREREVIEW_RUNPOD_RUN_DIR="$TEST_RUN_DIR"; PREREVIEW_RUNPOD_LOG_DIR="$TEST_LOG_DIR"; }; '
        'ensure_deployment_tree() { :; }; require_command() { :; }; '
        'supervisor_pid_file() { printf "%s/persistent-supervisor.pid\\n" "$PREREVIEW_RUNPOD_RUN_DIR"; }; '
        'pid_is_live() { [[ -f "$1" ]]; }; is_exact_supervisor() { :; }; '
        'setsid() { printf "4242\\n" > "$(supervisor_pid_file)"; }; '
        'with_action_lock() { "$@"; }; '
        'wait_for_ready() { printf "%s\\n" "$1" > "$TEST_WAITED"; }; '
        'start',
        environment={"TEST_RUN_DIR": str(run_dir), "TEST_LOG_DIR": str(tmp_path), "TEST_WAITED": str(waited)},
    )
    assert result.returncode == 0, result.stderr
    assert waited.read_text(encoding="ascii") == "4242\n"


def test_startup_cleanup_contract_is_reverse_order_and_single_guarded() -> None:
    text = LIFECYCLE.read_text(encoding="utf-8")
    cleanup = text[text.index("cleanup() {") : text.index("trap 'cleanup; exit 0'")]
    shutdown = text[text.index("shutdown_children() {") : text.index("run() {")]
    assert "PREREVIEW_LIFECYCLE_CLEANED" in cleanup
    assert shutdown.index('tailscale_userspace.sh" serve-off') < shutdown.index("stop_api")
    assert shutdown.index("stop_api") < shutdown.index("stop_surya_vllm.sh")
    assert shutdown.index("stop_surya_vllm.sh") < shutdown.index('tailscale_userspace.sh" stop')
    assert cleanup.index("rm -f -- \"$READY_FILE\"") < cleanup.index("shutdown_children")
    assert cleanup.index("remove_pid_record") > cleanup.index("shutdown_children")


def test_run_locked_stage_failure_executes_cleanup_once_in_reverse_order(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    events = tmp_path / "events"
    for name, body in {
        "tailscale_userspace.sh": '#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "$TEST_EVENTS"\n',
        "start_surya_vllm.sh": '#!/usr/bin/env bash\nprintf "vllm-start\\n" >> "$TEST_EVENTS"\nexit 1\n',
        "stop_surya_vllm.sh": '#!/usr/bin/env bash\nprintf "vllm-stop\\n" >> "$TEST_EVENTS"\n',
    }.items():
        path = scripts / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    run_dir = tmp_path / "pids"
    run_dir.mkdir()
    result = _shell(
        'source "$1"; SCRIPT_DIR="$TEST_SCRIPTS"; '
        'resolve_worker_layout() { PREREVIEW_RUNPOD_RUN_DIR="$TEST_RUN_DIR"; PREREVIEW_RUNPOD_LOG_DIR="$TEST_RUN_DIR"; }; '
        'ensure_deployment_tree() { :; }; require_command() { :; }; '
        'pid_is_live() { return 1; }; write_pid_file() { printf "777\\n" > "$1"; printf "1\\n" > "$1.start_ticks"; }; '
        'bootstrap_runtime_secrets() { :; }; bootstrap_config() { :; }; '
        'stop_api() { printf "api-stop\\n" >> "$TEST_EVENTS"; }; '
        'rm() { command rm "$@"; }; (_run_locked)',
        environment={"TEST_SCRIPTS": str(scripts), "TEST_RUN_DIR": str(run_dir), "TEST_EVENTS": str(events)},
    )
    assert result.returncode != 0
    assert events.read_text(encoding="utf-8").splitlines() == [
        "start", "vllm-start", "serve-off", "api-stop", "vllm-stop", "stop"
    ]
    assert not (run_dir / "persistent-supervisor.pid").exists()
    assert not (run_dir / "persistent-supervisor.pid.start_ticks").exists()


def test_tailscaled_start_contract_uses_pod_local_statedir_only() -> None:
    text = TAILSCALE.read_text(encoding="utf-8")
    assert 'readonly VAR_ROOT="$PREREVIEW_RUNPOD_RUNTIME_ROOT/tailscale-var"' in text
    assert '"--statedir=$VAR_ROOT"' in text
    assert "chmod 0700 -- \"$VAR_ROOT\"" in text
    assert '"$(id -u):700"' in text
    assert "/workspace" not in text


def test_tailscaled_start_executes_with_pod_local_statedir(tmp_path: Path) -> None:
    source = TAILSCALE.read_text(encoding="utf-8")
    lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
    runtime = tmp_path / "runtime"
    lib = lib.replace('readonly PREREVIEW_RUNPOD_RUNTIME_ROOT="/run/prereview-surya"', f'readonly PREREVIEW_RUNPOD_RUNTIME_ROOT="{runtime}"')
    copied = tmp_path / "tail-script"
    copied.write_text(source, encoding="utf-8")
    (tmp_path / "lib.sh").write_text(lib, encoding="utf-8")
    copied.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    argv = tmp_path / "argv"
    (fake_bin / "tailscaled").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$TEST_ARGV"\n'
        'for a in "$@"; do [[ "$a" == --socket=* ]] && s="${a#--socket=}"; done\n'
        'python3 - "$s" <<"PY"\nimport socket,sys\nx=socket.socket(socket.AF_UNIX); x.bind(sys.argv[1]); x.close()\nPY\n'
        'sleep 10\n', encoding="utf-8")
    (fake_bin / "tailscale").write_text(
        '#!/usr/bin/env bash\n'
        'if [[ "$*" == *"status --json"* ]]; then printf \'%s\\n\' \'{"BackendState":"Running","Self":{"Online":true,"HostName":"pod","DNSName":"pod.tail.ts.net"}}\'; '
        'elif [[ "$*" == *"serve status --json"* ]]; then printf \'%s\\n\' \'{"Web":{},"TCP":{},"AllowFunnel":{}}\'; fi\n', encoding="utf-8")
    for item in fake_bin.iterdir(): item.chmod(0o755)
    run_dir, logs = runtime / "pids", tmp_path / "logs"
    run_dir.mkdir(parents=True); logs.mkdir()
    auth = runtime / "tailscale-auth-key"; auth.write_text("tskey-auth-" + "x" * 32 + "\n", encoding="ascii"); auth.chmod(0o600)
    result = subprocess.run([
        "bash", "-c",
        'source "$1"; resolve_worker_layout() { PREREVIEW_RUNPOD_RUN_DIR="$TEST_RUN_DIR"; PREREVIEW_RUNPOD_LOG_DIR="$TEST_LOG_DIR"; }; ensure_deployment_tree() { :; }; start',
        "--", str(copied),
    ], check=False, text=True, capture_output=True, env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "TEST_ARGV": str(argv), "TEST_RUN_DIR": str(run_dir), "TEST_LOG_DIR": str(logs), "PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME": "pod", "PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME": "pod.tail.ts.net"})
    # The sandbox may forbid AF_UNIX bind, so the fake daemon can fail after
    # argv capture; the exercised start path has already created/validated the
    # var root and launched tailscaled with the exact arguments.
    assert result.returncode != 0
    assert f"--statedir={runtime / 'tailscale-var'}" in argv.read_text(encoding="utf-8")
    var_root = runtime / "tailscale-var"
    assert var_root.is_dir() and not var_root.is_symlink() and (var_root.stat().st_mode & 0o777) == 0o700


def test_bootstrap_writer_uses_a_fake_mktemp_and_never_echoes_secret(tmp_path: Path) -> None:
    destination = tmp_path / "api-bearer-token"
    temporary = tmp_path / "temporary"
    secret = "a" * 64
    result = _shell(
        'source "$1"; '
        'mktemp() { : > "$TEST_TEMPORARY"; printf "%s\\n" "$TEST_TEMPORARY"; }; '
        'write_bootstrap_secret "$TEST_DESTINATION" "$TEST_SECRET" '
        "'^[A-Za-z0-9_-]{32,512}$'; "
        'test "$(stat -c %a "$TEST_DESTINATION")" = 600',
        environment={
            "TEST_DESTINATION": str(destination),
            "TEST_TEMPORARY": str(temporary),
            "TEST_SECRET": secret,
        },
    )

    assert result.returncode == 0, result.stderr
    assert destination.read_text(encoding="ascii") == f"{secret}\n"
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_api_status_is_read_only_with_fake_pid_and_curl(tmp_path: Path) -> None:
    run_dir = tmp_path / "pids"
    run_dir.mkdir()
    (run_dir / "persistent-api.pid").write_text("4242\n", encoding="ascii")
    marker = tmp_path / "must-not-exist"
    result = _shell(
        'source "$1"; '
        'resolve_worker_layout() { :; }; '
        'require_existing_deployment_tree() { :; }; '
        'pid_is_live() { return 0; }; '
        'require_exact_process_arguments() { '
        '[[ "$2" = -m && "$3" = prereview_runpod_worker.persistent_api.entrypoint ]]; }; '
            "curl() { printf '%s\\n' '{\"status\":\"ok\",\"ready\":true,\"gpu_concurrency\":1}'; }; "
        'PREREVIEW_RUNPOD_RUN_DIR="$TEST_RUN_DIR"; '
        'status_api; test ! -e "$TEST_MARKER"',
        environment={"TEST_RUN_DIR": str(run_dir), "TEST_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_unapproved_tailscale_secret_environment_name_is_rejected() -> None:
    result = _shell(
        'source "$1"; bootstrap_runtime_secrets',
        environment={"TAILSCALE_AUTH_KEY": "not-used-but-must-be-rejected"},
    )

    assert result.returncode != 0
    assert "not-used-but-must-be-rejected" not in result.stdout
    assert "not-used-but-must-be-rejected" not in result.stderr
    assert "Tailscale auth key must be supplied by its runtime file" in result.stderr


def test_config_bootstrap_refuses_source_sha_mismatch_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "deploy-config.json"
    source.write_text('{"not":"the-example"}\n', encoding="utf-8")
    destination = tmp_path / "runtime-config.json"
    result = _shell(
        'source "$1"; PREREVIEW_RUNPOD_CONFIG_FILE="$TEST_DESTINATION"; '
        'PREREVIEW_RUNPOD_WORKER_HOME="$TEST_WORKER"; bootstrap_config',
        environment={
            "TEST_DESTINATION": str(destination),
            "TEST_WORKER": str(tmp_path / "worker"),
            "PREREVIEW_RUNPOD_CONFIG_SOURCE_FILE": str(source),
            "PREREVIEW_RUNPOD_CONFIG_SHA256": "0" * 64,
        },
    )

    assert result.returncode != 0
    assert "config source digest mismatch" in result.stderr
    assert not destination.exists()


def test_lifecycle_documents_strict_order_and_no_funnel() -> None:
    text = LIFECYCLE.read_text(encoding="utf-8")
    tailscale = TAILSCALE.read_text(encoding="utf-8")
    run_body = text[text.index("run() {") : text.index("start_locked() {")]
    assert run_body.index('tailscale_userspace.sh" start') < run_body.index(
        'start_surya_vllm.sh'
    )
    assert run_body.index('start_surya_vllm.sh') < run_body.index("start_api")
    assert run_body.index("start_api") < run_body.index('tailscale_userspace.sh" serve-on')
    shutdown_body = text[text.index("shutdown_children() {") : text.index("run() {")]
    assert shutdown_body.index('tailscale_userspace.sh" serve-off') < shutdown_body.index(
        "stop_api"
    )
    assert shutdown_body.index("stop_api") < shutdown_body.index("stop_surya_vllm.sh")
    executable_lines = "\n".join(
        line for line in tailscale.splitlines() if not line.lstrip().startswith("#")
    )
    assert re.search(r"tailscale(?:\\s|\\\")+funnel\\b", executable_lines) is None
    assert "--tun=userspace-networking" in tailscale
    assert "--auth-key=\"file:$AUTH_KEY_FILE\"" in tailscale


def test_release_readiness_and_lifetime_lock_contracts_are_present() -> None:
    text = LIFECYCLE.read_text(encoding="utf-8")
    assert "persistent-ready" in text
    assert "READY_TIMEOUT_SECONDS=1260" in text
    assert "wait_for_ready" in text
    assert "persistent-supervisor.lock" in text
    assert "lifecycle_lock_wrapper.py" in text
    assert "_run_locked" in text
    cleanup_body = text[text.index("cleanup() {") : text.index("trap 'cleanup; exit 0'")]
    assert cleanup_body.index("rm -f -- \"$READY_FILE\"") < cleanup_body.index(
        "shutdown_children"
    )
    assert "existing API bearer differs; refusing journal-key rotation" in text
    assert "existing Tailscale auth key differs; refusing replacement" in text
    assert "config source digest mismatch" in text
    assert "config.sha256" in text
    assert "runtime config digest marker is invalid" in text
    assert "runtime config digest marker is missing" in text
    assert "config source changed during copy" in text
    assert 'value.get("gpu_concurrency") != 1' in text
    stop_wait = text[text.index("wait_for_supervisor_stop() {") : text.index("stop() {")]
    assert "remove_pid_record" not in stop_wait
    tailscale = TAILSCALE.read_text(encoding="utf-8")
    assert "persisted Tailscale Serve/Funnel configuration is unsafe" in tailscale
    assert "serve_status_is_expected off" in tailscale
    assert "tailscale-var" in tailscale
    assert '"--statedir=$VAR_ROOT"' in tailscale
    assert "legacy tailscaled socket is live" in tailscale
    assert "legacy tailscaled daemon is live" in tailscale


def test_tailscale_json_contract_rejects_extra_handler_and_funnel(tmp_path: Path) -> None:
    fake = tmp_path / "fake-tailscale"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *'serve status --json'* ]]; then\n"
        "  printf '%s\\n' \"$TEST_SERVE_JSON\"\n"
        "else\n"
        "  printf '%s\\n' '{\"BackendState\":\"Running\",\"Self\":{\"Online\":true,\"HostName\":\"surya-pod\",\"DNSName\":\"surya-pod.tail.ts.net\"}}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    base_env = {
        "TEST_FAKE": str(fake),
        "PREREVIEW_RUNPOD_TAILSCALE_HOSTNAME": "surya-pod",
        "PREREVIEW_RUNPOD_TAILSCALE_DNS_NAME": "surya-pod.tail.ts.net",
    }
    program = 'source "$2"; tailscale_bin() { printf "%s\\n" "$TEST_FAKE"; }; serve_status_is_expected on'
    good = json.dumps({"Web": {"surya-pod.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}, "TCP": {"443": {"HTTPS": True}}, "AllowFunnel": {}})
    extra = json.dumps({"Web": {"surya-pod.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787", "Text": "no"}}}}, "TCP": {"443": {"HTTPS": True}}, "AllowFunnel": {}})
    funnel = json.dumps({"Web": {"surya-pod.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}, "TCP": {"443": {"HTTPS": True}}, "AllowFunnel": {"surya-pod.tail.ts.net:443": True}})
    assert _shell(program, environment={**base_env, "TEST_SERVE_JSON": good}).returncode == 0
    assert _shell(program, environment={**base_env, "TEST_SERVE_JSON": extra}).returncode != 0
    assert _shell(program, environment={**base_env, "TEST_SERVE_JSON": funnel}).returncode != 0


def test_signal_forwarding_lock_wrapper_cleans_child_and_releases_lock(tmp_path: Path) -> None:
    lock = tmp_path / "supervisor.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    os.chmod(lock, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cleaned = tmp_path / "cleaned"
    child = tmp_path / "fake-child.sh"
    child.write_text(
        "#!/usr/bin/env bash\ntrap 'touch \"$1\"; exit 0' TERM INT HUP\nwhile :; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    wrapper = subprocess.Popen(
        [sys.executable, str(LOCK_WRAPPER), "--lock-fd", str(descriptor), "--", str(child), str(cleaned)],
        pass_fds=(descriptor,),
    )
    os.close(descriptor)
    try:
        time.sleep(0.12)
        wrapper.terminate()
        assert wrapper.wait(timeout=3) == 0
        assert cleaned.exists()
        assert subprocess.run(["flock", "--nonblock", str(lock), "true"], check=False).returncode == 0
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=3)


def test_group_signal_interrupts_foreground_startup_child_and_releases_lock(tmp_path: Path) -> None:
    lock = tmp_path / "startup.lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    os.chmod(lock, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cleaned = tmp_path / "cleanup"
    child = tmp_path / "foreground-startup.sh"
    child.write_text(
        "#!/usr/bin/env bash\ntrap 'touch \"$1\"; exit 0' TERM INT HUP\nsleep 30\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    wrapper = subprocess.Popen(
        [sys.executable, str(LOCK_WRAPPER), "--lock-fd", str(descriptor), "--", str(child), str(cleaned)],
        pass_fds=(descriptor,),
    )
    os.close(descriptor)
    try:
        time.sleep(0.12)
        wrapper.terminate()
        assert wrapper.wait(timeout=3) == 0
        assert cleaned.exists()
        assert subprocess.run(["flock", "--nonblock", str(lock), "true"], check=False).returncode == 0
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=3)


def test_lock_wrapper_rejects_an_unsafe_inherited_lock_fd(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o644)
    os.chmod(lock, 0o644)
    marker = tmp_path / "must-not-spawn"
    child = tmp_path / "child.sh"
    child.write_text('#!/usr/bin/env bash\ntouch "$1"\n', encoding="utf-8")
    child.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(LOCK_WRAPPER), "--lock-fd", str(descriptor), "--", str(child), str(marker)],
        check=False,
        pass_fds=(descriptor,),
    )
    os.close(descriptor)
    assert result.returncode != 0
    assert not marker.exists()


def test_sanitized_wrapper_and_child_environment_exclude_bootstrap_secrets(tmp_path: Path) -> None:
    lock = tmp_path / "lock"
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    os.chmod(lock, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    captured = tmp_path / "environ"
    child = tmp_path / "child.sh"
    child.write_text('#!/usr/bin/env bash\ntr "\\0" "\\n" </proc/$$/environ > "$1"\n', encoding="utf-8")
    child.chmod(0o755)
    env = {
        **os.environ,
        "PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN": "a" * 64,
        "PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY": "tskey-auth-" + "x" * 32,
    }
    command = (
        'unset PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN '
        'PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY; '
        'exec python3 "$1" --lock-fd "$2" -- "$3" "$4"'
    )
    result = subprocess.run(
        ["bash", "-c", command, "--", str(LOCK_WRAPPER), str(descriptor), str(child), str(captured)],
        check=False,
        env=env,
        pass_fds=(descriptor,),
    )
    os.close(descriptor)
    observed = captured.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "PREREVIEW_RUNPOD_BOOTSTRAP_API_BEARER_TOKEN=" not in observed
    assert "PREREVIEW_RUNPOD_BOOTSTRAP_TAILSCALE_AUTH_KEY=" not in observed


def test_lock_wrapper_forwards_signal_received_before_child_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("lock_wrapper_test", LOCK_WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeChild:
        signals: list[int] = []
        pid = os.getpgrp()

        def poll(self) -> None:
            return None

        def wait(self) -> int:
            return 0

    fake_child = FakeChild()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: fake_child)
    forwarded: list[tuple[int, int]] = []
    monkeypatch.setattr(module.os, "killpg", lambda pid, signal: forwarded.append((pid, signal)))
    original_signal = module.signal.signal
    delivered = False

    def install_then_signal(candidate: int, handler: object) -> None:
        nonlocal delivered
        if candidate == module.signal.SIGTERM and not delivered:
            delivered = True
            assert callable(handler)
            handler(candidate, None)  # type: ignore[misc]

    monkeypatch.setattr(module.signal, "signal", install_then_signal)
    descriptor = os.open(tmp_path / "lock", os.O_WRONLY | os.O_CREAT, 0o600)
    os.chmod(tmp_path / "lock", 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(sys, "argv", [str(LOCK_WRAPPER), "--lock-fd", str(descriptor), "--", "fake"])
    assert module.main() == 0
    # os.killpg is exercised by the real wrapper test above; the early handler
    # regression here proves a signal is retained until Popen returns.
    assert delivered is True
    assert forwarded == [(fake_child.pid, module.signal.SIGTERM)]
    monkeypatch.setattr(module.signal, "signal", original_signal)
