import struct
import unittest

from export_packet_headers import parse_metadata, sequence_in_response


class CaptureMetadataTests(unittest.TestCase):
    def test_old_and_new_captures_keep_their_actual_header_size(self):
        for values in ((7, 100, 0, 0), (7, 100, 0, 0, 2)):
            raw = struct.pack('<' + 'I' * len(values), *values)
            flow = parse_metadata(raw.hex())
            self.assertEqual((flow['flow_id'], flow['flow_size']), (7, 100))
            self.assertEqual(flow['metadata_size'], len(raw))
            flow['response_start'] = 0xfffffff0
            end = (flow['response_start'] + len(raw) + 100) % (1 << 32)
            self.assertTrue(sequence_in_response((end - 1) % (1 << 32), flow))
            self.assertFalse(sequence_in_response(end, flow))
            self.assertTrue(sequence_in_response(end, flow, include_end=True))

    def test_partial_or_invalid_metadata_is_not_a_request(self):
        for payload in ('', 'not hex', '00' * 15, '00' * 19, '00' * 21):
            self.assertIsNone(parse_metadata(payload))
