# Minimize imports to decrease overhead
# Changes to blockchain.py must be reflected here

import hashlib
import multiprocessing
import struct

import kawpow
import x16r_hash
import x16rv2_hash

import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)


def to_bytes(something, encoding='utf8') -> bytes:
    if isinstance(something, bytes):
        return something
    if isinstance(something, str):
        return something.encode(encoding)
    elif isinstance(something, bytearray):
        return bytes(something)
    else:
        raise TypeError("Not a string or bytes like object")


def sha256(x) -> bytes:
    x = to_bytes(x, 'utf8')
    return bytes(hashlib.sha256(x).digest())


def sha256d(x) -> bytes:
    x = to_bytes(x, 'utf8')
    out = bytes(sha256(sha256(x)))
    return out


def hash_encode(x: bytes) -> str:
    return (x[::-1]).hex()


def rev_hex(s: str) -> str:
    return (bytes.fromhex(s)[::-1]).hex()


def int_to_hex(i: int, length: int = 1) -> str:
    if not isinstance(i, int):
        raise TypeError('{} instead of int'.format(i))
    range_size = pow(256, length)
    if i < -(range_size // 2) or i >= range_size:
        raise OverflowError('cannot convert int {} to hex ({} bytes)'.format(i, length))
    if i < 0:
        # two's complement
        i = range_size + i
    s = hex(i)[2:].rstrip('L')
    s = "0" * (2 * length - len(s)) + s
    return rev_hex(s)


def deserialize_header(s: bytes,
                       height: int,
                       pre_kawpow_header_size: int,
                       post_kawpow_header_size: int,
                       kawpow_activation_ts: int) -> dict:
    if not s:
        raise Exception('No header data')
    if len(s) not in (post_kawpow_header_size, pre_kawpow_header_size):
        raise Exception('Invalid header length: {}'.format(len(s)))

    def hex_to_int(hex):
        return int.from_bytes(hex, byteorder='little')

    h = {'version': hex_to_int(s[0:4]),
         'prev_block_hash': hash_encode(s[4:36]),
         'merkle_root': hash_encode(s[36:68]),
         'timestamp': int(hash_encode(s[68:72]), 16),
         'bits': int(hash_encode(s[72:76]), 16)}
    if h['timestamp'] >= kawpow_activation_ts:
        h['nheight'] = int(hash_encode(s[76:80]), 16)
        h['nonce'] = int(hash_encode(s[80:88]), 16)
        h['mix_hash'] = hash_encode(s[88:120])
    else:
        h['nonce'] = int(hash_encode(s[76:80]), 16)
    h['block_height'] = height
    return h


def serialize_header(header_dict: dict,
                     kawpow_activation_ts: int,
                     post_kawpow_header_size: int) -> str:
    ts = header_dict['timestamp']
    if ts >= kawpow_activation_ts:
        s = int_to_hex(header_dict['version'], 4) \
            + rev_hex(header_dict['prev_block_hash']) \
            + rev_hex(header_dict['merkle_root']) \
            + int_to_hex(int(header_dict['timestamp']), 4) \
            + int_to_hex(int(header_dict['bits']), 4) \
            + int_to_hex(int(header_dict['nheight']), 4) \
            + int_to_hex(int(header_dict['nonce']), 8) \
            + rev_hex(header_dict['mix_hash'])
    else:
        s = int_to_hex(header_dict['version'], 4) \
            + rev_hex(header_dict['prev_block_hash']) \
            + rev_hex(header_dict['merkle_root']) \
            + int_to_hex(int(header_dict['timestamp']), 4) \
            + int_to_hex(int(header_dict['bits']), 4) \
            + int_to_hex(int(header_dict['nonce']), 4)
        s = s.ljust(post_kawpow_header_size * 2, '0')  # pad with zeros to post kawpow header size
    return s


def hash_raw_header(header: str) -> str:
    raw_hash = x16r_hash.getPoWHash(bytes.fromhex(header)[:80])
    hash_result = hash_encode(raw_hash)
    return hash_result


def hash_raw_header_v2(header: str) -> str:
    raw_hash = x16rv2_hash.getPoWHash(bytes.fromhex(header)[:80])
    hash_result = hash_encode(raw_hash)
    return hash_result


def revb(data):
    b = bytearray(data)
    b.reverse()
    return bytes(b)


def kawpow_hash(hdr_bin):
    header_hash = revb(sha256d(hdr_bin[:80]))
    mix_hash = revb(hdr_bin[88:120])
    nNonce64 = struct.unpack("< Q", hdr_bin[80:88])[0]
    final_hash = revb(kawpow.light_verify(header_hash, mix_hash, nNonce64))
    return final_hash


def hash_raw_header_kawpow(header: str) -> str:
    final_hash = hash_encode(kawpow_hash(bytes.fromhex(header)))
    return final_hash


def hash_header(header: dict,
                kawpow_activation_ts: int,
                x16rv2_activation_ts: int,
                post_kawpow_header_size: int
                ) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00' * 32
    if header['timestamp'] >= kawpow_activation_ts:
        return hash_raw_header_kawpow(serialize_header(header, kawpow_activation_ts, post_kawpow_header_size))
    elif header['timestamp'] >= x16rv2_activation_ts:
        hdr = serialize_header(header, kawpow_activation_ts, post_kawpow_header_size)[:80 * 2]
        h = hash_raw_header_v2(hdr)
        return h
    else:
        hdr = serialize_header(header, kawpow_activation_ts, post_kawpow_header_size)[:80 * 2]
        h = hash_raw_header(hdr)
        return h


def convbignum(bits):
    MM = 256 * 256 * 256
    a = bits % MM
    if a < 0x8000:
        a *= 256
    target = a * pow(2, 8 * (bits // MM - 3))
    return target


def get_target_dgwv3(height: int, dgw_past_blocks: int, max_target: int, chain: dict) -> int:
    # params
    BlockReading = chain[height - 1]
    nActualTimespan = 0
    LastBlockTime = 0
    PastBlocksMin = dgw_past_blocks
    PastBlocksMax = dgw_past_blocks
    CountBlocks = 0
    PastDifficultyAverage = 0
    PastDifficultyAveragePrev = 0

    for i in range(PastBlocksMax):
        CountBlocks += 1

        if CountBlocks <= PastBlocksMin:
            if CountBlocks == 1:
                PastDifficultyAverage = convbignum(BlockReading.get('bits'))
            else:
                bnNum = convbignum(BlockReading.get('bits'))
                PastDifficultyAverage = ((PastDifficultyAveragePrev * CountBlocks) + (bnNum)) // (CountBlocks + 1)
            PastDifficultyAveragePrev = PastDifficultyAverage

        if LastBlockTime > 0:
            Diff = (LastBlockTime - BlockReading.get('timestamp'))
            nActualTimespan += Diff
        LastBlockTime = BlockReading.get('timestamp')

        BlockReading = chain[(height - 1) - CountBlocks]

    bnNew = PastDifficultyAverage
    nTargetTimespan = CountBlocks * 60  # 1 min

    nActualTimespan = max(nActualTimespan, nTargetTimespan // 3)
    nActualTimespan = min(nActualTimespan, nTargetTimespan * 3)

    # retarget
    bnNew *= nActualTimespan
    bnNew //= nTargetTimespan
    bnNew = min(bnNew, max_target)

    return bnNew


def get_target(is_testnet: bool,
               height: int,
               checkpoints: dict,
               dgw_activation_block: int,
               kawpow_limit: int,
               dgw_past_blocks: int,
               max_target: int,
               chain=None) -> int:
    if is_testnet:
        return 0
    elif height < dgw_activation_block:
        h, t = checkpoints[height // 2016]
        return t
    # There was a difficulty reset for kawpow
    elif not is_testnet and height in range(1219736, 1219736 + 180):  # kawpow reset
        return kawpow_limit
    # Look-back un-needed for checking a chunk
    else:
        # Now we no longer have cached checkpoints and need to compute our own DWG targets to verify
        # a header
        return get_target_dgwv3(height, dgw_past_blocks, max_target, chain)


def target_to_bits(target: int) -> int:
    c = ("%064x" % target)[2:]
    while c[:2] == '00' and len(c) > 6:
        c = c[2:]
    bitsN, bitsBase = len(c) // 2, int.from_bytes(bytes.fromhex(c[:6]), byteorder='big')
    if bitsBase >= 0x800000:
        bitsN += 1
        bitsBase >>= 8
    return bitsN << 24 | bitsBase


def verify_header(header: dict,
                  is_testnet: bool,
                  kawpow_activation_ts: int,
                  x16rv2_activation_ts: int,
                  post_kawpow_header_size: int,
                  prev_hash: str,
                  target: int,
                  expected_header_hash: str = None) -> None:
    _hash = hash_header(header,
                        kawpow_activation_ts,
                        x16rv2_activation_ts,
                        post_kawpow_header_size)

    if expected_header_hash and expected_header_hash != _hash:
        raise Exception("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
    if prev_hash != header.get('prev_block_hash'):
        raise Exception("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
    if is_testnet:
        return
    bits = target_to_bits(target)
    if bits != header.get('bits'):
        raise Exception("bits mismatch: %s vs %s" % (bits, header.get('bits')))
    if header['timestamp'] >= kawpow_activation_ts:
        hash_func = kawpow_hash
    elif header['timestamp'] >= x16rv2_activation_ts:
        hash_func = x16rv2_hash.getPoWHash
    else:
        hash_func = x16r_hash.getPoWHash
    _powhash = rev_hex((hash_func(bytes.fromhex(serialize_header(header,
                                                                 kawpow_activation_ts,
                                                                 post_kawpow_header_size)))).hex())
    if int('0x' + _powhash, 16) > target:
        raise Exception("insufficient proof of work: %s vs target %s" % (int('0x' + _powhash, 16), target))


def _verify_chunk(is_testnet: bool,
                  kawpow_activation_height: int,
                  dgw_activation_height: int,
                  kawpow_activation_ts: int,
                  x16rv2_activation_ts: int,
                  pre_kawpow_header_size: int,
                  post_kawpow_header_size: int,
                  prev_dgw_n_headers: dict,
                  dgw_past_blocks: int,
                  kawpow_limit: int,
                  max_target: int,
                  start_height: int,
                  data: bytes,
                  checkpoints: dict) -> None:
    raw = []
    p = 0
    s = start_height
    prev_dgw_n_hashes = {h: hash_header(d,
                                        kawpow_activation_ts,
                                        x16rv2_activation_ts,
                                        post_kawpow_header_size) for h, d in prev_dgw_n_headers.items()}
    prev_hash = prev_dgw_n_hashes.get(start_height - 1, None)
    if not prev_hash:
        raise Exception(f'No previous hash! (Wanted {start_height - 1})')
    while p < len(data):
        if s < kawpow_activation_height:
            raw = data[p:p + pre_kawpow_header_size]
            p += pre_kawpow_header_size
        else:
            raw = data[p:p + post_kawpow_header_size]
            p += post_kawpow_header_size

        expected_header_hash = prev_dgw_n_hashes.get(s, None)
        header = deserialize_header(raw, s, pre_kawpow_header_size, post_kawpow_header_size, kawpow_activation_ts)
        prev_dgw_n_headers[header.get('block_height')] = header

        target = get_target(is_testnet,
                            s,
                            checkpoints,
                            dgw_activation_height,
                            kawpow_limit,
                            dgw_past_blocks,
                            max_target,
                            prev_dgw_n_headers)

        verify_header(header,
                      is_testnet,
                      kawpow_activation_ts,
                      x16rv2_activation_ts,
                      post_kawpow_header_size,
                      prev_hash,
                      target,
                      expected_header_hash)

        prev_hash = hash_header(header,
                                kawpow_activation_ts,
                                x16rv2_activation_ts,
                                post_kawpow_header_size)

        s += 1
    if len(raw) not in (post_kawpow_header_size, pre_kawpow_header_size):
        raise Exception('Invalid header length: {}'.format(len(raw)))


queue_in = queue_out = _proc = None


def _verify_process(q_in, q_out):
    for data in iter(q_in.get, 'STOP'):
        try:
            _verify_chunk(
                *data
            )
            q_out.put(None)
        except BaseException as e:
            q_out.put(e)


# Blocking, but shouldn't block
def verify_chunk(data_tup):
    return queue_in.put(data_tup)


# Blocking, will block
def get_verify_result():
    return queue_out.get()


# Blocking, may block if syncing
def end_process():
    queue_in.put('STOP')
    _proc.join()


# Technically not spawning for nix
def spawn_process():
    global queue_in, queue_out, _proc
    multiprocessing.freeze_support()
    queue_in = multiprocessing.Queue()
    queue_out = multiprocessing.Queue()
    _proc = multiprocessing.Process(target=_verify_process, args=(queue_in, queue_out))
    _proc.start()
