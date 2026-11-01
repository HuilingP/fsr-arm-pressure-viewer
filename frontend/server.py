"""Two independent acquisition workers; one coordinated recording session."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import tempfile
import zipfile
import shutil
import socket
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote
from uuid import uuid4

import core

FRONTEND = Path(__file__).resolve().parent
DATA_DIR = FRONTEND / 'data'
EVENT_FIELDS = ['pc_epoch_ms', 'session_elapsed_ms', 'device_slot', 'board_id', 'event_id',
    'missing_before', 'reset_count', 'receipt_us', 'timestamp_us', 'report_id', 'report_length',
    'sequence', 'status', 'delay', 'x_counts', 'y_counts', 'z_counts', 'temperature_counts',
    'sensor_timestamp_us', 'report_hex']


def write_json(path, value):
    path.write_text(json.dumps(core.json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


class Device(core.Acquisition):
    def __init__(self, slot, data_dir=DATA_DIR, claim_board=None):
        super().__init__(data_dir)
        self.slot = slot
        self.config = {}
        self.claim_board = claim_board
        self.board_id = ''
        self.extra_files = {}
        self.event_writer = None
        self.event_received = self.event_missing = self.recorded_events = 0
        self.last_event = None
        self.session_started_ns = None
        self.wire_bytes = 0
        self.last_aux_flush = 0

    def start(self, config):
        self.config = dict(config)
        self.board_id = ''
        self.event_received = self.event_missing = 0
        self.last_event = None
        super().start(config)

    def elapsed(self):
        return (time.monotonic_ns() - self.session_started_ns) / 1e6 if self.session_started_ns else math.nan

    def identify(self, board_id):
        if board_id and board_id != self.board_id:
            if self.claim_board:
                self.claim_board(self.slot, board_id)
            self.board_id = board_id

    def _extra_row(self, raw):
        extra = dict(raw.get('extra', {}))
        self.identify(extra.get('board_id', ''))
        complete = extra.get('protocol_version') == 4 and extra.get('raw_seen_mask') == 7 \
            and extra.get('raw_config_mask') == 7 and all(
                extra.get(f'raw_{name}_age_ms', math.inf) <= (100 if name == 'mag' else 30)
                for name in ('accel', 'gyro', 'mag'))
        extra.update(device_slot=self.slot, session_elapsed_ms=self.elapsed(), raw_complete=int(complete))
        return extra

    def _accept(self, raw):
        before = json.dumps(core.json_safe(vars(self.motion)), allow_nan=False) if self.extra_files else None
        previous_index = self.index
        super()._accept(raw)
        if self.extra_files and self.index != previous_index:
            try:
                self.extra_files['state'].write(json.dumps(dict(
                    index=self.index, sample_id=raw['sample_id'], boot_id=self.boot_id,
                    time_us=self.extended_time, before=json.loads(before),
                    after=core.json_safe(vars(self.motion))), allow_nan=False) + '\n')
            except Exception as error:
                self._record_failed(error)

    def _capture_chunk(self, chunk):
        if not chunk or not self.extra_files:
            return
        try:
            self.extra_files['chunks'].write(json.dumps(dict(offset=self.wire_bytes, length=len(chunk),
                pc_epoch_ms=time.time_ns()/1e6, session_elapsed_ms=self.elapsed())) + '\n')
            self.extra_files['wire'].write(chunk)
            self.wire_bytes += len(chunk)
            now = time.monotonic_ns()
            if now - self.last_aux_flush >= 1_000_000_000:
                for handle in self.extra_files.values():
                    handle.flush()
                self.last_aux_flush = now
        except Exception as error:
            self._record_failed(error)

    def _accept_event(self, raw):
        self.identify(raw.get('board_id', ''))
        self.event_received += 1
        gap = 0
        if self.last_event is not None:
            delta = (raw['event_id'] - self.last_event) & 0xffffffff
            if 1 < delta < 0x80000000:
                gap = delta - 1
        self.last_event = raw['event_id']
        self.event_missing += gap
        if self.event_writer:
            try:
                self.event_writer.writerow(dict(raw, pc_epoch_ms=time.time_ns()/1e6,
                    session_elapsed_ms=self.elapsed(), device_slot=self.slot, missing_before=gap))
                self.recorded_events += 1
            except Exception as error:
                self._record_failed(error)

    def _record_failed(self, error):
        self.record_error = f'Device {self.slot}: recording stopped: {error}'
        self._stop_record_locked()

    def start_record(self, note='', folder_name=''):
        with self.lock:
            super().start_record(note, folder_name)
            folder = Path(self.record_folder)
            try:
                self.extra_files['wire'] = (folder / 'stream.bin').open('xb')
                for key, name in (('chunks', 'chunks.jsonl'), ('state', 'processor_state.jsonl'), ('events', 'imu_events.csv')):
                    self.extra_files[key] = (folder / name).open('x', encoding='utf-8', newline='')
                self.event_writer = csv.DictWriter(self.extra_files['events'], fieldnames=EVENT_FIELDS, extrasaction='ignore')
                self.event_writer.writeheader()
                # Retain a partial frame already buffered at the exact start boundary.
                prefix = bytes(self.parser.buffer)
                self.extra_files['wire'].write(prefix)
                self.wire_bytes = len(prefix)
                self.recorded_events = 0
                self.last_aux_flush = time.monotonic_ns()
                write_json(folder / 'metadata.json', dict(
                    device_slot=self.slot, connection=self.config, board_id=self.board_id,
                    note=note, started_at_utc=datetime.now(timezone.utc).isoformat(),
                    session_elapsed_ms=self.elapsed(), fsr_channels=8, adc_voltage_v=core.ADC_VCC,
                    initial_processor_state=vars(self.motion), parser_prefix_bytes=len(prefix),
                    protocol='V3 legacy / V4 samples with optional raw IMU + V5 SH-2 events',
                    clock_note='PC arrival timestamps share a clock; ESP32 clocks are independent.',
                    velocity_frame='Fixed first valid quaternion of this connection/boot; angle zero is independent.',
                    software_sha256={name: hashlib.sha256((FRONTEND/name).read_bytes()).hexdigest()
                                     for name in ('core.py', 'server.py', 'protocol.py')},
                    initial_transport=dict(missing=self.missing, rejected=self.parser.rejected,
                                           event_missing=self.event_missing)))
            except Exception:
                self._stop_record_locked()
                raise

    def _stop_record_locked(self):
        had_files = bool(self.extra_files)
        for handle in self.extra_files.values():
            try:
                handle.close()
            except Exception as error:
                self.record_error = self.record_error or str(error)
        self.extra_files.clear()
        self.event_writer = None
        super()._stop_record_locked()
        if had_files:
            try:
                latest = self.rows[-1] if self.rows else {}
                write_json(Path(self.record_folder) / 'summary.json', dict(
                    device_slot=self.slot, board_id=self.board_id, samples=self.recorded,
                    events=self.recorded_events, bytes=self.wire_bytes, error=self.record_error,
                    connection_error=self.error, missing_samples=self.missing,
                    rejected_packets=self.parser.rejected, missing_events=self.event_missing,
                    last_sample=dict(latest), stopped_at_utc=datetime.now(timezone.utc).isoformat()))
            except Exception as error:
                self.record_error = self.record_error or str(error)

    def snapshot(self, after=0):
        with self.lock:
            result = super().snapshot(after)
            result.update(device_slot=self.slot, connection=self.config, board_id=self.board_id,
                          event_received=self.event_received, event_missing=self.event_missing,
                          recorded_events=self.recorded_events)
            return result


class Duo:
    def __init__(self, data_dir=DATA_DIR, device_profiles=None):
        self.data_dir = Path(data_dir)
        self.device_profiles = device_profiles or {}
        self.commands = threading.RLock()
        self.board_lock = threading.Lock()
        self.boards = {}
        self.devices = {slot: Device(slot, data_dir, self.claim_board) for slot in ('A', 'B')}
        self.recording = False
        self.session_dir = None
        self.started_ns = None
        self.stopped_elapsed = 0
        self.note = ''
        self.segments = {'A': 0, 'B': 0}

    def claim_board(self, slot, board_id):
        profile = self.device_profiles.get(slot, {})
        if profile.get('board_id') and board_id != profile['board_id']:
            raise ValueError(f"设备编号不符：{slot} 应连接 {profile['label']}。请检查左右连接。")
        with self.board_lock:
            for other, identity in self.boards.items():
                if other != slot and identity == board_id and self.devices[other].active:
                    raise ValueError(f'This physical ESP32 is already connected as device {other}.')
            self.boards[slot] = board_id

    def connect(self, slot, config):
        with self.commands:
            device = self.devices[slot]
            if device.active:
                raise ValueError(f'Device {slot} is already connected.')
            # Join a previous failing reader before reusing its resources.
            device.disconnect()
            source = config.get('source', 'usb')
            config = dict(config)
            profile = self.device_profiles.get(slot, {})
            if source == 'usb' and profile.get('serial_number'):
                ports = list(core.list_ports.comports()) if core.list_ports else []
                matched = [p for p in ports if p.serial_number == profile['serial_number']]
                if len(matched) != 1:
                    raise ValueError(f"未找到 {profile['label']}，请检查 USB 数据线并刷新串口。")
                selected = str(config.get('port', '')).strip()
                if selected and selected != matched[0].device:
                    raise ValueError(f"串口不匹配：{profile['label']} 当前是 {matched[0].device}。请刷新串口。")
                config.update(port=matched[0].device, serial_number=profile['serial_number'])
            if profile:
                config['device_profile'] = dict(profile)
            if source == 'wifi':
                host = str(config.get('host', '')).strip()
                if not host or host.lower() == 'auto':
                    raise ValueError('Discover and select an explicit IP for this device.')
                config['host'] = socket.gethostbyname(host)
                config['tcp_port'] = int(config.get('tcp_port', 9000))
            for other, worker in self.devices.items():
                if other == slot or not worker.active or worker.source != source:
                    continue
                same = source == 'usb' and str(worker.config.get('port', '')).casefold() == str(config.get('port', '')).casefold()
                same |= source == 'wifi' and worker.config.get('host') == config.get('host') and worker.config.get('tcp_port', 9000) == config.get('tcp_port', 9000)
                if same:
                    raise ValueError(f'That connection is already assigned to device {other}.')
            with self.board_lock:
                self.boards.pop(slot, None)
            with device.lock:
                device.start(config)
                if self.recording:
                    self._record_device(slot)
            self._event('connected', slot)

    def disconnect(self, slot):
        with self.commands:
            self.devices[slot].disconnect()
            with self.board_lock:
                self.boards.pop(slot, None)
            self._event('disconnected', slot)

    def _event(self, kind, slot=None):
        if self.session_dir:
            with (self.session_dir / 'session_events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(dict(event=kind, device_slot=slot,
                    pc_time=datetime.now(timezone.utc).isoformat())) + '\n')

    def _record_device(self, slot):
        device = self.devices[slot]
        self.segments[slot] += 1
        device.data_dir = self.session_dir / slot
        device.session_started_ns = self.started_ns
        device.start_record(self.note, f'segment_{self.segments[slot]:03d}')

    def start_record(self, note='', folder_name=''):
        with self.commands, ExitStack() as stack:
            if self.recording:
                raise ValueError('A duo recording is already active.')
            for worker in self.devices.values():
                stack.enter_context(worker.lock)
            if not any(worker.active for worker in self.devices.values()):
                raise ValueError('Connect at least one device first.')
            if not core.valid_folder_name(folder_name):
                raise ValueError('Invalid recording folder name.')
            folder = self.data_dir / (folder_name.strip() or datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid4().hex[:8])
            folder.mkdir(parents=True, exist_ok=False)
            self.session_dir = folder
            self.started_ns = time.monotonic_ns()
            self.note = str(note)[:1000]
            self.segments = {'A': 0, 'B': 0}
            write_json(folder / 'session.json', dict(started_at_utc=datetime.now(timezone.utc).isoformat(),
                note=self.note, devices={slot: worker.config for slot, worker in self.devices.items()},
                device_profiles=self.device_profiles,
                clock_note='Shared PC start boundary, independent device clocks; no hardware synchronization.'))
            # Save the exact implementation used, not only the hash, for replay.
            (folder / 'software').mkdir()
            for name in ('core.py', 'protocol.py', 'server.py'):
                (folder / 'software' / name).write_bytes((FRONTEND / name).read_bytes())
            try:
                for slot, worker in self.devices.items():
                    if worker.active:
                        self._record_device(slot)
                self.recording = True
                self._event('recording_started')
            except Exception:
                for worker in self.devices.values():
                    worker.stop_record()
                self._event('recording_start_failed')
                raise

    def stop_record(self):
        with self.commands, ExitStack() as stack:
            for worker in self.devices.values():
                stack.enter_context(worker.lock)
            for worker in self.devices.values():
                worker.stop_record()
            if self.recording:
                self.stopped_elapsed = (time.monotonic_ns()-self.started_ns)/1e6
                self._event('recording_stopped')
            self.recording = False

    def snapshot(self):
        with self.commands:
            devices = {slot: worker.snapshot(worker.index) for slot, worker in self.devices.items()}
            return dict(recording=self.recording, path=str(self.session_dir or self.data_dir),
                elapsed_ms=(time.monotonic_ns()-self.started_ns)/1e6 if self.recording else self.stopped_elapsed,
                devices=devices, device_profiles=self.device_profiles)

    def close(self):
        self.stop_record()
        for slot in self.devices:
            self.disconnect(slot)


class Handler(core.Handler):
    server_version = 'RigidityDuo/1.0'

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if not self.local_host():
                return self.send_json({'error': 'Host rejected'}, HTTPStatus.FORBIDDEN)
            if url.path == '/api/status':
                return self.send_json(self.server.duo.snapshot())
            if url.path == '/api/recordings':
                duo = self.server.duo
                entries = []
                for folder in sorted(duo.data_dir.iterdir(), reverse=True):
                    if folder.is_dir() and not folder.is_symlink() and (folder / 'session.json').is_file():
                        entries.append(dict(name=folder.name, active=duo.recording and folder == duo.session_dir))
                return self.send_json(dict(recordings=entries))
            if url.path == '/api/download':
                duo = self.server.duo
                name = parse_qs(url.query).get('name', [''])[0]
                folder = duo.data_dir / name
                if not name or Path(name).name != name or folder.is_symlink() or folder.resolve().parent != duo.data_dir.resolve() or not (folder / 'session.json').is_file():
                    raise ValueError('记录不存在')
                with duo.commands:
                    if duo.recording and folder == duo.session_dir:
                        raise ValueError('请先停止记录，再下载完整数据')
                    with tempfile.TemporaryFile() as archive:
                        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
                            for item in folder.rglob('*'):
                                if item.is_file() and not item.is_symlink() and folder.resolve() in item.resolve().parents:
                                    bundle.write(item, str(Path(folder.name) / item.relative_to(folder)))
                        size = archive.tell()
                        archive.seek(0)
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/zip')
                        self.send_header('Content-Disposition', "attachment; filename=fsr-recording.zip; filename*=UTF-8''" + quote(name + '.zip'))
                        self.send_header('Content-Length', str(size))
                        self.end_headers()
                        shutil.copyfileobj(archive, self.wfile)
                return
            if url.path == '/api/health':
                return self.send_json(dict(app='super-mini-duo-local', frontend=str(FRONTEND)))
            parts = url.path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'device'] and parts[2] in ('A', 'B') and parts[3] == 'status':
                after = max(0, int(parse_qs(url.query).get('after', ['0'])[0]))
                return self.send_json(self.server.duo.devices[parts[2]].snapshot(after))
            if url.path == '/api/ports':
                ports = [dict(device=p.device, description=p.description, serial_number=p.serial_number)
                         for p in core.list_ports.comports()] if core.list_ports else []
                for port in ports:
                    for slot, profile in self.server.duo.device_profiles.items():
                        if port['serial_number'] == profile.get('serial_number'):
                            port.update(assigned_slot=slot, label=profile['label'])
                return self.send_json(dict(ports=ports, available=core.serial is not None,
                                           device_profiles=self.server.duo.device_profiles))
            if url.path == '/api/discover':
                return self.send_json(dict(devices=core.discover()))
            if url.path not in ('/', '/index.html', '/device.html', '/app.js', '/duo.js', '/styles.css', '/duo.css'):
                return self.send_error(HTTPStatus.NOT_FOUND)
            return core.SimpleHTTPRequestHandler.do_GET(self)
        except Exception as error:
            return self.send_json({'error': str(error)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        if not self.local_host() or self.headers.get('Origin', f'http://{self.headers.get("Host")}') != f'http://{self.headers.get("Host")}':
            return self.send_json({'error': 'Origin/Host rejected'}, HTTPStatus.FORBIDDEN)
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected JSON')
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 8192:
                raise ValueError('Invalid request size')
            payload = json.loads(self.rfile.read(length))
            duo = self.server.duo
            parts = self.path.strip('/').split('/')
            with duo.commands:
                if self.path == '/api/record/start':
                    duo.start_record(payload.get('note', ''), payload.get('folder_name', ''))
                elif self.path == '/api/record/stop':
                    duo.stop_record()
                elif len(parts) == 4 and parts[:2] == ['api', 'device'] and parts[2] in duo.devices:
                    slot, action = parts[2:]
                    if action == 'connect':
                        duo.connect(slot, payload)
                    elif action == 'reconnect':
                        device = duo.devices[slot]
                        if device.active:
                            raise ValueError('设备已连接，不需要重连')
                        config = dict(device.config)
                        if not config:
                            config = {'source': 'usb'}
                        if config.get('source', 'usb') == 'usb' and duo.device_profiles.get(slot, {}).get('serial_number'):
                            config.pop('port', None)
                        duo.connect(slot, config)
                    elif action == 'disconnect':
                        duo.disconnect(slot)
                    elif action == 'zero':
                        duo.devices[slot].zero_attitude()
                    else:
                        return self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    return self.send_error(HTTPStatus.NOT_FOUND)
            return self.send_json({'ok': True})
        except Exception as error:
            return self.send_json({'error': str(error)}, HTTPStatus.BAD_REQUEST)


def main():
    parser = argparse.ArgumentParser(description='ESP32-C3 Super Mini Duo v2')
    parser.add_argument('--port', type=int, default=5182)
    parser.add_argument('--data-dir', type=Path, default=DATA_DIR)
    parser.add_argument('--no-auto-connect', action='store_true', help='Leave both devices disconnected on startup')
    args = parser.parse_args()
    server = core.MonitorServer(('127.0.0.1', args.port), Handler)
    profiles_path = FRONTEND / 'device_profiles.json'
    profiles = json.loads(profiles_path.read_text(encoding='utf-8')) if profiles_path.exists() else {}
    server.duo = Duo(args.data_dir, profiles)
    print(f'Open http://127.0.0.1:{args.port}/\nData: {args.data_dir.resolve()}', flush=True)
    try:
        if not args.no_auto_connect:
            for slot, profile in profiles.items():
                try:
                    server.duo.connect(slot, {'source': 'usb'})
                    print(f"{profile['label']} 已连接。", flush=True)
                except Exception as error:
                    server.duo.devices[slot].error = str(error)
                    print(f"{profile['label']}: {error}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.duo.close()
        server.server_close()


if __name__ == '__main__':
    main()
