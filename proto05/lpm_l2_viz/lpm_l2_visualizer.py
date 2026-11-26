#!/usr/bin/env python3
"""Tutorial 5 LPM + learning Linux bridges: live packet visualizer.

Runs on the Mac, capturing ICMP from Mininet switch interfaces by running a
stdlib-only AF_PACKET worker inside the existing privileged Docker container.
No changes to P4 or Mininet topology, no tcpdump/pip installation, no added
Docker port publication. The HTTP dashboard stays on 127.0.0.1 by default.
"""

import argparse
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

BASE = Path(__file__).resolve().parent
DASHBOARD = BASE / 'dashboard.html'
DEFAULT_CONTAINER = 'p4mn-lpm-l2'

# Executed with `docker exec CONTAINER python3 -u -c WORKER_CODE`.
# These are exactly one observed switch-side interface per physical Mininet link.
# Capturing both sides of the s1--s2 link would duplicate each packet.
WORKER_CODE = r'''
import json
import re
import selectors
import socket
import struct
import subprocess
import sys
import time
import threading

LINKS = {
    's1-eth1': ('s1', 's2'),
    's1-eth2': ('s1', 's3'),
    's1-eth3': ('s1', 's4'),
    's1-eth4': ('s1', 'h12'),
    's2-eth2': ('s2', 'h1'),
    's2-eth3': ('s2', 'h2'),
    's2-eth4': ('s2', 'h3'),
    's3-eth2': ('s3', 'h4'),
    's3-eth3': ('s3', 'h5'),
    's3-eth4': ('s3', 'h6'),
    's3-eth5': ('s3', 'h7'),
    's4-eth2': ('s4', 'h8'),
    's4-eth3': ('s4', 'h9'),
    's4-eth4': ('s4', 'h10'),
    's4-eth5': ('s4', 'h11'),
}

send_lock = threading.Lock()
def send(obj):
    with send_lock:
        print(json.dumps(obj, ensure_ascii=False, separators=(',', ':')), flush=True)

def mac(raw):
    return ':'.join('%02x' % b for b in raw)

def parse_frame(frame):
    if len(frame) < 34:
        return None
    ethertype = struct.unpack('!H', frame[12:14])[0]
    off = 14
    if ethertype in (0x8100, 0x88a8):
        if len(frame) < 38:
            return None
        ethertype = struct.unpack('!H', frame[16:18])[0]
        off = 18
    if ethertype != 0x0800 or len(frame) < off + 20:
        return None
    ihl = (frame[off] & 15) * 4
    if ihl < 20 or len(frame) < off + ihl + 8 or frame[off+9] != 1:
        return None
    src = '.'.join(str(x) for x in frame[off+12:off+16])
    dst = '.'.join(str(x) for x in frame[off+16:off+20])
    if not src.startswith('10.0.0.') or not dst.startswith('10.0.0.'):
        return None
    try:
        s, d = int(src.rsplit('.', 1)[1]), int(dst.rsplit('.', 1)[1])
    except ValueError:
        return None
    if not (1 <= s <= 12 and 1 <= d <= 12):
        return None
    icmp_offset = off + ihl
    t = frame[icmp_offset]
    if t not in (0, 8):
        return None
    ident, seq = struct.unpack('!HH', frame[icmp_offset+4:icmp_offset+8])
    return dict(srcHost='h'+str(s), dstHost='h'+str(d), srcIp=src, dstIp=dst,
                kind='reply' if t == 0 else 'request', seq=seq, icmpId=ident,
                srcMac=mac(frame[6:12]), dstMac=mac(frame[0:6]))

def get_fdb():
    result = {}
    for sw in ('s2', 's3', 's4'):
        try:
            p = subprocess.run(['bridge', 'fdb', 'show', 'br', sw],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=2)
            if p.returncode:
                return None
            items = []
            for ln in p.stdout.splitlines():
                m = re.match(r'^([0-9a-f:]{17})\s+dev\s+('+sw+r'-eth\d+)\b', ln, re.I)
                if not m or any(k in ln.split() for k in ('self', 'permanent', 'static')):
                    continue
                items.append(dict(mac=m.group(1), port=m.group(2)))
            result[sw] = sorted(items, key=lambda v: (v['port'],v['mac']))
        except (OSError, subprocess.TimeoutExpired):
            return None
    return result

sel = selectors.DefaultSelector()
ready, failed = [], []
# Linux SO_TIMESTAMPNS provides the packet's kernel capture time rather than
# the time this worker happens to poll the interface's socket. This is
# important for unknown-unicast flooding, where a frame may go s2 -> s1 -> s2.
SO_TIMESTAMPNS = getattr(socket, 'SO_TIMESTAMPNS', 35)

for iface in LINKS:
    sock = None
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        sock.bind((iface, 0))
        try:
            sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        except OSError:
            pass  # Kernel timestamps may not be supported; use topology order.
        sock.setblocking(False)
        sel.register(sock, selectors.EVENT_READ, iface)
        ready.append(iface)
    except (OSError, ValueError) as e:
        failed.append(iface + ': ' + str(e))
        if sock:
            sock.close()
send(dict(event='worker_status', ready=ready, failed=failed, total=len(LINKS)))
if not ready:
    sys.exit(2)
def poll_fdb():
    # FDB command execution must not pause raw packet capture.
    while True:
        send(dict(event='fdb', tables=get_fdb(), ts=time.time()))
        time.sleep(1.7)

threading.Thread(target=poll_fdb, daemon=True).start()
try:
    while True:
        for key, _ in sel.select(timeout=0.12):
            # One interface can have multiple queued frames due to flooding;
            # drain up to 128 so later ICMP replies don't get stuck behind them.
            for _ in range(128):
                try:
                    frame, ancillary, _flags, addr = key.fileobj.recvmsg(65535, 128)
                except (OSError, BlockingIOError):
                    break
                pkt = parse_frame(frame)
                if pkt is None:
                    continue
                iface = key.data
                node, peer = LINKS[iface]
                # Linux PACKET_OUTGOING = 4. Otherwise the frame entered the
                # switch-side interface from its peer.
                outgoing = len(addr) >= 3 and addr[2] == 4
                captured_at = None
                for level, ctype, cdata in ancillary:
                    if level == socket.SOL_SOCKET and ctype == SO_TIMESTAMPNS:
                        if len(cdata) >= struct.calcsize('@ll'):
                            sec, nsec = struct.unpack('@ll', cdata[:struct.calcsize('@ll')])
                            captured_at = sec + nsec / 1000000000.0
                        break
                pkt.update(event='packet', iface=iface, direction='out' if outgoing else 'in',
                           fromNode=node if outgoing else peer,
                           toNode=peer if outgoing else node,
                           ts=captured_at if captured_at is not None else time.time(),
                           captureClock='kernel' if captured_at is not None else 'poll')
                send(pkt)
finally:
    sel.close()
'''


