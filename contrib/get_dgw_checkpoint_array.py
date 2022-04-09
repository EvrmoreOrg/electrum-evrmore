#!/usr/bin/env python
from json import loads, dumps
from sys import exit, argv
import base64
import urllib.request, urllib.error, urllib.parse

if len(argv) < 3:
    print('Arguments: <rpc_username> <rpc_password> [<rpc_port>]')
    exit(1)

def bits_to_target(bits: int) -> int:
        # arith_uint256::SetCompact in Bitcoin Core
        if not (0 <= bits < (1 << 32)):
            raise Exception(f"bits should be uint32. got {bits!r}")
        bitsN = (bits >> 24) & 0xff
        bitsBase = bits & 0x7fffff
        if bitsN <= 3:
            target = bitsBase >> (8 * (3-bitsN))
        else:
            target = bitsBase << (8 * (bitsN-3))
        if target != 0 and bits & 0x800000 != 0:
            # Bit number 24 (0x800000) represents the sign of N
            raise Exception("target cannot be negative")
        if (target != 0 and
                (bitsN > 34 or
                 (bitsN > 33 and bitsBase > 0xff) or
                 (bitsN > 32 and bitsBase > 0xffff))):
            raise Exception("target has overflown")
        return target

def rpc(method, params):
    data = {
        "jsonrpc": "1.0",
        "id":"1",
        "method": method,
        "params": params
    }

    data_json = dumps(data)
    username = argv[1]
    password = argv[2]
    port = 8766
    if len(argv) > 3:
        port = argv[3]
    url = "http://127.0.0.1:{}/".format(port)
    req = urllib.request.Request(url, data_json.encode("utf-8"), {'content-type': 'application/json'})

    base64string = base64.encodebytes(('%s:%s' % (username, password)).encode('ascii')).decode().replace('\n', '')
    req.add_header("Authorization", "Basic %s" % base64string)

    response_stream = urllib.request.urlopen(req)
    json_response = response_stream.read()

    return loads(json_response)

INTERVAL = 2016
START = 168 * INTERVAL

curr_height = START

checkpoints = []
block_count = int(rpc('getblockcount', [])['result'])
print(('Blocks: {}'.format(block_count)))
while True:
    print(curr_height)
    h = rpc('getblockhash', [curr_height])['result']
    block = rpc('getblock', [h])['result']

    h2 = rpc('getblockhash', [curr_height + INTERVAL - 1])['result']
    block2 = rpc('getblock', [h2])['result']

    checkpoints.append([
        [block['hash'],
        bits_to_target(int(block['bits'], 16))],
        [block2['hash'],
        bits_to_target(int(block2['bits'], 16))]
    ])

    curr_height += INTERVAL
    if curr_height > block_count - INTERVAL:
        print('Done.')
        break

with open('checkpoints_dgw.json', 'w+') as f:
    f.write(dumps(checkpoints, indent=4, separators=(',', ':')))
