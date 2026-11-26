"""Run with: python3 -m unittest -v test_lpm_l2_visualizer.py"""
import ast
import json
import struct
import threading
import unittest
from urllib.request import urlopen

import lpm_l2_visualizer as viz


def worker_parser():
    """Extract only the worker's pure parse_frame() without starting sockets."""
    tree = ast.parse(viz.WORKER_CODE)
    target = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'parse_frame')
    mac = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'mac')
    code = compile(ast.Module(body=[mac, target], type_ignores=[]), '<test-parser>', 'exec')
    env = {'struct': struct}
    exec(code, env)
    return env['parse_frame']


def make_frame(src_ip, dst_ip, typ=8, seq=5):
    import socket
    eth = bytes.fromhex('0200000000020200000000010800')
    ip = bytes([0x45,0,0,28,0,0,0,0,64,1,0,0])+socket.inet_aton(src_ip)+socket.inet_aton(dst_ip)
    icmp = bytes([typ,0,0,0,0,1])+struct.pack('!H',seq)
    return eth+ip+icmp


class PacketParser(unittest.TestCase):
    def test_request(self):
        result = worker_parser()(make_frame('10.0.0.1','10.0.0.5',8,2))
        self.assertEqual((result['srcHost'],result['dstHost'],result['seq'],result['kind']), ('h1','h5',2,'request'))

    def test_reply(self):
        result = worker_parser()(make_frame('10.0.0.5','10.0.0.1',0,3))
        self.assertEqual((result['srcHost'],result['dstHost'],result['kind']), ('h5','h1','reply'))

    def test_irrelevant(self):
        parse = worker_parser()
        self.assertIsNone(parse(make_frame('10.0.0.1','10.0.0.19')))
        self.assertIsNone(parse(b'123'))


class HTTPService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hub = viz.Hub('demo', 'test')
        cls.hub.run_demo()
        cls.server = viz.ThreadingHTTPServer(('127.0.0.1',0),viz.WebHandler)
        cls.server.daemon_threads = True
        cls.server.hub = cls.hub
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:'+str(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.hub.shutdown()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_dashboard(self):
        with urlopen(self.url,timeout=3) as r:
            html=r.read().decode('utf-8')
        self.assertIn('LPM × L2 通信経路ビジュアライザ',html)
        self.assertIn('s1-eth4',html)
        self.assertIn('h1 ping -c 3 h2',html)

    def test_status(self):
        with urlopen(self.url+'/api/status',timeout=3) as r:
            d=json.load(r)
        self.assertEqual(d['expectedInterfaces'],15)
        self.assertEqual(d['mode'],'demo')

    def test_events(self):
        with urlopen(self.url+'/events',timeout=3) as r:
            line=r.readline().decode('utf-8')
        self.assertTrue(line.startswith('data: '))
        self.assertEqual(json.loads(line[6:])['event'],'status')


if __name__=='__main__':
    unittest.main()


