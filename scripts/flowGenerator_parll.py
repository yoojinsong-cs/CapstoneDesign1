"""Generate flows using the TrafficGenerator tool.
   https://github.com/HKUST-SING/TrafficGenerator """

import os, time
import multiprocessing 
from multiprocessing import Pool
from itertools import product
from contextlib import contextmanager

from mininet.cli import CLI

def flowGenerator (network, port, flowsPerPair):
    workDirectory = os.getcwd()

    allHosts = network.hosts
    serverHosts = []
    clientHosts = []

    for host in allHosts:
        if host.name[0] == 's':
            serverHosts.append (host)
    for host in allHosts:
        if host.name[0] == 'c':
            clientHosts.append (host)



    for clientIndex in range (len (clientHosts)):
        configLines = []


        serverIp = serverHosts[clientIndex % len (serverHosts)].params['ip']
        line = 'server %s %s' % (serverIp, port)
        configLines.append (line)

        cdf = 'VL2_CDF.txt' # change this if you want to use a different CDF file.
        line = 'req_size_dist TrafficGenerator/conf/%s' % cdf
        configLines.append (line)

        with open ('TrafficGenerator/conf/client%s_config.txt' % clientIndex, 'w') as configFile:
            for line in configLines:
                configFile.write (line)
                configFile.write ('\n')
    
    # Delete previous results

    resultText = 'flows.txt'
    if os.path.exists ('TrafficGenerator/%s' % resultText):
        os.remove ('TrafficGenerator/%s' % resultText)

    # offload off on every interface: capture what is on the wire (1448B segments), not merged 2896B ones
    for node in network.hosts + network.switches:
        for intf in node.intfNames():
            if intf != 'lo':
                node.cmd('ethtool -K %s gro off gso off tso off' % intf)

    # Start server daemons

    startServer(serverHosts, flowsPerPair, port)
    
    time.sleep (1)
    print ('opened all servers...')

    # CLI (network)
    
    # Start client requests
    poolcount = multiprocessing.cpu_count()
    print(poolcount)
    pool = Pool(poolcount)
    #parll_func = partial(startClient, clientHosts, flowsPerPair)
    lenHosts = len(clientHosts)
    
    # capture client-side packets (main.py kills tshark at the end)
    # /tmp: dumpcap drops root's file override, so it can't write into a 750 home dir
    for c in clientHosts:
        if os.path.exists('tshark_%s.log' % c.name):
            os.remove('tshark_%s.log' % c.name)  # stale "Capturing on" would fool the wait below
        # -s 128: keep headers + the 16B request payload only (payload is dummy zeros); tcp.len still comes from the IP header
        c.cmd('tshark -s 128 -i %s-eth0 -f "tcp port %s" -w /tmp/%s.pcap > tshark_%s.log 2>&1 &' % (c.name, port, c.name, c.name))
    # wait until every tshark says "Capturing on" (a fixed 2s sleep missed the first flows)
    for c in clientHosts:
        for _ in range(60):
            if os.path.exists('tshark_%s.log' % c.name) and 'Capturing on' in open('tshark_%s.log' % c.name).read():
                break
            time.sleep(0.5)
        else:
            print('WARNING: tshark on %s did not start' % c.name)
    time.sleep(1)

    # different seed per run and per client; printed so a run can be reproduced
    baseSeed = int(time.time()) % 1000000
    print('baseSeed', baseSeed)

    processes = []
    print("lenHosts",lenHosts)
    for cIndex in range(0,lenHosts): 
        p = multiprocessing.Process(target=startClient, args=(clientHosts, flowsPerPair,cIndex, baseSeed))  
        p.start()
        processes.append(p)
 
    for process in processes:
        process.join()
    
    print ('client requests done...')

def startServer(serverHosts, flowsPerPair, port):
  for server in serverHosts:
        print ('open ' + server.name + ' port#%s' % (port))
        server.cmd ('TrafficGenerator/bin/server -p %s -d ' % (port))
          
def startClient(clientHosts, flowsPerPair, clientIndex, baseSeed):
  seed = baseSeed * 100 + clientIndex + 1  # never 0 (0 = time-based in client)
  print ('start flows from ' + clientHosts[clientIndex].name)
  print (clientHosts[clientIndex].name+ ' client flow generate current time' + time.strftime('%Y.%m.%d - %H:%M:%S'))
  clientHosts[clientIndex].cmd ('TrafficGenerator/bin/client -b 900 -c TrafficGenerator/conf/client%s_config.txt -n %s -l flows_%s.txt -s %s -v > log_%s.txt' % (clientIndex, flowsPerPair, clientIndex, seed, clientIndex))
  print (clientHosts[clientIndex].name + ' seed %s' % seed)
def merge_startClinet(args):
  startClient(*args)
