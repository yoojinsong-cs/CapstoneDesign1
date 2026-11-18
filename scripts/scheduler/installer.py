"""Install a scheduler only on explicitly resolved Mininet switch ports."""

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .mock_classifier import DEFAULT_CLASSIFIER_DELAY


POLICY_VERSION = '4'
PRIORITY_POLICY = 'unknown_mice_first'
MODULE_STATE = Path('/sys/module/sch_flowprio')
KERNEL_DIRECTORY = Path(__file__).resolve().parent / 'kernel'


@dataclass(frozen=True)
class SchedulerConfig:
    mode: str = 'priority'
    rate_mbps: int = 10
    delay_seconds: float = DEFAULT_CLASSIFIER_DELAY
    threshold_bytes: int = 1_000_000
    quantum_bytes: int = 1514
    packet_limit: int = 4096
    per_flow_limit: int = 256

    def __post_init__(self):
        if self.mode not in ('fifo', 'fair', 'priority'):
            raise ValueError('unknown scheduler mode')
        if not 1 <= self.rate_mbps <= 100000:
            raise ValueError('invalid scheduler rate')
        if not math.isfinite(self.delay_seconds) or self.delay_seconds < 0 or self.threshold_bytes <= 0:
            raise ValueError('invalid classifier configuration')
        if not 1500 <= self.quantum_bytes <= 65536:
            raise ValueError('invalid quantum')
        if not 1 <= self.per_flow_limit <= self.packet_limit <= 65536:
            raise ValueError('invalid queue limits')

    def command(self, ifindex):
        mode = {'fifo': 0, 'fair': 1, 'priority': 2}[self.mode]
        return 'config %d %d %d %d %d %d\n' % (
            ifindex, self.rate_mbps * 1_000_000, mode,
            self.quantum_bytes,
            self.packet_limit, self.per_flow_limit)


def ensure_module(port):
    if os.geteuid() != 0:
        raise RuntimeError('switch scheduling requires running main.py with sudo')
    loaded = MODULE_STATE
    reload_module = False
    if loaded.exists():
        actual = int((loaded / 'parameters/server_port').read_text())
        if actual != port:
            raise RuntimeError('loaded sch_flowprio uses server port %d, requested %d; '
                               'detach its qdiscs before unloading/reloading' % (actual, port))
        version = loaded / 'version'
        if version.exists() and version.read_text().strip() == POLICY_VERSION:
            return
        if int((loaded / 'refcnt').read_text()) != 0:
            raise RuntimeError('outdated sch_flowprio is in use; finish the running experiment '
                               'and remove its qdiscs before retrying')
        reload_module = True
    kernel = KERNEL_DIRECTORY
    module = kernel / 'sch_flowprio.ko'
    sources = [kernel / 'sch_flowprio.c', kernel / 'Makefile']
    up_to_date = module.exists() and all(
        source.stat().st_mtime <= module.stat().st_mtime for source in sources)
    if up_to_date:
        version = subprocess.check_output(['modinfo', '-F', 'vermagic', str(module)], text=True)
        policy = subprocess.check_output(['modinfo', '-F', 'version', str(module)], text=True)
        up_to_date = version.split()[0] == os.uname().release and policy.strip() == POLICY_VERSION
    if not up_to_date and not shutil.which('gcc-12'):
        raise RuntimeError('gcc-12 is required for the current kernel: '
                           'sudo apt-get install gcc-12')
    if not up_to_date:
        subprocess.run(['make', '-C', str(kernel)], check=True)
    if reload_module:
        # Ordinary removal refuses active references, including ones acquired
        # after the check above. Never detach another experiment's qdiscs.
        print('switch scheduler: updating unused module to policy version %s' % POLICY_VERSION,
              flush=True)
        subprocess.run(['rmmod', 'sch_flowprio'], check=True)
    subprocess.run(['insmod', str(module),
                    'server_port=%d' % port], check=True)


class SwitchScheduler:
    def __init__(self, config, directory, experiment=None):
        self.config = config
        self.directory = Path(directory).resolve()
        self.interfaces = []
        self.process = None
        self.output = None
        self.experiment = experiment or {}

    def start(self, network):
        self.directory.mkdir(parents=True, exist_ok=False)
        ports = []
        sources = [network.get(node.name) for node in network.switches
                   if node.name.startswith('Edges')]
        for source in sources:
            for destination in network.switches:
                if not destination.name.startswith('core'):
                    continue
                connections = source.connectionsTo(destination)
                if len(connections) != 1:
                    raise RuntimeError('expected exactly one server-edge/core link')
                interface = connections[0][0].name
                ports.append(dict(interface=interface,
                                  ifindex=socket.if_nametoindex(interface),
                                  core=destination.name))
        if not ports:
            raise RuntimeError('no server-edge/core output ports found')
        manifest = dict(config=asdict(self.config), policy_version=POLICY_VERSION,
                        priority_policy=PRIORITY_POLICY, interfaces=ports,
                        interface=ports[0]['interface'], ifindex=ports[0]['ifindex'],
                        run_id=self.directory.name,
                        experiment=self.experiment)
        (self.directory / 'manifest.json').write_text(
            json.dumps(manifest, indent=2) + '\n')
        try:
            # flowGenerator disables offloads on all interfaces before traffic;
            # also disable this port before installing the packet-level qdisc.
            for port in ports:
                interface = port['interface']
                subprocess.run(['ethtool', '-K', interface, 'gro', 'off',
                                'gso', 'off', 'tso', 'off'], check=True,
                               stdout=subprocess.DEVNULL)
                # TCLink already created HTB root 5: and shaped class 5:1.
                # Preserve that token-bucket shaper and replace only its leaf;
                # flowprio v3 selects packets but never paces them itself.
                subprocess.run(['tc', 'qdisc', 'replace', 'dev', interface,
                                'parent', '5:1', 'handle', '10:', 'flowprio'],
                               check=True)
                self.interfaces.append(interface)
                with open('/proc/net/flowprio', 'w') as control:
                    control.write(self.config.command(port['ifindex']))
            ready = self.directory / 'controller.ready'
            self.output = (self.directory / 'controller.log').open('w')
            self.process = subprocess.Popen([
                sys.executable, '-m', 'scheduler.controller',
                '--interfaces', *[str(port['ifindex']) for port in ports],
                '--run-id', self.directory.name,
                '--logdir', str(self.directory), '--ready', str(ready),
                '--delay', str(self.config.delay_seconds),
                '--threshold', str(self.config.threshold_bytes)],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=self.output, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 5
            while not ready.exists():
                self.check()
                if time.monotonic() >= deadline:
                    raise RuntimeError('classifier controller startup timed out')
                time.sleep(0.05)
            self.check()
            print('switch scheduler: %s on %s, %dMbps, classifier delay %.3fs; logs %s'
                  % (self.config.mode, ', '.join(self.interfaces), self.config.rate_mbps,
                     self.config.delay_seconds, self.directory), flush=True)
        except BaseException:
            self.stop()
            raise

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise RuntimeError('classifier controller exited (%s); see %s' % (
                self.process.returncode, self.directory / 'controller.log'))

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None
        for interface in self.interfaces:
            subprocess.run(['tc', 'qdisc', 'del', 'dev', interface,
                            'parent', '5:1', 'handle', '10:'],
                           check=False, capture_output=True)
        self.interfaces.clear()
        # Keep experiment artifacts writable by the user who invoked sudo.
        if os.environ.get('SUDO_UID') and self.directory.exists():
            uid, gid = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
            for path in [self.directory, *self.directory.iterdir()]:
                os.chown(path, uid, gid)