class PlaybackOrdering(unittest.TestCase):
    def packet(self, kind, src, dst, fr, to, ts, seq=1, ident=55):
        return dict(event='packet', icmpId=ident, seq=seq, kind=kind,
                    srcHost=src, dstHost=dst, srcIp='10.0.0.'+src[1:],
                    dstIp='10.0.0.'+dst[1:], fromNode=fr,toNode=to,
                    iface='observed-interface',direction='out',ts=ts)

    def test_reply_cannot_begin_from_s1_when_h5_hop_is_observed_late(self):
        a=viz.PacketAssembler(quiet_seconds=0.5)
        # Simulate a socket poll that reads s1's reply BEFORE h5's actual reply.
        events=[
            self.packet('request','h1','h5','s1','s3',1.0),
            self.packet('reply','h5','h1','s1','s2',1.1),
            self.packet('reply','h5','h1','s3','s1',1.2),
            self.packet('request','h1','h5','h1','s2',1.3),
            self.packet('reply','h5','h1','h5','s3',1.4),
            self.packet('request','h1','h5','s3','h5',1.5),
            self.packet('request','h1','h5','s2','s1',1.6),
            self.packet('reply','h5','h1','s2','h1',1.7),
        ]
        for ev in events: a.add(ev,now=10)
        self.assertEqual(a.pop_ready(now=10.1),[])
        batches=a.pop_ready(now=10.6)
        self.assertEqual(len(batches),1)
        self.assertEqual([(e['fromNode'],e['toNode']) for e in batches[0]],
                         [('h1','s2'),('s2','s1'),('s1','s3'),('s3','h5'),
                          ('h5','s3'),('s3','s1'),('s1','s2'),('s2','h1')])
        self.assertTrue(batches[0][4]['firstInPhase'])
        self.assertTrue(batches[0][4]['originObserved'])
        self.assertFalse(batches[0][4]['missingOrigin'])

    def test_missing_h5_link_is_reported_not_invented(self):
        events=[self.packet('reply','h5','h1','s1','s2',1.0),
                self.packet('reply','h5','h1','s3','s1',2.0)]
        ordered=viz.ordered_observations(events)
        self.assertEqual([(e['fromNode'],e['toNode']) for e in ordered],
                         [('s3','s1'),('s1','s2')])
        self.assertEqual(len(ordered),2)
        self.assertTrue(ordered[0]['missingOrigin'])
        self.assertEqual(ordered[0]['srcHost'],'h5')

    def test_unknown_unicast_flood_return_follows_forward(self):
        events=[self.packet('request','h1','h2','s1','s2',1.0),
                self.packet('request','h1','h2','s2','s1',1.1),
                self.packet('request','h1','h2','s2','h2',1.2),
                self.packet('request','h1','h2','h1','s2',1.3)]
        ordered=viz.ordered_observations(events)
        self.assertEqual(ordered[0]['fromNode'],'h1')
        self.assertGreater([e['fromNode'] for e in ordered].index('s1'),
                           [e['fromNode'] for e in ordered].index('s2'))
        self.assertEqual(len(ordered),4)

    def test_identifier_distinguishes_interleaved_pings(self):
        a=viz.PacketAssembler(quiet_seconds=0.3)
        a.add(self.packet('reply','h5','h1','s1','s2',1,ident=10),now=100)
        a.add(self.packet('reply','h5','h1','s1','s2',1,ident=11),now=100)
        batches=a.pop_ready(now=100.4)
        self.assertEqual(len(batches),2)
        self.assertEqual({b[0]['icmpId'] for b in batches},{10,11})

class IntraL2Routing(unittest.TestCase):
    def packet(self, kind, src, dst, fr, to, ts, **kwargs):
        return dict(event='packet',kind=kind,srcHost=src,dstHost=dst,
                    srcIp='10.0.0.'+src[1:],dstIp='10.0.0.'+dst[1:],
                    fromNode=fr,toNode=to,seq=1,icmpId=22,ts=ts,**kwargs)

    def test_direct_h1_to_h2_never_makes_s1_a_main_hop(self):
        edges=[('h1','s2'),('s2','h2'),('h2','s2'),('s2','h1')]
        events=[self.packet('request' if i<2 else 'reply',
                            'h1' if i<2 else 'h2', 'h2' if i<2 else 'h1',
                            a,b,10+i) for i,(a,b) in enumerate(edges)]
        out=viz.ordered_observations(events)
        self.assertEqual([(e['fromNode'],e['toNode']) for e in out], edges)
        self.assertTrue(all(e['routeRole']=='main' for e in out))
        self.assertFalse(any(e['fromNode']=='s1' for e in out))

    def test_flood_branches_are_marked_and_never_fabricated(self):
        # Intentionally shuffled polling order.
        edges=[('s1','s2'),('s2','h3'),('s2','s1'),('s2','h2'),('h1','s2')]
        out=viz.ordered_observations([
            self.packet('request','h1','h2',a,b,i) for i,(a,b) in enumerate(edges)])
        self.assertEqual(len(out),5)
        self.assertEqual([e['routeRole'] for e in out],
                         ['main','main','branch','branch','branch'])
        self.assertEqual([(e['fromNode'],e['toNode']) for e in out[:2]],
                         [('h1','s2'),('s2','h2')])
        self.assertIn(('s1','s2'),[(e['fromNode'],e['toNode']) for e in out[2:]])
        self.assertIn('返送',next(e['pathDescription'] for e in out if e['fromNode']=='s1'))
        self.assertTrue(all(e['orderBasis']=='topology' for e in out))

    def test_kernel_timestamps_preserve_revisited_bridge_links(self):
        # The second s2 -> h2 is a *different copy*, after s1 -> s2;
        # sorting solely on distance would incorrectly move it earlier.
        edges=[('s2','h2',4),('s1','s2',3),('s2','s1',2),
               ('s2','h2',1.5),('h1','s2',1)]
        out=viz.ordered_observations([
            self.packet('request','h1','h2',a,b,t,captureClock='kernel')
            for a,b,t in edges])
        self.assertEqual([(e['fromNode'],e['toNode'],e['ts']) for e in out],
                         [('h1','s2',1),('s2','h2',1.5),('s2','s1',2),
                          ('s1','s2',3),('s2','h2',4)])
        self.assertTrue(all(e['orderBasis']=='kernel' for e in out))

    def test_only_observed_links_ever_play(self):
        observed=[self.packet('request','h1','h2','s2','h2',1)]
        out=viz.ordered_observations(observed)
        self.assertEqual(len(out),1)
        self.assertTrue(out[0]['missingOrigin'])
        self.assertEqual((out[0]['fromNode'],out[0]['toNode']),('s2','h2'))

