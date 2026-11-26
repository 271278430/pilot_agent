"""CPU-only Ray experiment gateway, used inside a network-none container.

JSON frames on stdin/stdout are relayed over an authenticated SSH channel. A
peer can only dial ports observed listening inside this task's container.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

CFG = json.loads(Path('/code/config.json').read_text())
SELF, PEER = CFG['self_ip'], CFG['peer_ip']
assert {SELF, PEER} == {'127.0.0.2', '127.0.0.3'}
assert CFG['role'] in {'head', 'worker'}
DEADLINE = float(CFG['deadline'])
LOCK, STATE_LOCK = threading.Lock(), threading.Lock()
connections, listeners, half_closed = {}, {}, {}
processes = []
stopping = threading.Event()


def emit(value):
    payload = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    with LOCK:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def event(name, **value):
    emit({'type': 'event', 'name': name, **value})


def close_connection(connection_id):
    with STATE_LOCK:
        conn = connections.pop(connection_id, None)
        half_closed.pop(connection_id, None)
    if conn:
        try:
            conn.close()
        except OSError:
            pass


def close_half(connection_id, side):
    with STATE_LOCK:
        state = half_closed.get(connection_id)
        if state is None:
            return
        state.add(side)
        complete = state == {'local', 'peer'}
    if complete:
        close_connection(connection_id)


def relay(connection_id, conn):
    try:
        while not stopping.is_set():
            block = conn.recv(32768)
            if not block:
                emit({'type': 'eof', 'id': connection_id})
                close_half(connection_id, 'local')
                return
            emit({'type': 'data', 'id': connection_id, 'data': base64.b64encode(block).decode()})
    except OSError:
        emit({'type': 'close', 'id': connection_id})
        close_connection(connection_id)


def register(connection_id, conn, start=True):
    with STATE_LOCK:
        if len(connections) >= 256 or connection_id in connections:
            raise ValueError('Connection limit or duplicate identity')
        connections[connection_id] = conn
        half_closed[connection_id] = set()
    if start:
        threading.Thread(target=relay, args=(connection_id, conn), daemon=True).start()


def listen(port):
    if port in listeners:
        return
    sock = socket.socket()
    try:
        sock.bind((PEER, port))
        sock.listen(32)
        sock.settimeout(0.5)
    except OSError as exc:
        sock.close()
        event('proxy_bind_failed', port=port, error=type(exc).__name__)
        return
    listeners[port] = sock

    def accept():
        while not stopping.is_set():
            try:
                conn, _ = sock.accept()
                conn.settimeout(None)
                ident = uuid.uuid4().hex
                register(ident, conn, start=False)
                emit({'type': 'open', 'id': ident, 'port': port})
                threading.Thread(target=relay, args=(ident, conn), daemon=True).start()
            except TimeoutError:
                continue
            except (OSError, ValueError):
                break

    threading.Thread(target=accept, daemon=True).start()


def observed_ports():
    result = set()
    for filename in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            rows = Path(filename).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or fields[3] != '0A':
                continue
            addr, port = fields[1].split(':')
            if filename.endswith('tcp6'):
                allowed = addr == '0' * 32
            else:
                value = socket.inet_ntoa(bytes.fromhex(addr)[::-1])
                allowed = value in ('0.0.0.0', SELF)
            if allowed:
                result.add(int(port, 16))
    return result


def monitor():
    previous = set()
    while not stopping.wait(0.15):
        ports = observed_ports()
        added = ports - previous
        if added:
            emit({'type': 'listen', 'ports': sorted(added)})
        previous = ports


def worker_env():
    env = dict(os.environ)
    env.update(PYTHONPATH='/env', HOME='/task/home', TMPDIR='/task/tmp', RAY_TMPDIR='/task/tmp',
               RAY_USAGE_STATS_ENABLED='0', CUDA_VISIBLE_DEVICES='', MUSA_VISIBLE_DEVICES='-1',
               RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO='0', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
               MKL_NUM_THREADS='1', NO_PROXY='127.0.0.1,127.0.0.2,127.0.0.3,localhost',
               no_proxy='127.0.0.1,127.0.0.2,127.0.0.3,localhost')
    return env


def start_ray():
    if processes:
        raise ValueError('Ray already started')
    base = 32000 if CFG['role'] == 'head' else 33000
    ports = [str(base + n) for n in range(10, 20)]
    command = [sys.executable, '-B', '-m', 'ray.scripts.scripts', 'start', '--block',
               '--node-ip-address', SELF, '--num-cpus', '1', '--num-gpus', '0', '--temp-dir', '/task/ray',
               '--object-store-memory', str(128 * 1024 * 1024),
               '--node-manager-port', str(base + 2), '--object-manager-port', str(base + 3),
               '--runtime-env-agent-port', str(base + 4), '--dashboard-agent-listen-port', str(base + 5),
               '--dashboard-agent-grpc-port', str(base + 6), '--metrics-export-port', str(base + 7),
               '--worker-port-list', ','.join(ports)]
    if CFG['role'] == 'head':
        command += ['--head', '--port', '32001', '--include-dashboard', 'false',
                    '--dashboard-host', '127.0.0.1', '--dashboard-port', '32008']
    else:
        command += ['--address', PEER + ':32001']
    log = open('/task/ray-start.log', 'ab', buffering=0)
    proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=worker_env(), start_new_session=True)
    processes.append(proc)
    event('ray_started', role=CFG['role'], pid=proc.pid)


def run_probe():
    if CFG['role'] != 'head':
        raise ValueError('Probe belongs on the head')
    log = open('/task/probe.log', 'ab', buffering=0)
    proc = subprocess.Popen([sys.executable, '-B', '/code/cpu_ray_probe.py'],
                            stdout=log, stderr=subprocess.STDOUT, env=worker_env(), start_new_session=True)
    processes.append(proc)

    def report():
        rc = proc.wait()
        output = Path('/task/probe-result.json')
        value = json.loads(output.read_text()) if output.is_file() else None
        event('probe_finished', rc=rc, result=value)

    threading.Thread(target=report, daemon=True).start()
    event('probe_started', pid=proc.pid)


def handle(frame):
    kind = frame['type']
    if kind == 'expose':
        for port in frame['ports']:
            if type(port) is int and 1024 <= port <= 65535:
                listen(port)
    elif kind == 'dial':
        ident, port = frame['id'], frame['port']
        if port not in observed_ports():
            emit({'type': 'close', 'id': ident})
            return
        try:
            conn = socket.create_connection((SELF, port), timeout=3)
            conn.settimeout(None)
            register(ident, conn)
        except (OSError, ValueError):
            emit({'type': 'close', 'id': ident})
    elif kind in {'data', 'eof', 'close'}:
        with STATE_LOCK:
            conn = connections.get(frame['id'])
        if conn is None:
            return
        try:
            if kind == 'data':
                conn.sendall(base64.b64decode(frame['data'], validate=True))
            elif kind == 'eof':
                conn.shutdown(socket.SHUT_WR)
                close_half(frame['id'], 'peer')
            else:
                close_connection(frame['id'])
        except OSError:
            close_connection(frame['id'])
    elif kind == 'start':
        start_ray()
    elif kind == 'probe':
        run_probe()
    elif kind == 'status':
        event('status', ray_alive=bool(processes and processes[0].poll() is None),
              listening=sorted(observed_ports()), proxies=sorted(listeners))
    elif kind == 'stop':
        stopping.set()


def watchdog():
    while not stopping.wait(0.5):
        if time.time() >= DEADLINE:
            stopping.set()
            os._exit(124)


def main():
    Path('/task/home').mkdir(exist_ok=True)
    Path('/task/tmp').mkdir(exist_ok=True)
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    event('ready', role=CFG['role'])
    try:
        while not stopping.is_set():
            raw = sys.stdin.buffer.readline(1024 * 1024)
            if not raw:
                break
            try:
                handle(json.loads(raw))
            except Exception as exc:
                event('gateway_error', error=type(exc).__name__, message=str(exc)[:300])
    finally:
        stopping.set()
        for sock in list(listeners.values()) + list(connections.values()):
            try:
                sock.close()
            except OSError:
                pass
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        event('exiting')


if __name__ == '__main__':
    main()
