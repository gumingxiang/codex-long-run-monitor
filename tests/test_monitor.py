"""Isolated stdlib tests: no real systemd unit or existing control socket."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
import uuid

import monitor


class MonitorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='long-run-monitor-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / 'project'
        self.output = self.workspace / 'run-output'
        self.output.mkdir(parents=True)
        self.config_file = self.base / 'config.toml'
        self.state = self.base / 'state'
        self.socket_path = self.base / 'test.sock'
        self.tid = str(uuid.uuid4())
        self.write_config()
        old = monitor.CONFIG
        monitor.CONFIG = monitor.load_config(str(self.config_file))
        self.addCleanup(setattr, monitor, 'CONFIG', old)
        self.record_path = self.state / (self.tid + '.json')

    def write_config(self, **overrides):
        values = {
            'allowed_workspaces': [str(self.workspace)],
            'state_dir': str(self.state),
            'socket_path': str(self.socket_path),
        }
        values.update(overrides)
        self.config_file.write_text('\n'.join(k + ' = ' + json.dumps(v) for k, v in values.items()))

    def record(self, **overrides):
        value = {
            'thread_id': self.tid, 'output_dir': str(self.output),
            'enabled': True, 'cadence': 60, 'verification_only': False, 'events': {},
            'config_path': str(self.config_file),
        }
        value.update(overrides)
        return value

    def task(self, status='idle', turns=None, cwd=None):
        return {
            'id': self.tid, 'cwd': str(cwd or self.workspace),
            'status': {'type': status}, 'turns': turns or [],
        }

    def rpc(self, tasks=None, start_error=None):
        replies = iter(tasks or [self.task()])
        client = mock.Mock()

        def call(method, params):
            if method == 'thread/read':
                return {'thread': next(replies)}
            if method == 'thread/resume':
                return {}
            if method == 'turn/start':
                if start_error:
                    raise start_error
                return {'turn': {'id': 'accepted-test-turn'}}
            raise AssertionError('Unexpected mocked RPC method: ' + method)

        client.call.side_effect = call
        return client

    def invoke(self, action, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            monitor.main([action, '--config', str(self.config_file), '--thread-id', self.tid, *extra])
        return json.loads(output.getvalue())

    def commands(self):
        return mock.patch.object(monitor, 'local_command', side_effect=lambda argv, check=True:
                                 subprocess.CompletedProcess(argv, 0, '', ''))

    def active_timer(self):
        statuses = iter([
            {'exit_code': 0, 'properties': 'ActiveState=inactive', 'error': ''},
        ])
        return mock.patch.object(monitor, 'timer_status', side_effect=lambda tid:
                                 next(statuses, {'exit_code': 0, 'properties': 'ActiveState=active', 'error': ''}))

    def job_marker(self, identity=None, **extra):
        job = {'thread_id': self.tid, 'attempt': 'test-attempt',
               **(identity or monitor.proc_identity(os.getpid())), **extra}
        monitor.atomic(self.output / 'monitor-job.json', job)
        return job

    def exit_marker(self, **extra):
        ended = {'thread_id': self.tid, 'attempt': 'test-attempt', 'exit_code': 7, **extra}
        monitor.atomic(self.output / 'monitor-exit.json', ended)
        return ended


class ConfigurationTests(MonitorCase):
    def test_missing_configuration_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            monitor.load_config(str(self.base / 'missing.toml'))

    def test_missing_empty_and_wrong_type_allowlist_fail_closed(self):
        for value in ('', 'allowed_workspaces = []', 'allowed_workspaces = "/tmp"'):
            with self.subTest(value=value):
                self.config_file.write_text(value)
                with self.assertRaisesRegex(ValueError, 'allowed_workspaces'):
                    monitor.load_config(str(self.config_file))

    def test_relative_missing_home_and_root_workspaces_rejected(self):
        for root in ('relative/project', str(self.base / 'absent'), str(monitor.HOME), '/'):
            with self.subTest(root=root):
                self.write_config(allowed_workspaces=[root])
                with self.assertRaises(ValueError):
                    monitor.load_config(str(self.config_file))

    def test_unknown_configuration_key_rejected(self):
        self.write_config(unrecognized_option='typo')
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            monitor.load_config(str(self.config_file))

    def test_workspace_symlink_rejected(self):
        link = self.base / 'linked-project'
        link.symlink_to(self.workspace, target_is_directory=True)
        self.write_config(allowed_workspaces=[str(link)])
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            monitor.load_config(str(self.config_file))

    def test_resolved_codex_home_controls_all_defaults(self):
        codex_home = self.base / 'codex-home'
        self.config_file.write_text('allowed_workspaces = ' + json.dumps([str(self.workspace)]))
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex_home), 'XDG_STATE_HOME': str(self.base / 'unused')}):
            config = monitor.load_config(str(self.config_file))
        self.assertEqual(config.codex_home, codex_home)
        self.assertEqual(config.state_dir, codex_home / 'long-run-monitor/state')
        self.assertEqual(config.socket_path, codex_home / 'app-server-control/app-server-control.sock')

    def test_explicit_paths_override_environment(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.base / 'other-home')}):
            config = monitor.load_config(str(self.config_file))
        self.assertEqual(config.state_dir, self.state)
        self.assertEqual(config.socket_path, self.socket_path)

    def test_xdg_default_configuration_path(self):
        root = self.base / 'xdg-config'
        destination = root / 'codex-long-run-monitor/config.toml'
        destination.parent.mkdir(parents=True)
        destination.write_text(self.config_file.read_text())
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(root)}):
            self.assertEqual(monitor.load_config().path, destination)

    def test_output_must_be_dedicated_allowed_real_directory(self):
        outside = self.base / 'outside'
        outside.mkdir()
        for path in (self.workspace, outside, self.base / 'missing'):
            with self.subTest(path=path), self.assertRaises((ValueError, FileNotFoundError)):
                monitor.safe_output(str(path))
        self.assertEqual(monitor.safe_output(str(self.output)), self.output)

    def test_symlink_output_or_marker_rejected(self):
        link = self.workspace / 'linked-output'
        link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            monitor.safe_output(str(link))
        target = self.base / 'marker.json'
        target.write_text('{}')
        (self.output / 'monitor-job.json').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            monitor.inspect_job(self.record())

    def test_task_and_output_must_match(self):
        sibling = self.workspace / 'sibling'
        sibling.mkdir()
        for task in (self.task(cwd=sibling), self.task(cwd=self.output)):
            with self.subTest(task=task), self.assertRaisesRegex(ValueError, 'dedicated child'):
                monitor.check_output_cwd(self.output, task)
        rpc = self.rpc([self.task(cwd=self.base)])
        with self.assertRaisesRegex(ValueError, 'outside authorized'):
            monitor.thread(rpc, self.tid)

    def test_state_directory_must_be_private(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'mode 700'):
            with monitor.lock(self.tid):
                pass

    def test_lock_and_record_symlinks_rejected(self):
        self.state.mkdir(mode=0o700)
        target = self.base / 'unrelated.json'
        target.write_text('{}')
        for suffix in ('.lock', '.json'):
            path = self.state / (self.tid + suffix)
            path.symlink_to(target)
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, 'Symlink'):
                with monitor.lock(self.tid):
                    pass
            path.unlink()
        self.assertEqual(target.read_text(), '{}')

    def test_atomic_marker_uses_private_mode_and_no_temp_residue(self):
        path = self.output / 'value.json'
        monitor.atomic(path, {'exit_code': 9})
        self.assertEqual(monitor.read_json(path), {'exit_code': 9})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.output.glob('*.tmp')), [])

    def test_oversized_json_rejected(self):
        path = self.output / 'oversized.json'
        path.write_bytes(b' ' * (monitor.MAX_JSON + 1))
        with self.assertRaisesRegex(ValueError, 'size limit'):
            monitor.read_json(path)


class ProcessTests(MonitorCase):
    def test_waiting_and_running_states(self):
        self.assertEqual(monitor.inspect_job(self.record())['state'], 'waiting_for_job')
        job = self.job_marker()
        self.assertEqual(job['pid'], os.getpid())
        self.assertGreater(job['start_ticks'], 0)
        self.assertEqual(monitor.inspect_job(self.record())['state'], 'running')

    def test_reused_pid_and_boot_identity_detect_process_loss(self):
        identity = monitor.proc_identity(os.getpid())
        for changed in ({'start_ticks': identity['start_ticks'] + 1}, {'boot_id': str(uuid.uuid4())}):
            self.job_marker(identity, **changed)
            with self.subTest(changed=changed):
                self.assertEqual(monitor.inspect_job(self.record())['state'], 'lost_process')

    def test_terminal_marker_while_exact_child_alive_is_deferred(self):
        self.job_marker()
        self.exit_marker()
        self.assertEqual(monitor.inspect_job(self.record())['state'], 'exiting')

    def test_real_short_child_exit_code_is_preserved(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1); sys.exit(7)'],
                                 stdin=subprocess.PIPE)
        try:
            identity = monitor.proc_identity(child.pid)
            self.assertIsNotNone(identity)
            self.job_marker(identity)
            self.assertEqual(monitor.inspect_job(self.record())['state'], 'running')
            child.communicate(b'x', timeout=5)
            self.exit_marker(exit_code=child.returncode)
            observation = monitor.inspect_job(self.record())
            self.assertEqual(observation, {'state': 'terminal', 'attempt': 'test-attempt', 'exit_code': 7})
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdin:
                child.stdin.close()

    def test_mismatched_and_noninteger_exit_markers_rejected(self):
        self.job_marker()
        for changed in ({'thread_id': str(uuid.uuid4())}, {'attempt': 'different'}, {'exit_code': True}):
            self.exit_marker(**changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'mismatch'):
                monitor.inspect_job(self.record())

    def test_invalid_job_identity_rejected(self):
        for changed in ({'pid': True}, {'pid': 1}, {'start_ticks': 0}, {'boot_id': ''},
                        {'attempt': '../invalid'}, {'thread_id': str(uuid.uuid4())}):
            self.job_marker(**changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                monitor.inspect_job(self.record())


class RegistrationTests(MonitorCase):
    def test_default_cadence_and_resolved_systemd_arguments(self):
        codex_home = self.base / 'selected codex home'
        rpc = self.rpc()
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), \
                mock.patch.object(monitor, 'RPC', return_value=rpc), self.commands() as commands, self.active_timer():
            result = self.invoke('register', '--output-dir', str(self.output))
        record = result['record']
        self.assertTrue(record['enabled'])
        self.assertEqual(record['cadence'], 60)
        self.assertFalse(record['verification_only'])
        argv = [call.args[0] for call in commands.call_args_list if call.args[0][0] == '/usr/bin/systemd-run'][0]
        self.assertIn('--on-active=60s', argv)
        self.assertIn('--on-unit-active=60s', argv)
        self.assertIn('--unit=codex-long-run-monitor-' + self.tid, argv)
        self.assertIn('--setenv=CODEX_HOME=' + str(codex_home), argv)
        self.assertEqual(argv[-8:], [str(monitor.PYTHON), '-I', str(monitor.SCRIPT), 'tick', '--config',
                                    str(self.config_file), '--thread-id', self.tid])
        self.assertEqual(record['config_path'], str(self.config_file))

    def test_timer_reload_preserves_default_state_and_socket(self):
        self.config_file.write_text('allowed_workspaces = ' + json.dumps([str(self.workspace)]))
        codex_home = self.base / 'alternate-codex-home'
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), \
                mock.patch.object(monitor, 'RPC', return_value=self.rpc()), self.commands() as commands, self.active_timer():
            self.invoke('register', '--output-dir', str(self.output))
            original = monitor.config()
        argv = [call.args[0] for call in commands.call_args_list if call.args[0][0] == '/usr/bin/systemd-run'][0]
        environment = dict(arg.removeprefix('--setenv=').split('=', 1) for arg in argv if arg.startswith('--setenv='))
        config_path = argv[argv.index('--config') + 1]
        with mock.patch.dict(os.environ, environment, clear=True):
            reloaded = monitor.load_config(config_path)
        self.assertEqual(reloaded, original)

    def test_other_registry_cannot_replace_active_thread_timer(self):
        with mock.patch.object(monitor, 'RPC', return_value=self.rpc()), self.commands() as commands, \
                mock.patch.object(monitor, 'timer_status', return_value={
                    'exit_code': 0, 'properties': 'ActiveState=active', 'error': ''}):
            with self.assertRaisesRegex(ValueError, 'original configuration'):
                self.invoke('register', '--output-dir', str(self.output))
        commands.assert_not_called()
        self.assertFalse(self.record_path.exists())

    def test_override_cadence_and_verification_registration(self):
        with mock.patch.object(monitor, 'RPC', return_value=self.rpc()), self.commands() as commands, self.active_timer():
            result = self.invoke('register', '--output-dir', str(self.output), '--cadence', '300', '--verification-only')
        self.assertEqual(result['record']['cadence'], 300)
        self.assertTrue(result['record']['verification_only'])
        argv = [call.args[0] for call in commands.call_args_list if call.args[0][0] == '/usr/bin/systemd-run'][0]
        self.assertIn('--on-unit-active=300s', argv)

    def test_cadence_range_rejected_without_systemd(self):
        with self.commands() as commands:
            for value in ('59', '3601'):
                with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.invoke('register', '--output-dir', str(self.output), '--cadence', value)
        commands.assert_not_called()

    def test_registration_options_rejected_for_other_actions(self):
        for args in (('--cadence', '60'), ('--verification-only',), ('--output-dir', str(self.output))):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke('status', *args)

    def test_identical_registration_reused_and_conflict_rejected(self):
        with mock.patch.object(monitor, 'RPC', side_effect=lambda: self.rpc()), self.commands() as commands, self.active_timer():
            first = self.invoke('register', '--output-dir', str(self.output))
            count = commands.call_count
            second = self.invoke('register', '--output-dir', str(self.output))
            self.assertEqual(commands.call_count, count)
            self.assertEqual(first['record'], second['record'])
            with self.assertRaisesRegex(ValueError, 'Active registration differs'):
                self.invoke('register', '--output-dir', str(self.output), '--cadence', '300')
            self.assertEqual(commands.call_count, count)

    def test_inactive_timer_does_not_claim_registration_enabled(self):
        with mock.patch.object(monitor, 'RPC', return_value=self.rpc()), self.commands(), \
                mock.patch.object(monitor, 'timer_status', return_value={'exit_code': 1, 'properties': '', 'error': 'failed'}):
            with self.assertRaisesRegex(RuntimeError, 'not active'):
                self.invoke('register', '--output-dir', str(self.output))
        self.assertFalse(monitor.read_json(self.record_path)['enabled'])

    def test_stop_preserves_job_and_events_and_only_stops_timer(self):
        record = self.record(events={'test-attempt:terminal': {'turn_id': 'old-turn'}})
        monitor.atomic(self.record_path, record)
        self.job_marker()
        before = (self.output / 'monitor-job.json').read_bytes()
        with self.commands() as commands, self.active_timer(), mock.patch.object(monitor, 'RPC') as rpc:
            result = self.invoke('stop')
        self.assertFalse(result['record']['enabled'])
        self.assertEqual(result['record']['events'], record['events'])
        self.assertEqual((self.output / 'monitor-job.json').read_bytes(), before)
        commands.assert_called_once_with(['/usr/bin/systemctl', '--user', 'stop', monitor.unit(self.tid) + '.timer'])
        rpc.assert_not_called()
        self.assertIsNotNone(monitor.proc_identity(os.getpid()))

    def test_reregister_preserves_accepted_event_history(self):
        record = self.record(enabled=False, events={'test-attempt:terminal': {'turn_id': 'old-turn'}})
        monitor.atomic(self.record_path, record)
        with mock.patch.object(monitor, 'RPC', return_value=self.rpc()), self.commands(), self.active_timer():
            result = self.invoke('register', '--output-dir', str(self.output))
        self.assertEqual(result['record']['events'], record['events'])
        self.assertEqual(len(list(self.state.glob('*.previous.*.json'))), 1)

    def test_doctor_is_read_only_and_reports_lifetime(self):
        with mock.patch.object(monitor, 'RPC', return_value=self.rpc()), self.commands() as commands:
            result = self.invoke('doctor')
        self.assertEqual(result['socket'], 'ok')
        self.assertEqual(result['user_bus'], 'ok')
        self.assertEqual([c.args[0][0] for c in commands.call_args_list], ['/usr/bin/systemctl', '/usr/bin/loginctl'])
        self.assertFalse(self.state.exists())


class InterleavedHistoryTests(MonitorCase):
    def completed_job(self, output, attempt):
        output.mkdir(exist_ok=True)
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                 stdin=subprocess.PIPE)
        try:
            identity = monitor.proc_identity(child.pid)
            self.assertIsNotNone(identity)
            monitor.atomic(output / 'monitor-job.json', {'thread_id': self.tid, 'attempt': attempt, **identity})
            child.communicate(b'x', timeout=5)
            monitor.atomic(output / 'monitor-exit.json', {
                'thread_id': self.tid, 'attempt': attempt, 'exit_code': child.returncode})
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdin:
                child.stdin.close()

    @contextlib.contextmanager
    def simulated_services(self, first_ack_lost=False):
        active = False
        starts = []
        turns = []

        def command(argv, check=True):
            nonlocal active
            if argv[0] == '/usr/bin/systemd-run':
                active = True
            elif argv[:3] == ['/usr/bin/systemctl', '--user', 'stop']:
                active = False
            return subprocess.CompletedProcess(argv, 0, '', '')

        def status(tid):
            return {'exit_code': 0, 'properties': 'ActiveState=' + ('active' if active else 'inactive'), 'error': ''}

        def call(method, params):
            if method == 'thread/read':
                return {'thread': self.task(turns=turns)}
            if method == 'turn/start':
                starts.append(params)
                if first_ack_lost and len(starts) == 1:
                    raise EOFError('test ambiguous acknowledgement')
                turn = {'id': 'test-turn-' + str(len(starts)), 'status': 'completed', 'items': params['input']}
                turns.append(turn)
                return {'turn': turn}
            raise AssertionError('Unexpected mocked RPC method: ' + method)

        client = mock.Mock()
        client.call.side_effect = call
        with mock.patch.object(monitor, 'local_command', side_effect=command), \
                mock.patch.object(monitor, 'timer_status', side_effect=status), \
                mock.patch.object(monitor, 'RPC', return_value=client):
            yield starts

    def test_a_b_a_restores_accepted_turn_without_another_dispatch(self):
        other_output = self.workspace / 'other-output'
        self.completed_job(self.output, 'attempt-a')
        self.completed_job(other_output, 'attempt-b')
        with self.simulated_services() as starts:
            self.invoke('register', '--output-dir', str(self.output))
            first = self.invoke('tick')['record']['events']['attempt-a:terminal']
            self.invoke('stop')
            self.invoke('register', '--output-dir', str(other_output))
            self.invoke('tick')
            self.invoke('stop')
            self.invoke('register', '--output-dir', str(self.output))
            restored = self.invoke('tick')['record']['events']['attempt-a:terminal']
            self.assertEqual(len(starts), 2)
        self.assertEqual(restored['turn_id'], first['turn_id'])
        self.assertEqual(restored['dispatch_intent'], first['dispatch_intent'])
        self.assertEqual(restored['delivery'], 'accepted')

    def test_a_b_a_restores_ambiguous_intent_without_resending(self):
        other_output = self.workspace / 'other-output'
        self.completed_job(self.output, 'attempt-a')
        self.completed_job(other_output, 'attempt-b')
        with self.simulated_services(first_ack_lost=True) as starts:
            self.invoke('register', '--output-dir', str(self.output))
            with self.assertRaisesRegex(EOFError, 'ambiguous acknowledgement'):
                self.invoke('tick')
            ambiguous = self.invoke('tick')['record']['events']['attempt-a:terminal']
            self.assertEqual(ambiguous['delivery'], 'ambiguous_no_resend')
            self.invoke('stop')
            self.invoke('register', '--output-dir', str(other_output))
            self.invoke('tick')
            self.invoke('stop')
            self.invoke('register', '--output-dir', str(self.output))
            restored = self.invoke('tick')['record']['events']['attempt-a:terminal']
            self.assertEqual(len(starts), 2)
        self.assertEqual(restored['dispatch_intent'], ambiguous['dispatch_intent'])
        self.assertEqual(restored['token'], ambiguous['token'])
        self.assertEqual(restored['delivery'], 'ambiguous_no_resend')
        self.assertNotIn('turn_id', restored)

    def test_conflicting_archived_delivery_identity_fails_closed(self):
        self.state.mkdir(mode=0o700)
        event = {'token': 'MONITOR_EVENT_test', 'dispatch_intent': 1, 'turn_id': 'accepted-turn'}
        first = self.record(enabled=False, events={'attempt-a:terminal': event})
        first_path = self.state / (self.tid + '.previous.1.json')
        second_path = self.state / (self.tid + '.previous.2.json')
        monitor.atomic(first_path, first)
        for changed in ({'token': 'MONITOR_EVENT_other'}, {'dispatch_intent': 2}, {'turn_id': 'another-turn'}):
            second = self.record(enabled=False, events={'attempt-a:terminal': {**event, **changed}})
            monitor.atomic(second_path, second)
            with self.subTest(changed=changed), mock.patch.object(monitor, 'RPC', return_value=self.rpc()), \
                    self.commands() as commands, self.active_timer():
                with self.assertRaisesRegex(ValueError, 'Conflicting archived event'):
                    self.invoke('register', '--output-dir', str(self.output))
                commands.assert_not_called()
            self.assertFalse(self.record_path.exists())


class DeliveryTests(MonitorCase):
    def terminal(self):
        return mock.patch.object(monitor, 'inspect_job', return_value={
            'state': 'terminal', 'attempt': 'test-attempt', 'exit_code': 7})

    def test_running_and_disabled_monitors_never_open_rpc(self):
        self.job_marker()
        with mock.patch.object(monitor, 'RPC') as rpc:
            monitor.check_tick(self.record(), self.record_path)
            monitor.check_tick(self.record(enabled=False), self.record_path)
        rpc.assert_not_called()

    def test_busy_thread_is_deferred_without_dispatch(self):
        for status in ('active', 'waitingOnApproval', 'waitingOnUserInput'):
            client = self.rpc([self.task(status)])
            with self.subTest(status=status), self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
                record = monitor.check_tick(self.record(), self.record_path)
            event = record['events']['test-attempt:terminal']
            self.assertEqual(event['delivery'], 'waiting_for_idle')
            self.assertNotIn('dispatch_intent', event)
            self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/read'])

    def test_unloaded_thread_resumes_then_rechecks_idle(self):
        client = self.rpc([self.task('notLoaded'), self.task('idle')])
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
            result = monitor.check_tick(self.record(), self.record_path)
        self.assertEqual([c.args[0] for c in client.call.call_args_list],
                         ['thread/read', 'thread/resume', 'thread/read', 'turn/start'])
        self.assertEqual(result['events']['test-attempt:terminal']['delivery'], 'accepted')

    def test_busy_after_resume_is_deferred(self):
        client = self.rpc([self.task('notLoaded'), self.task('active')])
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
            result = monitor.check_tick(self.record(), self.record_path)
        self.assertEqual(result['events']['test-attempt:terminal']['delivery'], 'waiting_for_idle')
        self.assertNotIn('turn/start', [c.args[0] for c in client.call.call_args_list])

    def test_dispatch_intent_is_persisted_before_send(self):
        client = self.rpc()
        original = client.call.side_effect

        def observe(method, params):
            if method == 'turn/start':
                event = monitor.read_json(self.record_path)['events']['test-attempt:terminal']
                self.assertIn('dispatch_intent', event)
                self.assertEqual(event['delivery'], 'sending')
                self.assertNotIn('turn_id', event)
            return original(method, params)

        client.call.side_effect = observe
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
            result = monitor.check_tick(self.record(), self.record_path)
        self.assertEqual(result['events']['test-attempt:terminal']['turn_id'], 'accepted-test-turn')

    def test_accepted_turn_is_read_back_without_resend(self):
        first_client = self.rpc()
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=first_client):
            record = monitor.check_tick(self.record(), self.record_path)
        second_client = self.rpc([self.task(turns=[{'id': 'accepted-test-turn', 'status': 'completed', 'items': []}])])
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=second_client):
            result = monitor.check_tick(record, self.record_path)
        self.assertEqual(result['events']['test-attempt:terminal']['turn_status'], 'completed')
        self.assertEqual([c.args[0] for c in second_client.call.call_args_list], ['thread/read'])

    def test_lost_ack_retains_intent_and_unresolved_history_never_resends(self):
        client = self.rpc(start_error=EOFError('test lost acknowledgement'))
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client), self.assertRaises(EOFError):
            monitor.check_tick(self.record(), self.record_path)
        record = monitor.read_json(self.record_path)
        self.assertIn('dispatch_intent', record['events']['test-attempt:terminal'])
        client = self.rpc()
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
            result = monitor.check_tick(record, self.record_path)
        self.assertEqual(result['events']['test-attempt:terminal']['delivery'], 'ambiguous_no_resend')
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/read'])

    def test_unique_event_token_reconciles_missing_ack(self):
        record = self.record(events={'test-attempt:terminal': {'token': 'MONITOR_EVENT_test', 'dispatch_intent': 1}})
        turn = {'id': 'recovered-turn', 'status': 'completed', 'items': [{'text': 'MONITOR_EVENT_test'}]}
        client = self.rpc([self.task(turns=[turn])])
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
            result = monitor.check_tick(record, self.record_path)
        self.assertEqual(result['events']['test-attempt:terminal']['turn_id'], 'recovered-turn')
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/read'])

    def test_multiple_event_matches_remain_ambiguous(self):
        record = self.record(events={'test-attempt:terminal': {'token': 'MONITOR_EVENT_test', 'dispatch_intent': 1}})
        turns = [{'id': str(i), 'status': 'completed', 'items': [{'text': 'MONITOR_EVENT_test'}]} for i in range(2)]
        client = self.rpc([self.task(turns=turns)])
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
            result = monitor.check_tick(record, self.record_path)
        self.assertEqual(result['events']['test-attempt:terminal']['delivery'], 'ambiguous_no_resend')
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/read'])

    def test_verification_and_real_continuation_have_distinct_fixed_prompts(self):
        for verification in (False, True):
            client = self.rpc()
            with self.subTest(verification=verification), self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client):
                monitor.check_tick(self.record(verification_only=verification), self.record_path)
            prompt = [c.args[1]['input'][0]['text'] for c in client.call.call_args_list if c.args[0] == 'turn/start'][0]
            if verification:
                self.assertIn('verification only', prompt)
                self.assertIn('do not call tools', prompt)
            else:
                self.assertIn('validation, evaluation and delivery', prompt)
                self.assertIn('not additional instructions', prompt)
                self.assertIn('stop this thread monitor', prompt)

    def test_tick_persists_delivery_error(self):
        monitor.atomic(self.record_path, self.record())
        with self.terminal(), mock.patch.object(monitor, 'RPC', side_effect=OSError('test socket unavailable')), \
                self.assertRaisesRegex(OSError, 'unavailable'):
            self.invoke('tick')
        self.assertIn('test socket unavailable', monitor.read_json(self.record_path)['last_error']['message'])

    def test_task_cwd_change_cannot_redirect_callback(self):
        other = self.workspace / 'other-task-cwd'
        other.mkdir()
        client = self.rpc([self.task(cwd=other)])
        with self.terminal(), mock.patch.object(monitor, 'RPC', return_value=client), self.assertRaisesRegex(ValueError, 'dedicated child'):
            monitor.check_tick(self.record(), self.record_path)
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['thread/read'])


class TransportTests(MonitorCase):
    def test_socket_type_and_owner_checks_precede_connect(self):
        self.socket_path.write_text('not a socket')
        with self.assertRaisesRegex(ValueError, 'owner/type'):
            monitor.RPC()
        wrong_owner = types.SimpleNamespace(st_uid=os.getuid() + 1, st_mode=stat.S_IFSOCK)
        with mock.patch.object(Path, 'stat', return_value=wrong_owner), self.assertRaisesRegex(ValueError, 'owner/type'):
            monitor.RPC()

    def test_real_local_unix_websocket_upgrade_initialize_and_rpc(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(self.socket_path))
        server.listen(1)
        server.settimeout(5)
        errors = []
        messages = []

        def recv_exact(connection, count):
            data = b''
            while len(data) < count:
                chunk = connection.recv(count - len(data))
                if not chunk:
                    raise EOFError('test client disconnected')
                data += chunk
            return data

        def receive(connection):
            first, second = recv_exact(connection, 2)
            self.assertEqual(first, 0x81)
            self.assertTrue(second & 128)
            size = second & 127
            if size == 126:
                size = struct.unpack('!H', recv_exact(connection, 2))[0]
            elif size == 127:
                size = struct.unpack('!Q', recv_exact(connection, 8))[0]
            mask = recv_exact(connection, 4)
            body = recv_exact(connection, size)
            return json.loads(bytes(value ^ mask[i % 4] for i, value in enumerate(body)))

        def send(connection, message):
            body = json.dumps(message).encode()
            size = bytes([len(body)]) if len(body) < 126 else b'\x7e' + struct.pack('!H', len(body))
            connection.sendall(b'\x81' + size + body)

        def serve():
            try:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(5)
                    header = b''
                    while not header.endswith(b'\r\n\r\n'):
                        header += recv_exact(connection, 1)
                    fields = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
                    key = fields[b'Sec-WebSocket-Key'].strip()
                    accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                    connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                    initialize = receive(connection)
                    messages.append(initialize)
                    send(connection, {'id': initialize['id'], 'result': {'test': 'initialized'}})
                    messages.append(receive(connection))
                    request = receive(connection)
                    messages.append(request)
                    send(connection, {'id': request['id'], 'result': {'thread': self.task()}})
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        with contextlib.closing(monitor.RPC()) as client:
            task = monitor.thread(client, self.tid)
        worker.join(timeout=6)
        self.assertFalse(worker.is_alive(), 'fake Unix server did not terminate')
        if errors:
            raise errors[0]
        self.assertEqual([m['method'] for m in messages], ['initialize', 'initialized', 'thread/read'])
        self.assertEqual(task['id'], self.tid)


if __name__ == '__main__':
    unittest.main()
