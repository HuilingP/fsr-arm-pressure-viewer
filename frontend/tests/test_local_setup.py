"""Protect the physical left/right assignment across USB port changes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Duo, FRONTEND


class LocalMappingTests(unittest.TestCase):
    def setUp(self):
        self.profiles = json.loads((FRONTEND / 'device_profiles.example.json').read_text())
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.duo = Duo(self.directory.name, device_profiles=self.profiles)
        self.ports = [SimpleNamespace(device=f'/dev/cu.changed-{slot}', serial_number=p['serial_number'])
                      for slot, p in self.profiles.items()]

    def tearDown(self):
        self.duo.close()

    def test_identity_survives_port_change_and_is_saved_with_connection(self):
        with patch('core.list_ports.comports', return_value=self.ports):
            with patch.object(self.duo.devices['A'], 'start') as start:
                self.duo.connect('A', {'source': 'usb'})
        config = start.call_args.args[0]
        self.assertEqual(config['port'], '/dev/cu.changed-A')
        self.assertEqual(config['device_profile']['side'], 'left')
        self.assertEqual(config['serial_number'], self.profiles['A']['serial_number'])

    def test_wrong_side_usb_is_rejected_before_opening(self):
        with patch('core.list_ports.comports', return_value=self.ports):
            with patch.object(self.duo.devices['A'], 'start') as start:
                with self.assertRaisesRegex(ValueError, '串口不匹配'):
                    self.duo.connect('A', {'source': 'usb', 'port': '/dev/cu.changed-B'})
                start.assert_not_called()

    def test_missing_board_never_uses_the_other_board(self):
        with patch('core.list_ports.comports', return_value=self.ports[1:]):
            with self.assertRaisesRegex(ValueError, '未找到'):
                self.duo.connect('A', {'source': 'usb'})

    def test_firmware_identity_must_match_the_assigned_side(self):
        self.duo.claim_board('A', self.profiles['A']['board_id'])
        with self.assertRaisesRegex(ValueError, '设备编号不符'):
            self.duo.claim_board('B', self.profiles['A']['board_id'])

    def test_session_and_device_metadata_keep_side_labels(self):
        self.duo.connect('A', {'source': 'demo'})
        self.duo.start_record(folder_name='mapping-test')
        self.duo.stop_record()
        session = json.loads((Path(self.directory.name) / 'mapping-test/session.json').read_text())
        device = json.loads((Path(self.directory.name) / 'mapping-test/A/segment_001/metadata.json').read_text())
        self.assertEqual(session['device_profiles']['B']['side'], 'right')
        self.assertEqual(device['connection']['device_profile']['color'], 'black')


if __name__ == '__main__':
    unittest.main()