# Each physical link is observed on one switch-side interface. These are graph
# connections only; a graph edge must NEVER be rendered without a captured frame.
TOPOLOGY_LINKS = [
    ('s1','s2'),('s1','s3'),('s1','s4'),('s1','h12'),
    ('s2','h1'),('s2','h2'),('s2','h3'),
    ('s3','h4'),('s3','h5'),('s3','h6'),('s3','h7'),
    ('s4','h8'),('s4','h9'),('s4','h10'),('s4','h11'),
]
GRAPH = {}
for a, b in TOPOLOGY_LINKS:
    GRAPH.setdefault(a, set()).add(b)
    GRAPH.setdefault(b, set()).add(a)


def shortest_path(origin, destination):
    """Shortest path in the tutorial's topology, not proof of forwarding."""
    if origin not in GRAPH or destination not in GRAPH:
        return []
    parent = {origin: None}
    todo = deque([origin])
    while todo and destination not in parent:
        curr = todo.popleft()
        for neighbor in sorted(GRAPH[curr]):
            if neighbor not in parent:
                parent[neighbor] = curr
                todo.append(neighbor)
    if destination not in parent:
        return []
    result, node = [], destination
    while node is not None:
        result.append(node)
        node = parent[node]
    return result[::-1]


def hop_distances(origin):
    """Distance from an origin for fallback order of extra observed branches."""
    result = {origin: 0}
    q = deque([origin])
    while q:
        u = q.popleft()
        for v in GRAPH.get(u, ()):
            if v not in result:
                result[v] = result[u] + 1
                q.append(v)
    return result


