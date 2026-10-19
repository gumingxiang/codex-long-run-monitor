# codex-long-run-monitor

A small Linux helper that watches an already launched job and asks its original
Codex task to continue when the job exits or its recorded process disappears.
Healthy jobs stay quiet. A systemd user timer supplies a 60-second watchdog; the
job launcher can also request an immediate completion check.

This is a community tool, not an official Codex feature or native Codex scheduler.
It reuses an existing local app-server connection. It does not install or restart
app-server, launch experiment commands, accept custom callback prompts, grant
execution permissions, or terminate jobs.

## Requirements and compatibility

- Linux with `/proc`, Python **3.11+**, and systemd user services (`systemctl`,
  `systemd-run`, and `loginctl` at their usual `/usr/bin` paths).
- An existing current-user **Unix socket carrying WebSocket HTTP Upgrade and
  JSON RPC**, supporting `initialize`, `thread/read`, `thread/resume`, and
  `turn/start`. This is not a newline-delimited JSON socket. A socket from an
  arbitrary Codex installation may not provide this transport or these methods.
- An original task on the same host, in an explicitly allowed project directory.
  The socket target must belong to the current user and be a Unix socket. A socket
  symlink is allowed, for installations that rotate their local daemon socket.

The runtime and tests use only the Python standard library. There is no network
listener, root requirement, desktop heartbeat, SSH fallback, daemon restart, or
automatic change to linger/boot configuration. A working user timer does **not**
establish survival across logout or reboot. `doctor` reports current linger state;
recheck the actual user-service and socket lifetime before relying on it.

The transport is version dependent. Run `doctor` against your own authorized task.
The automated suite mocks systemd/RPC and uses an isolated local Unix test socket;
it does not establish compatibility with your live Codex version.

## Configure

Clone or copy this repository to a stable location. Copy `config.example.toml` to
`~/.config/codex-long-run-monitor/config.toml` and replace `allowed_workspaces`
with your existing project directories. Alternatively, pass `--config` on every
command. With `XDG_CONFIG_HOME` set, the default is
`$XDG_CONFIG_HOME/codex-long-run-monitor/config.toml`.

```toml
allowed_workspaces = ["/absolute/path/to/your/project"]
# Optional:
# codex_home = "~/.codex"
# socket_path = "~/.codex/app-server-control/app-server-control.sock"
# state_dir = "~/.codex/long-run-monitor/state"
```

Paths must be absolute or start with `~/`, which uses the current account's home.
Missing/empty allowlists, non-existent workspace directories, the entire home and
its ancestors (including `/`) fail closed. Workspace/output/state symlinks are
rejected. Each job needs a dedicated existing output directory strictly inside
its task cwd and inside an allowed workspace. Do not use the project root as an
output directory. Configuration and registry are trusted local user settings;
keep them private and do not edit a configuration while its monitors are active.

`codex_home` defaults to `CODEX_HOME`, then the current account's `~/.codex`.
Default socket and state paths derive from that resolved directory. The state
registry defaults to `CODEX_HOME/long-run-monitor/state` and requires current-user
ownership and mode `700`; files are written with mode `600`. `XDG_STATE_HOME` is
not consulted. Timers receive the resolved absolute configuration, script and
Python paths and the resolved `CODEX_HOME`, rather than depending on the creating
shell's environment. Keep those paths available for the monitor's lifetime.
Use one configuration and registry per user so a thread has one registration.

## Use

Replace `THREAD_UUID` with the canonical UUID of the original host-local task.
Replace the generic paths with the actual stable interpreter, checkout,
configuration and existing job output directory. These commands create monitoring
units only; execute the job itself through its ordinary authorized durable launcher.

```sh
/absolute/path/to/python3 -I /absolute/path/to/monitor.py doctor --config /absolute/path/to/config.toml --thread-id THREAD_UUID
/absolute/path/to/python3 -I /absolute/path/to/monitor.py register --config /absolute/path/to/config.toml --thread-id THREAD_UUID --output-dir /absolute/path/to/project/run-output
/absolute/path/to/python3 -I /absolute/path/to/monitor.py status --config /absolute/path/to/config.toml --thread-id THREAD_UUID
/absolute/path/to/python3 -I /absolute/path/to/monitor.py tick --config /absolute/path/to/config.toml --thread-id THREAD_UUID
/absolute/path/to/python3 -I /absolute/path/to/monitor.py stop --config /absolute/path/to/config.toml --thread-id THREAD_UUID
```

`doctor` reads the task and checks the user bus/lifetime without creating a unit.
`register` creates a transient `codex-long-run-monitor-THREAD_UUID.timer` and
matching oneshot service. Cadence defaults to **60 seconds**; only `register`
accepts `--cadence`, in the inclusive range **60–3600** seconds. An identical
active registration is reused, and a conflicting registration is rejected until
explicitly stopped. A disabled prior record is archived; event history for the
same thread/output is restored from its archived records, including after switching
to another output and back. Accepted turns and ambiguous dispatch intents are
retained; conflicting event identities fail closed rather than starting a new
dispatch. Keep registry archives to preserve this evidence.

