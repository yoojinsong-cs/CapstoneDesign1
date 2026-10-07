"""라벨 정확도에 영향을 주는 재조립/재전송/초기 관측 경계를 검증한다."""

import struct
import unittest

from build_dataset import Packet, body_observations, decode_exchange, feature_row, reassemble


def packet(frame, seq, payload):
    return Packet(frame, frame * 1000000, seq, 40 + len(payload), payload)


class DatasetTests(unittest.TestCase):
    def test_split_coalesced_metadata_and_reused_connection(self):
        first = struct.pack('<4I', 1, 4, 0, 0)
        second = struct.pack('<4I', 2, 3, 0, 0)
        close = struct.pack('<4I', 0, 100, 0, 0)
        requests = first + second + close
        response = first + b'abcd' + second + b'xyz' + close + bytes(100)
        # 메타데이터 분할, 여러 메시지 합침, out-of-order 및 재전송.
        client = [packet(1, 1, requests[:7]), packet(2, 8, requests[7:])]
        server = [packet(4, 24, response[23:]), packet(3, 1, response[:23]),
                  packet(5, 1, response[:23])]
        rows = decode_exchange(client, server, 'little')
        self.assertEqual([row['request_id'] for row in rows], [1, 2, 0])
        self.assertEqual([row['final_bytes'] for row in rows], [4, 3, 100])
        obs = body_observations(server, rows[0]['body_seq_start'], rows[0]['body_seq_end'])
        self.assertEqual([(p.frame, size) for p, size in obs], [(3, 4)])

    def test_gap_rejected(self):
        with self.assertRaisesRegex(ValueError, 'gap'):
            reassemble([packet(1, 1, b'ab'), packet(2, 4, b'cd')])

    def test_conflicting_retransmission_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            reassemble([packet(1, 1, b'abcd'), packet(2, 3, b'xx')])

    def test_mismatched_size_rejected(self):
        client = struct.pack('<4I', 1, 4, 0, 0)
        server = struct.pack('<4I', 1, 5, 0, 0) + b'abcde'
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            decode_exchange([packet(1, 1, client)], [packet(2, 1, server)], 'little')

    def test_arrival_order_overlap_and_no_future_features(self):
        packets = [packet(1, 20, b'defgh'), packet(2, 17, b'abcde'),
                   packet(3, 17, b'abcde'), packet(4, 25, b'ij')]
        obs = body_observations(packets, 17, 27)
        self.assertEqual([count for _, count in obs], [5, 3, 2])
        row = feature_row('sample', 1, obs)
        self.assertEqual(row['prefix_bytes'], 5)
        self.assertEqual(row['mask_2'], 0)
        self.assertEqual(row['body_bytes_2'], 0)
        row = feature_row('sample', 5, obs)
        self.assertEqual(row['observed_count'], 3)
        self.assertEqual(row['iat_ms_3'], 2)
        self.assertEqual(row['mask_4'], 0)
        self.assertNotIn('final_bytes', row)


if __name__ == '__main__':
    unittest.main()