def ordered_observations(events):
    """Keep only captured link crossings; mark destination path vs side copies.

    When all records have Linux kernel capture timestamps, preserve actual
    capture order including revisits (s2 -> s1 -> s2 -> h2). If timestamp
    support is unavailable, show a clearly-labeled *illustrative* ordering:
    destination-path links first, then any observed side branches. This is
    not a substitute for capture order in the presence of a forwarding loop.
    """
    if not events:
        return []
    result = []
    for kind in ('request', 'reply'):
        raw = [ev for ev in events if ev.get('kind') == kind]
        if not raw:
            continue
        origin, destination = raw[0]['srcHost'], raw[0]['dstHost']
        path = shortest_path(origin, destination)
        main_edges = {(a, b): idx for idx, (a, b) in
                      enumerate(zip(path, path[1:]))}
        dist = hop_distances(origin)
        phase = []
        for ev in raw:
            item = dict(ev)
            edge = (item.get('fromNode'), item.get('toNode'))
            item['routeRole'] = 'main' if edge in main_edges else 'branch'
            item['pathDescription'] = '宛先に向かう経路上' if item['routeRole'] == 'main' else '主経路外のコピー'
            if item['routeRole'] == 'branch' and (edge[0], edge[1]) == ('s1', 's2') and {origin, destination}.issubset({'h1', 'h2', 'h3'}):
                item['pathDescription'] = 's1からs2への返送（主経路外）'
            phase.append(item)
        use_kernel_time = all(ev.get('captureClock') == 'kernel' for ev in phase)
        if use_kernel_time:
            phase.sort(key=lambda ev: (float(ev.get('ts', 0)),
                         main_edges.get((ev.get('fromNode'), ev.get('toNode')), 999)))
        else:
            def teaching_order(ev):
                edge = (ev.get('fromNode'), ev.get('toNode'))
                if edge in main_edges:
                    return (0, main_edges[edge], float(ev.get('ts', 0)))
                src = dist.get(edge[0], 999)
                dst = dist.get(edge[1], 999)
                rank = src if dst > src else max(src, dst) + .25
                return (1, rank, float(ev.get('ts', 0)))
            phase.sort(key=teaching_order)
        origin_seen = any(ev.get('fromNode') == origin for ev in phase)
        seen_main_links = set()
        for i, ev in enumerate(phase):
            edge = (ev.get('fromNode'), ev.get('toNode'))
            ev['repeatOnMainPath'] = ev['routeRole'] == 'main' and edge in seen_main_links
            if ev['routeRole'] == 'main':
                seen_main_links.add(edge)
            if ev['repeatOnMainPath']:
                ev['pathDescription'] = '宛先経路上の再観測（コピーの可能性）'
            ev['firstInPhase'] = i == 0
            ev['originObserved'] = origin_seen
            ev['missingOrigin'] = i == 0 and not origin_seen
            ev['orderBasis'] = 'kernel' if use_kernel_time else 'topology'
            result.append(ev)
    return result


class PacketAssembler:
    """Hold a ping exchange briefly, then replay captured hops in path order.

    Polling 15 AF_PACKET sockets can deliver s1's reply observation before
    s3-eth3's observation of h5 -> s3. Instead of rendering that arrival order,
    group a request/reply exchange by ICMP identifier, sequence and host pair.
    """
    def __init__(self, quiet_seconds=0.9):
        self.quiet_seconds = quiet_seconds
        self.pending = {}
        self.lock = threading.Lock()

    @staticmethod
    def key(ev):
        ends = tuple(sorted((ev.get('srcHost', ''), ev.get('dstHost', ''))))
        return (ev.get('icmpId', -1), ev.get('seq', -1), ends)

    def add(self, ev, now=None):
        now = time.monotonic() if now is None else now
        key = self.key(ev)
        with self.lock:
            group = self.pending.get(key)
            if group is None:
                self.pending[key] = {'last': now, 'first': now, 'events': [dict(ev)]}
            else:
                group['last'] = now
                group['events'].append(dict(ev))

    def pop_ready(self, now=None, force=False):
        now = time.monotonic() if now is None else now
        with self.lock:
            ready_keys = [k for k, g in self.pending.items()
                          if force or now - g['last'] >= self.quiet_seconds]
            ready = [self.pending.pop(k) for k in ready_keys]
        ready.sort(key=lambda g: g['first'])
        return [ordered_observations(g['events']) for g in ready]