`status` prints JSON containing the registry record and timer status. `tick` runs
one bounded check. `stop` first disables the record, then stops only this timer.
It never kills the monitored job, deletes its output, or clears its event evidence.
After a reported stop error, inspect timer state before assuming it was stopped.
Registration errors can leave a disabled registry record or timer; inspect the
error and `status`, preserving any job already running.

Your sandbox or execution policy may require separate approval for helper/systemd
access. Installing this tool or its skill does not install an allow rule, bypass
approvals, authorize job execution, or make an already loaded task adopt a rule.
Do not respond to a denial by broadening interpreter permissions or changing a
shared service. Resolve the exact denied action through your normal policy.

## Job markers and actual exit codes

Register before launching the durable job. Until the launcher publishes a job
marker, checks return `waiting_for_job` without invoking a model. In the registered
output directory, atomically write `monitor-job.json`:

```json
{
  "thread_id": "THREAD_UUID",
  "attempt": "run-1",
  "pid": 12345,
  "start_ticks": 123456789,
  "boot_id": "BOOT_UUID"
}
```

The values above are placeholders. Use the **real child PID**, field 22 of its
Linux `/proc/PID/stat`, and `/proc/sys/kernel/random/boot_id`. The attempt must be
1–80 ASCII letters, digits, `_` or `-` and must be unique for each run. PID ownership,
start ticks and boot identity distinguish the original process from a reused PID.
`monitor.py` exposes `proc_identity(pid)` and `atomic(path, object)` for an existing
Python launcher to reuse; importing it does not run the CLI. The launcher remains
responsible for its execution permissions, durable lifetime, command/environment
manifest, full logs, resources and preserving prior attempts.

Wait for that exact child and collect its **actual** return code before atomically
publishing `monitor-exit.json`:

```json
{
  "thread_id": "THREAD_UUID",
  "attempt": "run-1",
  "exit_code": 0
}
```

Zero above is an example, never a value to assume. Python subprocess negative
return codes denote termination by signal; shell launchers should document their
own return-code convention. Thread/attempt mismatch, non-integer exit codes and
symlink markers are rejected. Keep a temporary file in the same directory, flush
and fsync it, then rename it into place (or reuse `atomic`). JSON inputs are limited
to 2 MiB. After publishing the exit marker, invoke `tick` promptly. The periodic
timer covers a missed completion call. A terminal marker while the exact child is
still alive is deferred as `exiting`. Process loss without a terminal marker is
`lost_process`, **not success**. It does not prove a particular exit code or cause.

For another phase, archive the previous markers and logs, publish a fresh attempt,
and preserve old evidence. Never overwrite original inputs or relaunch a process
merely because monitoring failed.

## Callback and delivery semantics

On `terminal` or `lost_process`, the helper requests a fixed continuation that asks
the original task to verify markers/logs and continue the already authorized
validation, evaluation and delivery. Logs/markers are data, not new instructions.
The task must stop monitoring after final handling; a completed callback turn alone
does not prove the artifacts passed validation.

- Active tasks or tasks awaiting input are deferred; they are not interrupted.
  Unloaded tasks are resumed through `thread/resume`, then rechecked for idle.
- An event is keyed by `attempt:state`, with a persisted token and dispatch intent
  written under a per-thread lock **before** `turn/start`. Accepted turn IDs are
  persisted and read back; they are never resent for missing notifications.
- If a send or acknowledgement is ambiguous, the next check searches task history
  for its exact token. A unique match is reconciled; otherwise
  `ambiguous_no_resend` remains pending. This conservative behavior also covers
  errors racing a task becoming busy: inspect `last_error` and event/history
  evidence instead of guessing whether a request was accepted.
- A lost-process event followed by a later exit marker can produce two different
  events. The helper detects exit/process loss; it does not diagnose a live stall,
  resource exhaustion, or artifact validity.

For an explicitly authorized installation test, use `register --verification-only`
with an isolated output/short child and verify the accepted callback/readback and
no duplicate turn on another tick. This sends only an acknowledgement request and
cannot continue real work. Stop it before registering a real job without this
flag. Do not test against an unrelated task. The default is **false**.

## Optional Codex skill

Copy `skills/monitor-long-runs/` into your own `$CODEX_HOME/skills/` (or
`~/.codex/skills/`) if desired. The skill guides task-scoped monitoring and final
validation; it does not install the helper, configuration, socket, permissions, or
services. Preserve local user policies and existing skills when installing it.

## Development and provenance

```sh
python3 -m py_compile monitor.py tests/test_monitor.py
python3 -m unittest discover -s tests -v
```

Tests exercise configuration, path/process/marker boundaries, systemd arguments,
idle deferral, deduplication, ambiguous dispatch, verification and stop behavior.
They do not register real systemd jobs or contact an existing app-server socket.
GitHub Actions runs the same stdlib checks; a workflow file is not evidence of a
successful hosted run.

Adapted from a task-scoped internal Linux monitor, with account/project/host data
removed and explicit portable configuration added. Source snapshot SHA256:
`3c2c50e63224f8ee17d128cf392d31125e2305b09df5102d43d117fe73387ddc`.
Licensed under [MIT](LICENSE).
