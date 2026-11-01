import csv
import http.client
import json
import math
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from protocol import Parser, decode_frame, encode_demo, EXTRA_FIELDS
from server import Device, Duo, Handler


def v4(sample=1, board=123):
    frame = bytearray(344)
    frame[:140] = encode_demo(sample)[:140]
    frame[2] = 4
    struct.pack_into('<H', frame, 4, 334)
    struct.pack_into('<Q3I', frame, 140, board, 2, 3, 4)
    frame[160:162] = bytes([7, 7])
    for i in range(3):
        struct.pack_into('<4h2I4B', frame, 164+20*i, -123, 456, -789, 42,
                         sample*10000-20, sample*10000-10, 3, sample & 255, 1, 0)
    for ch in range(8):
        values = [1000+ch, 100+ch, 102+ch, 104+ch, 106+ch]
        struct.pack_into('<5H', frame, 224+ch*10, *values)
        struct.pack_into('<H', frame, 108+ch*2, 103+ch)
        struct.pack_into('<I', frame, 304+ch*4, sample*10000+ch*100)
    struct.pack_into('<I', frame, 336, sample*7)
    return crc(frame)


def crc(frame):
    struct.pack_into('<I', frame, len(frame)-4, zlib.crc32(frame[2:-4]) & 0xffffffff)
    return bytes(frame)


def event(number=1, board=123):
    frame = bytearray(56)
    struct.pack_into('<2sBBHIIIQBB', frame, 0, b'\xa5\x5a', 5, 0, 46,
                     number, 1, number*10000, 2**40+number, 16, 0x15)
    struct.pack_into('<4B4hI', frame, 28, 0x15, number & 255, 3, 2, -1, 2, -3, 42, 987654)
    struct.pack_into('<Q', frame, 44, board)
    return crc(frame)


def raw(sample=1):
    return decode_frame(encode_demo(sample))