class RepeatedMainCopies(unittest.TestCase):
    def test_repeat_main_edge_is_flagged_for_condensed_mode(self):
        e=dict(kind='request',srcHost='h1',dstHost='h2',icmpId=1,seq=1,ts=1.0,captureClock='kernel')
        events=[dict(e,fromNode=fr,toNode=to,ts=ts) for fr,to,ts in
                [('h1','s2',1),('s2','h2',2),('s2','s1',3),
                 ('s1','s2',4),('s2','h2',5)]]
        result=viz.ordered_observations(events)
        self.assertFalse(result[1]['repeatOnMainPath'])
        self.assertTrue(result[-1]['repeatOnMainPath'])
        self.assertEqual(len([x for x in result if x['routeRole']=='main' and not x['repeatOnMainPath']]),2)


class MultiPingMonitoring(unittest.TestCase):
    def test_three_sequences_keep_independent_replies_and_flood_copies(self):
        a = viz.PacketAssembler(quiet_seconds=0.7)
        def pkt(kind,seq,fr,to):
            src,dst=('h1','h2') if kind=='request' else ('h2','h1')
            return dict(kind=kind,seq=seq,icmpId=620,srcHost=src,dstHost=dst,
                        fromNode=fr,toNode=to,ts=float(seq),captureClock='poll')
        for seq in (1,2,3):
            edges=[('h1','s2'),('s2','h2')]
            if seq==1:
                edges += [('s2','h3'),('s2','s1'),('s1','s2')]
            for x,y in edges: a.add(pkt('request',seq,x,y),now=seq)
            for x,y in [('h2','s2'),('s2','h1')]:
                a.add(pkt('reply',seq,x,y),now=seq)
        grouped=a.pop_ready(now=6)
        self.assertEqual(len(grouped),3)
        for seq,events in enumerate(grouped,1):
            self.assertEqual({e['seq'] for e in events},{seq})
            self.assertEqual([(e['fromNode'],e['toNode']) for e in events if e['kind']=='reply'],
                             [('h2','s2'),('s2','h1')])
        self.assertEqual(len([e for e in grouped[0] if e['routeRole']=='branch']),3)
        self.assertTrue(all(e['routeRole']=='main' for g in grouped[1:] for e in g))

    def test_capture_worker_is_valid_python_and_backgrounds_fdb(self):
        compile(viz.WORKER_CODE,'<packet-worker>','exec')
        self.assertIn('threading.Thread(target=poll_fdb, daemon=True)',viz.WORKER_CODE)
        self.assertIn('for _ in range(128):',viz.WORKER_CODE)

class Diagnostics(unittest.TestCase):
    def test_snapshot_preserves_raw_observations_without_synthesizing_reply(self):
        h=viz.Hub('live','test')
        item=dict(event='packet',kind='request',seq=17,icmpId=111,
                  fromNode='h1',toNode='s2',srcHost='h1',dstHost='h2',
                  srcMac='02:00:00:00:00:01',dstMac='02:00:00:00:00:02',
                  ts=123.5,iface='s2-eth2',direction='in',captureClock='kernel')
        with h.lock:
            h.raw_packets.append(dict(item,receivedAt=124.1))
            h.latest_fdb={'s2':[{'mac':item['srcMac'],'port':'s2-eth1'}]}
        snapshot=h.diagnostics()
        self.assertEqual(snapshot['rawPackets'][0]['kind'],'request')
        self.assertEqual(len(snapshot['rawPackets']),1)
        self.assertEqual(snapshot['fdb']['s2'][0]['port'],'s2-eth1')
        self.assertEqual(snapshot['format'],'lpm-l2-capture-v1')