class Hub:
    def __init__(self, mode, container):
        self.mode, self.container = mode, container
        self.lock = threading.Lock()
        self.listeners = set()
        self.events = deque(maxlen=100)
        # Raw interface observations, kept before packet grouping and playback.
        # These let us distinguish capture loss from incorrect animation.
        self.raw_packets = deque(maxlen=12000)
        self.latest_fdb = None
        self.proc = None
        self.assembler = PacketAssembler()
        self.stopped = threading.Event()
        self.state = {
            'mode': mode, 'container': container, 'running': False,
            'interfaces': 0, 'expectedInterfaces': 15,
            'packets': 0, 'warning': '', 'fdbSupported': None,
        }

    def state_snapshot(self):
        with self.lock:
            return dict(self.state)

    def emit(self, message):
        with self.lock:
            if message.get('event') in ('packet', 'packet_batch'):
                self.events.append(message)
                self.state['packets'] += (len(message['packets']) if message['event'] == 'packet_batch' else 1)
            if message.get('event') == 'worker_status':
                self.state['interfaces'] = len(message.get('ready', []))
                self.state['running'] = self.state['interfaces'] > 0
                if message.get('failed'):
                    self.state['warning'] = '監視できないインターフェース: ' + '; '.join(message['failed'])
            if message.get('event') == 'fdb':
                self.state['fdbSupported'] = message.get('tables') is not None
                self.latest_fdb = message.get('tables')
            listeners = list(self.listeners)
        payload = json.dumps(message, ensure_ascii=False, separators=(',', ':'))
        for q in listeners:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass

    def diagnostics(self):
        """A snapshot of UNMODIFIED capture records, not synthesized paths."""
        with self.lock:
            return {'format': 'lpm-l2-capture-v1', 'generatedAt': datetime.now().isoformat(),
                    'state': dict(self.state), 'rawPackets': list(self.raw_packets),
                    'fdb': self.latest_fdb}

    def error(self, msg):
        print('警告:', msg, file=sys.stderr, flush=True)
        with self.lock:
            self.state['warning'] = str(msg)
        self.emit({'event': 'warning', 'message': str(msg)})

    def add_listener(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.listeners.add(q)
            recent = list(self.events)[-45:]
        for ev in recent:
            try:
                q.put_nowait(json.dumps(ev, ensure_ascii=False))
            except queue.Full:
                break
        return q

    def remove_listener(self, q):
        with self.lock:
            self.listeners.discard(q)

    def run_live(self):
        if not shutil.which('docker'):
            raise RuntimeError('dockerコマンドが見つかりません。Docker Desktopを起動してください。')
        try:
            p = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', self.container],
                               capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError('docker inspectを実行できません: ' + str(e))
        if p.returncode or p.stdout.strip() != 'true':
            raise RuntimeError('コンテナ '+self.container+' が起動していません。docker psを確認してください。')
        self.proc = subprocess.Popen(
            ['docker', 'exec', self.container, 'python3', '-u', '-c', WORKER_CODE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, bufsize=1, errors='replace')
        threading.Thread(target=self._consume_worker, daemon=True).start()
        threading.Thread(target=self._drain_errors, daemon=True).start()
        threading.Thread(target=self._flush_packets, daemon=True).start()

    def _consume_worker(self):
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        if message.get('event') == 'packet':
                            with self.lock:
                                self.raw_packets.append(dict(message, receivedAt=time.time()))
                            self.assembler.add(message)
                        else:
                            self.emit(message)
                except json.JSONDecodeError:
                    self.error('監視プロセスから解析できないデータを受信しました。')
            if not self.stopped.is_set():
                self.error('コンテナ内のパケット監視が停止しました。Dockerのログを確認してください。')
        except Exception as exc:
            if not self.stopped.is_set():
                self.error('パケット監視エラー: ' + str(exc))

    def _flush_packets(self):
        # A delayed batch avoids replaying s1's observation of an Echo Reply
        # before the later-polled h5 -> s3 observation of that same reply.
        while not self.stopped.wait(0.08):
            for packets in self.assembler.pop_ready():
                if packets:
                    self.emit({'event': 'packet_batch', 'packets': packets})

    def _drain_errors(self):
        for line in self.proc.stderr:
            if line.strip():
                self.error(line.strip()[:500])

    def run_demo(self):
        self.emit({'event': 'worker_status', 'ready': ['demo'] * 15, 'failed': []})
        self.state['warning'] = 'デモモード：表示するパケットは実測ではありません。'
        # Each tuple represents one *observed* physical-link crossing.
        # For h1 -> h2, demonstrate the main route AND a flood to s1/h3;
        # s1's copy is not a reply from h2.
        exchanges = [
            [ ('request','h1','h5','h1','s2','s2-eth2'),
              ('request','h1','h5','s2','s1','s1-eth1'),
              ('request','h1','h5','s1','s3','s1-eth2'),
              ('request','h1','h5','s3','h5','s3-eth3'),
              ('reply','h5','h1','h5','s3','s3-eth3'),
              ('reply','h5','h1','s3','s1','s1-eth2'),
              ('reply','h5','h1','s1','s2','s1-eth1'),
              ('reply','h5','h1','s2','h1','s2-eth2') ],
            [ ('request','h1','h2','h1','s2','s2-eth2'),
              ('request','h1','h2','s2','h2','s2-eth3'),
              ('request','h1','h2','s2','h3','s2-eth4'),
              ('request','h1','h2','s2','s1','s1-eth1'),
              ('request','h1','h2','s1','s2','s1-eth1'),
              ('reply','h2','h1','h2','s2','s2-eth3'),
              ('reply','h2','h1','s2','h1','s2-eth2') ],
        ]
        def stepper():
            seq = 1
            while not self.stopped.is_set():
                for exchange in exchanges:
                    events=[]
                    for kind,src,dst,fr,to,iface in exchange:
                        if self.stopped.wait(.13):
                            return
                        events.append({'event':'packet','iface':iface,
                                       'direction':'out', 'fromNode':fr,'toNode':to,
                                       'ts':time.time(),'captureClock':'kernel',
                                       'srcHost':src,'dstHost':dst,
                                       'srcIp':'10.0.0.'+src[1:],
                                       'dstIp':'10.0.0.'+dst[1:],
                                       'kind':kind,'seq':seq,'icmpId':33,
                                       'srcMac':'demo','dstMac':'demo'})
                    self.emit({'event':'packet_batch',
                               'packets':ordered_observations(events)})
                    seq += 1
                if self.stopped.wait(2):
                    return
        threading.Thread(target=stepper, daemon=True).start()

    def shutdown(self):
        self.stopped.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            try:
                payload = DASHBOARD.read_bytes()
            except OSError as exc:
                self.send_error(500, str(exc))
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif path == '/api/status':
            payload = json.dumps(self.server.hub.state_snapshot(), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)
        elif path == '/api/diagnostics.json':
            payload = json.dumps(self.server.hub.diagnostics(), ensure_ascii=False,
                                 indent=2).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename=icmp_diagnostics.json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            q = self.server.hub.add_listener()
            try:
                self.wfile.write(('data: '+json.dumps({'event':'status', **self.server.hub.state_snapshot()},
                                                      ensure_ascii=False)+'\n\n').encode('utf-8'))
                self.wfile.flush()
                while not self.server.hub.stopped.is_set():
                    try:
                        message = q.get(timeout=12)
                        self.wfile.write(('data: '+message+'\n\n').encode('utf-8'))
                    except queue.Empty:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.server.hub.remove_listener(q)
        elif path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Tutorial 5 LPM/L2 live packet visualizer')
    parser.add_argument('--container', default=DEFAULT_CONTAINER, help='起動中のMininetコンテナ名')
    parser.add_argument('--port', type=int, default=8766, help='ブラウザ表示用のローカルポート')
    parser.add_argument('--no-browser', action='store_true', help='自動でブラウザを開かない')
    parser.add_argument('--demo', action='store_true', help='Dockerなしで表示を試す（架空パケット）')
    args = parser.parse_args(argv)
    hub = Hub('demo' if args.demo else 'live', args.container)
    try:
        if args.demo:
            hub.run_demo()
        else:
            hub.run_live()
    except (RuntimeError, OSError) as exc:
        parser.exit(1, 'エラー: '+str(exc)+'\n')
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), WebHandler)
    except OSError as exc:
        hub.shutdown()
        parser.error('ポート '+str(args.port)+' を使用できません: '+str(exc))
    server.daemon_threads = True
    server.hub = hub
    url = 'http://127.0.0.1:'+str(args.port)
    print('Tutorial 5 LPM/L2 可視化ツール: '+url, flush=True)
    if args.demo:
        print('デモモード：架空のパケットを表示中（実通信ではありません）', flush=True)
    else:
        print('接続先: '+args.container+' / Mininetで h1 ping -c 3 h5 等を実行してください。', flush=True)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print('\n可視化ツールを終了します。', flush=True)
    finally:
        hub.shutdown()
        server.shutdown()
        server.server_close()
    return 0

if __name__ == '__main__':
    sys.exit(main())