def wait_for(predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Timed out waiting for condition')


class ProtocolTests(unittest.TestCase):
    def test_discovery_accepts_c3_duo_tag(self):
        class FakeDiscoverySocket:
            def __init__(self):
                self.received = False
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def setsockopt(self, *_): pass
            def bind(self, *_): pass
            def sendto(self, *_): pass
            def settimeout(self, *_): pass
            def recvfrom(self, _):
                if self.received:
                    raise socket.timeout()
                self.received = True
                return (b'RIGIDITY_ESP32_V2,10.0.0.22,9000,SUPER_MINI_DUO_RAW_V4',
                        ('10.0.0.22', 9001))
        with patch.object(core.socket, 'socket', return_value=FakeDiscoverySocket()):
            self.assertEqual(core.discover(.1), [dict(
                host='10.0.0.22', port=9000, firmware='SUPER_MINI_DUO_RAW_V4')])

    def test_mixed_versions_fragmentation_crc_and_raw_fields(self):
        stream = encode_demo(1)+event()+v4(2)
        parser = Parser()
        output=[]
        for offset in range(0, len(stream), 7):
            output.extend(parser.push(stream[offset:offset+7]))
        self.assertEqual([item['kind'] for item in output], ['sample','event','sample'])
        self.assertEqual(output[1]['timestamp_us'], 2**40+1)
        self.assertEqual(output[1]['temperature_counts'],42)
        extra=output[2]['extra']
        self.assertEqual(extra['raw_accel_x_counts'],-123)
        self.assertEqual(extra['raw_gyro_temperature_counts'],42)
        self.assertEqual(extra['fsr7_adc0'],1007)
        self.assertEqual(output[2]['fsr_raw'][7],110)
        self.assertAlmostEqual(extra['raw_mag_age_ms'],.01)
        self.assertTrue(all(key in extra for key in EXTRA_FIELDS))
        self.assertEqual(extra['event_queue_dropped'],4)

    def test_resync_lost_byte_and_corrupt_event(self):
        bad=bytearray(event()); bad[30]^=0xff
        parser=Parser()
        result=parser.push(v4(1)[:88]+bytes(bad)+v4(2))
        self.assertEqual([item['sample_id'] for item in result], [2])
        self.assertGreater(parser.rejected,0)

    def test_missing_raw_is_nan_not_zero(self):
        frame=bytearray(v4()); frame[161]=0
        extra=decode_frame(crc(frame))['extra']
        self.assertTrue(math.isnan(extra['raw_accel_x_counts']))
        self.assertTrue(math.isnan(decode_frame(encode_demo(1))['extra']['fsr0_adc0']))

    def test_v4_disabled_raw_reports_keep_fsr_and_drop_counters(self):
        frame = bytearray(v4())
        frame[160:162] = b'\x00\x00'
        extra = decode_frame(crc(frame))['extra']
        self.assertEqual(extra['raw_config_mask'], 0)
        self.assertTrue(math.isnan(extra['raw_accel_x_counts']))
        self.assertEqual(extra['fsr7_adc0'], 1007)
        self.assertEqual(extra['wifi_packets_dropped'], 3)


class VelocityTests(unittest.TestCase):
    def sample(self, i, acceleration=1.0):
        item=raw(i)
        item.update(statuses=[0]*5, linear=[acceleration,0,0], mag=[260,0,0], quaternion=[1,0,0,0])
        return item

    def test_integrates_with_low_accuracy_before_calibration(self):
        processor=core.MotionProcessor()
        for i in range(1,102):
            values,_=processor.process(self.sample(i),i*10000)
        self.assertAlmostEqual(values['vx_m_s'],1.0,places=6)
        self.assertEqual(values['velocity_reliable'],0)
        self.assertEqual(values['velocity_preview'],1)
        self.assertFalse(processor.calibrated)

    def test_continues_past_old_ten_second_cutoff(self):
        processor=core.MotionProcessor()
        for i in range(1,1202):
            values,_=processor.process(self.sample(i),i*10000)
        self.assertAlmostEqual(values['vx_m_s'],12.0,places=6)

    def test_missing_data_pauses_gap_restarts_segment(self):
        p=core.MotionProcessor()
        p.process(self.sample(1),10000)
        values,_=p.process(self.sample(2),20000)
        segment=values['velocity_segment']
        missing=self.sample(3); missing['ages_ms'][3]=65535
        values,_=p.process(missing,30000)
        self.assertTrue(math.isnan(values['vx_m_s']))
        values,_=p.process(self.sample(5),50000)
        self.assertEqual(values['vx_m_s'],0)
        self.assertGreater(values['velocity_segment'],segment)

    def test_zero_angle_does_not_reset_velocity_or_rotate_reference(self):
        p=core.MotionProcessor()
        p.process(self.sample(1),10000)
        p.process(self.sample(2),20000)
        p.set_display_reference([math.cos(.5),0,0,math.sin(.5)])
        values,_=p.process(self.sample(3),30000)
        self.assertAlmostEqual(values['vx_m_s'],.02)
        self.assertEqual(p.integration_reference,[1,0,0,0])

    def test_repeated_linear_report_not_integrated_twice(self):
        p=core.MotionProcessor()
        p.process(self.sample(1),10000)
        repeat=self.sample(2); repeat['sequences'][3]=1
        values,_=p.process(repeat,20000)
        self.assertEqual(values['vx_m_s'],0)
        values,_=p.process(self.sample(3),30000)
        self.assertAlmostEqual(values['vx_m_s'],.02)

    def test_calibrated_stationary_zero_and_sensor_reset(self):
        p=core.MotionProcessor()
        for i in range(1,260):
            values,_=p.process(raw(i),i*10000)
        self.assertTrue(p.calibrated)
        self.assertEqual(values['vx_m_s'],0)
        self.assertEqual(values['velocity_reliable'],1)
        item=self.sample(260); item['reset_count']+=1
        values,_=p.process(item,2600000)
        self.assertFalse(p.calibrated)
        self.assertEqual(values['vx_m_s'],0)

    def test_bias_zero_succeeds_without_magnetic_or_accuracy_grade(self):
        p = core.MotionProcessor()
        for i in range(1, core.CALIBRATION_SAMPLES + 2):
            item = raw(i)
            item.update(statuses=[0] * 5, mag=[math.nan] * 3,
                        heading_accuracy=math.nan)
            values, _ = p.process(item, i * 10000)
        self.assertTrue(p.calibrated)
        self.assertEqual(values['calibration_complete'], 1)
        self.assertEqual(p.gyro_bias, [0.0] * 3)
        self.assertEqual(p.linear_bias, [0.0] * 3)
        self.assertIsNone(p.magnetic_reference)
        self.assertEqual(values['magnetic_reliable'], 0)

    def test_bias_zero_requires_still_fresh_reports(self):
        p = core.MotionProcessor()
        for i in range(1, core.CALIBRATION_SAMPLES + 2):
            item = raw(i)
            item['gyro'] = [0.1, 0.0, 0.0]
            p.process(item, i * 10000)
        self.assertFalse(p.calibrated)
        self.assertEqual(p.calibration_count, 0)
        for i in range(core.CALIBRATION_SAMPLES + 2, 2 * core.CALIBRATION_SAMPLES + 3):
            item = raw(i)
            item['ages_ms'][1] = 100
            p.process(item, i * 10000)
        self.assertFalse(p.calibrated)


class FakeSerial:
    """Serial transport with fragmented raw events + samples and cable removal."""
    def __init__(self, port, baud, timeout):
        self.board = int(port.removeprefix('COM'))
        self.index=0; self.closed=False; self.unplugged=False
    @property
    def in_waiting(self):
        return 8192
    def read(self, size):
        time.sleep(.006)
        if self.unplugged:
            raise OSError('USB cable removed')
        if self.closed:
            return b''
        self.index+=1
        return event(self.index,self.board)+v4(self.index,self.board)
    def close(self):
        self.closed=True


class TcpBoard:
    def __init__(self, board):
        self.board=board
        self.listener=socket.socket()
        self.listener.bind(('127.0.0.1',0)); self.listener.listen()
        self.port=self.listener.getsockname()[1]
        self.stop=threading.Event()
        self.client=None
        self.thread=threading.Thread(target=self.run,daemon=True); self.thread.start()
    def run(self):
        try:
            self.listener.settimeout(3)
            self.client,_=self.listener.accept()
            sample=0
            while not self.stop.wait(.01):
                sample+=1
                data=event(sample,self.board)+v4(sample,self.board)
                self.client.sendall(data[:17]); self.client.sendall(data[17:])
        except OSError:
            pass
    def close(self):
        self.stop.set()
        if self.client:
            try: self.client.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.client.close()
        self.listener.close()
        self.thread.join(4)


class DuoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.duo=Duo(self.temp.name)
    def tearDown(self):
        self.duo.close(); self.temp.cleanup()
    def connect_demo(self, slot):
        self.duo.connect(slot,{'source':'demo'})
        wait_for(lambda:self.duo.devices[slot].index>=3)
    def test_two_usb_one_button_disconnect_and_reconnect(self):
        with patch.object(core,'serial',SimpleNamespace(Serial=FakeSerial)):
            for slot,port in [('A','COM11'),('B','COM12')]:
                self.duo.connect(slot,{'source':'usb','port':port})
            wait_for(lambda:all(d.index>=4 for d in self.duo.devices.values()))
            self.assertEqual(list(Path(self.temp.name).iterdir()),[])
            self.duo.start_record('notes','duo-usb')
            wait_for(lambda:all(d.recorded>=5 for d in self.duo.devices.values()))
            self.duo.devices['A'].connection.unplugged=True
            wait_for(lambda:not self.duo.devices['A'].active)
            b_before=self.duo.devices['B'].recorded
            wait_for(lambda:self.duo.devices['B'].recorded>=b_before+5)
            self.assertTrue(self.duo.recording)
            self.assertIn('USB cable',self.duo.devices['A'].error)
            self.duo.connect('A',{'source':'usb','port':'COM11'})
            wait_for(lambda:self.duo.devices['A'].recorded>=4)
            self.duo.stop_record()
        folder=Path(self.temp.name)/'duo-usb'
        self.assertTrue((folder/'A/segment_001/samples.csv').exists())
        self.assertTrue((folder/'A/segment_002/samples.csv').exists())
        self.assertTrue((folder/'B/segment_001/samples.csv').exists())
        for segment in (folder/'A/segment_001',folder/'B/segment_001'):
            with (segment/'samples.csv').open() as handle: rows=list(csv.DictReader(handle))
            self.assertTrue(all(row['device_slot']==segment.parent.name for row in rows))
            self.assertEqual(rows[-1]['raw_complete'],'1')
            self.assertEqual(rows[-1]['raw_accel_x_counts'],'-123')
            wire=Parser().push((segment/'stream.bin').read_bytes())
            self.assertEqual(sum(x['kind']=='sample' for x in wire),len(rows))
            states=[json.loads(line) for line in (segment/'processor_state.jsonl').read_text().splitlines()]
            self.assertEqual(len(states),len(rows))
            self.assertIn('gyro_bias',states[-1]['after'])
            self.assertIn('integration_reference',states[-1]['after'])
            self.assertTrue((segment/'summary.json').exists())

    def test_two_wifi_and_remote_eof_do_not_stop_other(self):
        a,b=TcpBoard(501),TcpBoard(502)
        try:
            for slot,board in [('A',a),('B',b)]:
                self.duo.connect(slot,{'source':'wifi','host':'127.0.0.1','tcp_port':board.port})
            wait_for(lambda:all(d.index>=2 for d in self.duo.devices.values()))
            self.duo.start_record(folder_name='wifi')
            wait_for(lambda:all(d.recorded>=3 for d in self.duo.devices.values()))
            a.close()
            wait_for(lambda:not self.duo.devices['A'].active)
            count=self.duo.devices['B'].recorded
            wait_for(lambda:self.duo.devices['B'].recorded>count+3)
            self.assertTrue(self.duo.devices['B'].snapshot()['recording'])
        finally:
            a.close(); b.close()

    def test_mixed_usb_wifi(self):
        board=TcpBoard(301)
        try:
            with patch.object(core,'serial',SimpleNamespace(Serial=FakeSerial)):
                self.duo.connect('A',{'source':'usb','port':'COM21'})
                self.duo.connect('B',{'source':'wifi','host':'127.0.0.1','tcp_port':board.port})
                wait_for(lambda:all(d.index>=2 for d in self.duo.devices.values()))
                self.duo.start_record(folder_name='mixed')
                wait_for(lambda:all(d.recorded>=3 for d in self.duo.devices.values()))
        finally: board.close()

    def test_single_device_then_second_joins_active_recording(self):
        self.connect_demo('B')
        self.duo.start_record(folder_name='single')
        wait_for(lambda:self.duo.devices['B'].recorded>=3)
        self.assertFalse(self.duo.devices['A'].record_file)
        self.connect_demo('A')
        wait_for(lambda:self.duo.devices['A'].recorded>=3)
        self.duo.disconnect('B')
        before=self.duo.devices['A'].recorded
        wait_for(lambda:self.duo.devices['A'].recorded>before)

    def test_duplicate_endpoint_and_physical_device_rejected(self):
        with patch.object(core,'serial',SimpleNamespace(Serial=FakeSerial)):
            self.duo.connect('A',{'source':'usb','port':'COM11'})
            wait_for(lambda:self.duo.devices['A'].board_id)
            with self.assertRaisesRegex(ValueError,'already assigned'):
                self.duo.connect('B',{'source':'usb','port':'com11'})
            with self.assertRaisesRegex(ValueError,'physical ESP32'):
                self.duo.claim_board('B',self.duo.devices['A'].board_id)
            self.assertTrue(self.duo.devices['A'].active)

    def test_cannot_overwrite_session_or_traverse_path(self):
        self.connect_demo('A')
        with self.assertRaises(ValueError): self.duo.start_record(folder_name='../escape')
        self.duo.start_record(folder_name='trial'); self.duo.stop_record()
        with self.assertRaises(FileExistsError): self.duo.start_record(folder_name='trial')

    def test_event_loss_is_counted(self):
        d=self.duo.devices['A']
        for number in (1,2,5): d._accept_event(Parser().push(event(number))[0])
        self.assertEqual(d.event_missing,2)

    def test_sample_wrap_reboot_duplicate_and_raw_counter_preservation(self):
        d=self.duo.devices['A']
        first=decode_frame(v4(100)); first['raw_time_us']=0xfffffff0
        d._accept(first)
        second=decode_frame(v4(101)); second['raw_time_us']=0x20
        d._accept(second)
        self.assertEqual(d.rows[-1]['device_dt_us'],48)
        self.assertEqual(d.rows[-1]['boot_id'],1)
        d._accept(second)
        self.assertEqual(d.duplicates,1)
        reboot=decode_frame(v4(1)); reboot['reset_count']=0
        d._accept(reboot)
        self.assertEqual(d.rows[-1]['boot_id'],2)
        self.assertEqual(d.rows[-1]['velocity_segment'],1)
        self.assertEqual(d.rows[-1]['device_time_raw_us'],10000)

    def test_record_start_rolls_back_both_files_on_second_device_failure(self):
        self.connect_demo('A'); self.connect_demo('B')
        with patch.object(self.duo.devices['B'],'start_record',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):
                self.duo.start_record(folder_name='start-failure')
        self.assertFalse(self.duo.recording)
        self.assertTrue(all(not d.record_file for d in self.duo.devices.values()))
        self.assertTrue(all(d.active for d in self.duo.devices.values()))

    def test_full_ring_does_not_truncate_disk_recording(self):
        from collections import deque
        d=self.duo.devices['A']
        d.rows=deque(maxlen=3)
        self.connect_demo('A')
        self.duo.start_record(folder_name='ring')
        wait_for(lambda:d.recorded>=10)
        self.duo.stop_record()
        with Path(d.record_path).open() as handle: rows=list(csv.DictReader(handle))
        self.assertGreaterEqual(len(rows),10)
        self.assertEqual(len(d.rows),3)

    def test_disk_failure_isolated_from_other_device(self):
        self.connect_demo('A'); self.connect_demo('B')
        self.duo.start_record(folder_name='failure')
        wait_for(lambda:all(d.recorded>=3 for d in self.duo.devices.values()))
        with self.duo.devices['A'].lock:
            self.duo.devices['A'].extra_files['wire'].close()
        wait_for(lambda:bool(self.duo.devices['A'].record_error))
        count=self.duo.devices['B'].recorded
        wait_for(lambda:self.duo.devices['B'].recorded>count+3)
        self.assertTrue(self.duo.devices['A'].active)


class HttpTests(unittest.TestCase):
    def test_device_routes_static_ui_and_record_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            server=core.MonitorServer(('127.0.0.1',0),Handler)
            server.duo=Duo(directory)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=http.client.HTTPConnection(*server.server_address)
            def request(method,path,data=None,extra=None):
                body=json.dumps(data) if data is not None else None
                client.request(method,path,body,{'Content-Type':'application/json',**(extra or {})})
                response=client.getresponse();content=response.read()
                return response.status,content
            try:
                for path in ('/','/device.html?device=B','/app.js','/duo.js','/duo.css'):
                    self.assertEqual(request('GET',path)[0],200)
                self.assertEqual(request('GET','/core.py')[0],404)
                self.assertEqual(request('POST','/api/device/B/connect',{'source':'demo'})[0],200)
                wait_for(lambda:server.duo.devices['B'].index>1)
                status=json.loads(request('GET','/api/device/B/status?after=0')[1])
                self.assertEqual(status['device_slot'],'B')
                self.assertEqual(request('POST','/api/record/start',{'folder_name':'http'})[0],200)
                self.assertEqual(request('GET','/api/download?name=http')[0],400)
                self.assertEqual(request('POST','/api/device/B/disconnect',{})[0],200)
                self.assertEqual(request('POST','/api/device/B/reconnect',{})[0],200)
                wait_for(lambda:server.duo.devices['B'].recorded>1)
                self.assertEqual(server.duo.segments['B'],2)
                self.assertEqual(request('POST','/api/record/stop',{})[0],200)
                self.assertEqual(json.loads(request('GET','/api/recordings')[1])['recordings'][0]['name'],'http')
                code,archive=request('GET','/api/download?name=http')
                self.assertEqual(code,200)
                import zipfile,io
                with zipfile.ZipFile(io.BytesIO(archive)) as z:
                    self.assertIn('http/B/segment_002/samples.csv',z.namelist())
                    self.assertIn('http/session.json',z.namelist())
                self.assertEqual(request('GET','/api/download?name=../http')[0],400)
                self.assertEqual(request('POST','/api/device/B/disconnect',{}, {'Origin':'http://evil.example'})[0],403)
                self.assertEqual(request('GET','/api/status',extra={'Host':'evil.example'})[0],403)
                self.assertTrue(server.duo.devices['B'].active)
            finally:
                client.close();server.shutdown();server.duo.close();server.server_close();thread.join()


if __name__ == '__main__': unittest.main()
