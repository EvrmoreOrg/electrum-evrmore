# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import asyncio
import sys
import os
import threading
import time
import struct
from typing import Optional, Dict, Mapping, Sequence

from . import util
from .evrmore import hash_encode, int_to_hex, rev_hex
from .crypto import sha256d
from . import constants
from .util import bfh, bh2u, with_lock
from .simple_config import SimpleConfig
from .logging import get_logger, Logger

try:
    import x16r_hash
    import x16rv2_hash
    import kawpow
except ImportError as e:
    sys.exit("x16r, x16rv2 and kawpow modules are required")

_logger = get_logger(__name__)

MAX_TARGET = 0x00000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
KAWPOW_LIMIT = 0x0000000000ffffffffffffffffffffffffffffffffffffffffffffffffffffff

POST_KAWPOW_HEADER_SIZE = 120  # bytes
PRE_KAWPOW_HEADER_SIZE = 80

DGW_PASTBLOCKS = 180

if constants.net.TESTNET:
    X16Rv2ActivationTS = 1567533600
    KawpowActivationTS = 1585159200
    KawpowActivationHeight = 231544
    nDGWActivationBlock = 1
else:
    X16Rv2ActivationTS = 1569945600
    KawpowActivationTS = 1588788000
    KawpowActivationHeight = 1219736
    nDGWActivationBlock = 338778


def set_constants():
    global X16Rv2ActivationTS, KawpowActivationTS, KawpowActivationHeight, nDGWActivationBlock
    if constants.net.TESTNET:
        X16Rv2ActivationTS = 1567533600
        KawpowActivationTS = 1585159200
        KawpowActivationHeight = 231544
        nDGWActivationBlock = 1
    else:
        X16Rv2ActivationTS = 1569945600
        KawpowActivationTS = 1588788000
        KawpowActivationHeight = 1219736
        nDGWActivationBlock = 338778


class MissingHeader(Exception):
    pass


class InvalidHeader(Exception):
    pass


def serialize_header(header_dict: dict) -> str:
    ts = header_dict['timestamp']
    if ts >= KawpowActivationTS:
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
        s = s.ljust(POST_KAWPOW_HEADER_SIZE * 2, '0')  # pad with zeros to post kawpow header size
    return s


def deserialize_header(s: bytes, height: int) -> dict:
    if not s:
        raise InvalidHeader('Invalid header: {}'.format(s))
    if len(s) not in (POST_KAWPOW_HEADER_SIZE, PRE_KAWPOW_HEADER_SIZE):
        raise InvalidHeader('Invalid header length: {}'.format(len(s)))

    def hex_to_int(hex):
        return int.from_bytes(hex, byteorder='little')

    h = {'version': hex_to_int(s[0:4]),
         'prev_block_hash': hash_encode(s[4:36]),
         'merkle_root': hash_encode(s[36:68]),
         'timestamp': int(hash_encode(s[68:72]), 16),
         'bits': int(hash_encode(s[72:76]), 16)}
    if h['timestamp'] >= KawpowActivationTS:
        h['nheight'] = int(hash_encode(s[76:80]), 16)
        h['nonce'] = int(hash_encode(s[80:88]), 16)
        h['mix_hash'] = hash_encode(s[88:120])
    else:
        h['nonce'] = int(hash_encode(s[76:80]), 16)
    h['block_height'] = height
    return h


def hash_header(header: dict) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00' * 32
    if header['timestamp'] >= KawpowActivationTS:
        return hash_raw_header_kawpow(serialize_header(header))
    elif header['timestamp'] >= X16Rv2ActivationTS:
        hdr = serialize_header(header)[:80 * 2]
        h = hash_raw_header_v2(hdr)
        return h
    else:
        hdr = serialize_header(header)[:80 * 2]
        h = hash_raw_header(hdr)
        return h


def hash_raw_header(header: str) -> str:
    raw_hash = x16r_hash.getPoWHash(bfh(header)[:80])
    hash_result = hash_encode(raw_hash)
    return hash_result


def hash_raw_header_v2(header: str) -> str:
    raw_hash = x16rv2_hash.getPoWHash(bfh(header)[:80])
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
    final_hash = hash_encode(kawpow_hash(bfh(header)))
    return final_hash


# key: blockhash hex at forkpoint
# the chain at some key is the best chain that includes the given hash
blockchains = {}  # type: Dict[str, Blockchain]
blockchains_lock = threading.RLock()  # lock order: take this last; so after Blockchain.lock


def read_blockchains(config: 'SimpleConfig'):
    best_chain = Blockchain(config=config,
                            forkpoint=0,
                            parent=None,
                            forkpoint_hash=constants.net.GENESIS,
                            prev_hash=None)
    blockchains[constants.net.GENESIS] = best_chain
    # consistency checks
    if best_chain.height() > constants.net.max_dgw_checkpoint():
        header_after_cp = best_chain.read_header(constants.net.max_dgw_checkpoint() + 1)
        if not header_after_cp or not best_chain.can_connect(header_after_cp, check_height=False):
            _logger.info("[blockchain] deleting best chain. cannot connect header after last cp to last cp.")
            os.unlink(best_chain.path())
            best_chain.update_size()
    # forks
    fdir = os.path.join(util.get_headers_dir(config), 'forks')
    util.make_dir(fdir)
    # files are named as: fork2_{forkpoint}_{prev_hash}_{first_hash}
    l = filter(lambda x: x.startswith('fork2_') and '.' not in x, os.listdir(fdir))
    l = sorted(l, key=lambda x: int(x.split('_')[1]))  # sort by forkpoint

    def delete_chain(filename, reason):
        _logger.info(f"[blockchain] deleting chain {filename}: {reason}")
        os.unlink(os.path.join(fdir, filename))

    def instantiate_chain(filename):
        __, forkpoint, prev_hash, first_hash = filename.split('_')
        forkpoint = int(forkpoint)
        prev_hash = (64 - len(prev_hash)) * "0" + prev_hash  # left-pad with zeroes
        first_hash = (64 - len(first_hash)) * "0" + first_hash
        # forks below the max checkpoint are not allowed
        if forkpoint <= constants.net.max_dgw_checkpoint() + 2016:
            delete_chain(filename, "deleting fork below max checkpoint")
            return
        # find parent (sorting by forkpoint guarantees it's already instantiated)
        for parent in blockchains.values():
            if parent.check_hash(forkpoint - 1, prev_hash):
                break
        else:
            delete_chain(filename, "cannot find parent for chain")
            return
        b = Blockchain(config=config,
                       forkpoint=forkpoint,
                       parent=parent,
                       forkpoint_hash=first_hash,
                       prev_hash=prev_hash)
        # consistency checks
        h = b.read_header(b.forkpoint)
        if first_hash != hash_header(h):
            delete_chain(filename, "incorrect first hash for chain")
            return
        if not b.parent.can_connect(h, check_height=False):
            delete_chain(filename, "cannot connect chain to parent")
            return
        chain_id = b.get_id()
        assert first_hash == chain_id, (first_hash, chain_id)
        blockchains[chain_id] = b

    for filename in l:
        instantiate_chain(filename)


def get_best_chain() -> 'Blockchain':
    return blockchains[constants.net.GENESIS]


# block hash -> chain work; up to and including that block
_CHAINWORK_CACHE = {
    "0000000000000000000000000000000000000000000000000000000000000000": 0,  # virtual block at height -1
}  # type: Dict[str, int]

if len(constants.net.DGW_CHECKPOINTS) > 0:
    _CHAINWORK_CACHE[constants.net.DGW_CHECKPOINTS[-1][1][0]] = 0  # set start of cache to 0 work

def init_headers_file_for_best_chain():
    b = get_best_chain()
    filename = b.path()
    # We want to start with one less than the checkpoint so we have headers to calculate the new
    # Chainwork from
    length = POST_KAWPOW_HEADER_SIZE * constants.net.max_dgw_checkpoint()
    if not os.path.exists(filename) or os.path.getsize(filename) < length:
        with open(filename, 'wb') as f:
            if length > 0:
                f.seek(length - 1)
                f.write(b'\x00')
        util.ensure_sparse_file(filename)
    with b.lock:
        b.update_size()


class Blockchain(Logger):
    """
    Manages blockchain headers and their verification
    """

    def __init__(self, config: SimpleConfig, forkpoint: int, parent: Optional['Blockchain'],
                 forkpoint_hash: str, prev_hash: Optional[str]):
        assert isinstance(forkpoint_hash, str) and len(forkpoint_hash) == 64, forkpoint_hash
        assert (prev_hash is None) or (isinstance(prev_hash, str) and len(prev_hash) == 64), prev_hash
        # assert (parent is None) == (forkpoint == 0)
        if 0 < forkpoint <= constants.net.max_dgw_checkpoint():
            raise Exception(f"cannot fork below max checkpoint. forkpoint: {forkpoint}")
        Logger.__init__(self)
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
        self.update_size()

    @property
    def checkpoints(self):
        return constants.net.CHECKPOINTS

    @property
    def dgw_checkpoints(self):
        return constants.net.DGW_CHECKPOINTS

    def get_max_child(self) -> Optional[int]:
        children = self.get_direct_children()
        return max([x.forkpoint for x in children]) if children else None

    def get_max_forkpoint(self) -> int:
        """Returns the max height where there is a fork
        related to this chain.
        """
        mc = self.get_max_child()
        return mc if mc is not None else self.forkpoint

    def get_direct_children(self) -> Sequence['Blockchain']:
        with blockchains_lock:
            return list(filter(lambda y: y.parent == self, blockchains.values()))

    def get_parent_heights(self) -> Mapping['Blockchain', int]:
        """Returns map: (parent chain -> height of last common block)"""
        with self.lock, blockchains_lock:
            result = {self: self.height()}
            chain = self
            while True:
                parent = chain.parent
                if parent is None: break
                result[parent] = chain.forkpoint - 1
                chain = parent
            return result

    def get_height_of_last_common_block_with_chain(self, other_chain: 'Blockchain') -> int:
        last_common_block_height = 0
        our_parents = self.get_parent_heights()
        their_parents = other_chain.get_parent_heights()
        for chain in our_parents:
            if chain in their_parents:
                h = min(our_parents[chain], their_parents[chain])
                last_common_block_height = max(last_common_block_height, h)
        return last_common_block_height

    @with_lock
    def get_branch_size(self) -> int:
        return self.height() - self.get_max_forkpoint() + 1

    def get_name(self) -> str:
        return self.get_hash(self.get_max_forkpoint()).lstrip('0')[0:10]

    def check_header(self, header: dict) -> bool:
        header_hash = hash_header(header)
        height = header.get('block_height')
        return self.check_hash(height, header_hash)

    def check_hash(self, height: int, header_hash: str) -> bool:
        """Returns whether the hash of the block at given height
        is the given hash.
        """
        assert isinstance(header_hash, str) and len(header_hash) == 64, header_hash  # hex
        try:
            return header_hash == self.get_hash(height)
        except Exception:
            return False

    def fork(parent, header: dict) -> 'Blockchain':
        if not parent.can_connect(header, check_height=False):
            raise Exception("forking header does not connect to parent chain")
        forkpoint = header.get('block_height')
        self = Blockchain(config=parent.config,
                          forkpoint=forkpoint,
                          parent=parent,
                          forkpoint_hash=hash_header(header),
                          prev_hash=parent.get_hash(forkpoint - 1))
        self.assert_headers_file_available(parent.path())
        open(self.path(), 'w+').close()
        self.save_header(header)
        # put into global dict. note that in some cases
        # save_header might have already put it there but that's OK
        chain_id = self.get_id()
        with blockchains_lock:
            blockchains[chain_id] = self
        return self

    @with_lock
    def height(self) -> int:
        return self.forkpoint + self.size() - 1

    @with_lock
    def size(self) -> int:
        return self._size

    @with_lock
    def update_size(self) -> None:
        p = self.path()
        self._size = os.path.getsize(p) // POST_KAWPOW_HEADER_SIZE if os.path.exists(p) else 0

    @classmethod
    def verify_header(cls, header: dict, prev_hash: str, target: int, expected_header_hash: str = None) -> None:
        _hash = hash_header(header)
        if expected_header_hash and expected_header_hash != _hash:
            raise Exception("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
        if prev_hash != header.get('prev_block_hash'):
            raise Exception("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
        if constants.net.TESTNET:
            return
        bits = cls.target_to_bits(target)
        if bits != header.get('bits'):
            raise Exception("bits mismatch: %s vs %s" % (bits, header.get('bits')))
        if header['timestamp'] >= KawpowActivationTS:
            hash_func = kawpow_hash
        elif header['timestamp'] >= X16Rv2ActivationTS:
            hash_func = x16rv2_hash.getPoWHash
        else:
            hash_func = x16r_hash.getPoWHash
        _powhash = rev_hex(bh2u(hash_func(bfh(serialize_header(header)))))
        if int('0x' + _powhash, 16) > target:
            raise Exception("insufficient proof of work: %s vs target %s" % (int('0x' + _powhash, 16), target))

    def verify_chunk(self, start_height: int, data: bytes) -> None:
        raw = []
        p = 0
        s = start_height
        prev_hash = self.get_hash(start_height - 1)
        headers = {}
        while p < len(data):
            if s < KawpowActivationHeight:
                raw = data[p:p + PRE_KAWPOW_HEADER_SIZE]
                p += PRE_KAWPOW_HEADER_SIZE
            else:
                raw = data[p:p + POST_KAWPOW_HEADER_SIZE]
                p += POST_KAWPOW_HEADER_SIZE
            try:
                expected_header_hash = self.get_hash(s)
            except MissingHeader:
                expected_header_hash = None
            if len(raw) not in (POST_KAWPOW_HEADER_SIZE, PRE_KAWPOW_HEADER_SIZE):
                raise Exception('Invalid header length: {}'.format(len(raw)))
            header = deserialize_header(raw, s)
            headers[header.get('block_height')] = header
            
            # Don't bother with the target of headers in the middle of
            # DGW checkpoints
            target = 0
            if constants.net.DGW_CHECKPOINTS_START <= s <= constants.net.max_dgw_checkpoint() + 2016:
                if self.is_dgw_height_checkpoint(s) is not None:
                    target = self.get_target(s, headers)
                else:
                    # Just use the headers own bits for the logic
                    target = self.bits_to_target(header['bits'])
            else:
                target = self.get_target(s, headers)
            
            self.verify_header(header, prev_hash, target, expected_header_hash)
            prev_hash = hash_header(header)
            s += 1

        # DGW must be received in correct chunk sizes to be valid with our checkpoints
        if constants.net.DGW_CHECKPOINTS_START <= start_height <= constants.net.max_dgw_checkpoint():
            assert start_height % constants.net.DGW_CHECKPOINTS_SPACING == 0, 'dgw chunk not from start'
            assert s - start_height == constants.net.DGW_CHECKPOINTS_SPACING, 'dgw chunk not correct size'

    @with_lock
    def path(self):
        d = util.get_headers_dir(self.config)
        if self.parent is None:
            filename = 'blockchain_headers'
        else:
            assert self.forkpoint > 0, self.forkpoint
            prev_hash = self._prev_hash.lstrip('0')
            first_hash = self._forkpoint_hash.lstrip('0')
            basename = f'fork2_{self.forkpoint}_{prev_hash}_{first_hash}'
            filename = os.path.join('forks', basename)
        return os.path.join(d, filename)

    @with_lock
    def save_chunk(self, start_height: int, chunk: bytes):
        assert start_height >= 0, start_height
        chunk_within_checkpoint_region = start_height < constants.net.max_dgw_checkpoint() + 2016
        # chunks in checkpoint region are the responsibility of the 'main chain'
        if chunk_within_checkpoint_region and self.parent is not None:
            main_chain = get_best_chain()
            main_chain.save_chunk(start_height, chunk)
            return

        delta_height = (start_height - self.forkpoint)
        delta_bytes = delta_height * POST_KAWPOW_HEADER_SIZE
        # if this chunk contains our forkpoint, only save the part after forkpoint
        # (the part before is the responsibility of the parent)
        if delta_bytes < 0:
            chunk = chunk[-delta_bytes:]
            delta_bytes = 0
        truncate = not chunk_within_checkpoint_region

        def convert_to_kawpow_len():
            r = b''
            p = 0
            s = start_height
            while p < len(chunk):
                if s < KawpowActivationHeight:
                    r += chunk[p:p + PRE_KAWPOW_HEADER_SIZE] + bytes(40)
                    p += PRE_KAWPOW_HEADER_SIZE
                else:
                    r += chunk[p:p + POST_KAWPOW_HEADER_SIZE]
                    p += POST_KAWPOW_HEADER_SIZE
                s += 1
            if len(r) % POST_KAWPOW_HEADER_SIZE != 0:
                raise Exception('Header extension error')
            return r

        chunk = convert_to_kawpow_len()
        self.write(chunk, delta_bytes, truncate)
        assert self.read_header(start_height) == deserialize_header(chunk[:120], start_height)
        self.swap_with_parent()

    def swap_with_parent(self) -> None:
        with self.lock, blockchains_lock:
            # do the swap; possibly multiple ones
            cnt = 0
            while True:
                old_parent = self.parent
                if not self._swap_with_parent():
                    break
                # make sure we are making progress
                cnt += 1
                if cnt > len(blockchains):
                    raise Exception(f'swapping fork with parent too many times: {cnt}')
                # we might have become the parent of some of our former siblings
                for old_sibling in old_parent.get_direct_children():
                    if self.check_hash(old_sibling.forkpoint - 1, old_sibling._prev_hash):
                        old_sibling.parent = self

    def _swap_with_parent(self) -> bool:
        """Check if this chain became stronger than its parent, and swap
        the underlying files if so. The Blockchain instances will keep
        'containing' the same headers, but their ids change and so
        they will be stored in different files."""
        if self.parent is None:
            return False
        if self.parent.get_chainwork() >= self.get_chainwork():
            return False
        self.logger.info(f"swapping {self.forkpoint} {self.parent.forkpoint}")
        parent_branch_size = self.parent.height() - self.forkpoint + 1
        forkpoint = self.forkpoint  # type: Optional[int]
        parent = self.parent  # type: Optional[Blockchain]
        child_old_id = self.get_id()
        parent_old_id = parent.get_id()
        # swap files
        # child takes parent's name
        # parent's new name will be something new (not child's old name)
        self.assert_headers_file_available(self.path())
        child_old_name = self.path()
        with open(self.path(), 'rb') as f:
            my_data = f.read()
        self.assert_headers_file_available(parent.path())
        assert forkpoint > parent.forkpoint, (f"forkpoint of parent chain ({parent.forkpoint}) "
                                              f"should be at lower height than children's ({forkpoint})")
        with open(parent.path(), 'rb') as f:
            f.seek((forkpoint - parent.forkpoint) * POST_KAWPOW_HEADER_SIZE)
            parent_data = f.read(parent_branch_size * POST_KAWPOW_HEADER_SIZE)
        self.write(parent_data, 0)
        parent.write(my_data, (forkpoint - parent.forkpoint) * POST_KAWPOW_HEADER_SIZE)
        # swap parameters
        self.parent, parent.parent = parent.parent, self
        self.forkpoint, parent.forkpoint = parent.forkpoint, self.forkpoint
        self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_raw_header(
            bh2u(parent_data[:POST_KAWPOW_HEADER_SIZE]))
        self._prev_hash, parent._prev_hash = parent._prev_hash, self._prev_hash
        # parent's new name
        os.replace(child_old_name, parent.path())
        self.update_size()
        parent.update_size()
        # update pointers
        blockchains.pop(child_old_id, None)
        blockchains.pop(parent_old_id, None)
        blockchains[self.get_id()] = self
        blockchains[parent.get_id()] = parent
        return True

    def get_id(self) -> str:
        return self._forkpoint_hash

    def assert_headers_file_available(self, path):
        if os.path.exists(path):
            return
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise FileNotFoundError('Electrum headers_dir does not exist. Was it deleted while running?')
        else:
            raise FileNotFoundError('Cannot find headers file but headers_dir is there. Should be at {}'.format(path))

    @with_lock
    def write(self, data: bytes, offset: int, truncate: bool = True) -> None:
        filename = self.path()
        self.assert_headers_file_available(filename)
        with open(filename, 'rb+') as f:
            if truncate and offset != self._size * POST_KAWPOW_HEADER_SIZE:
                f.seek(offset)
                f.truncate()
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.update_size()

    @with_lock
    def save_header(self, header: dict) -> None:
        delta = header.get('block_height') - self.forkpoint
        data = bfh(serialize_header(header))
        # headers are only _appended_ to the end:
        assert delta == self.size(), (delta, self.size())
        assert len(data) == POST_KAWPOW_HEADER_SIZE
        self.write(data, delta * POST_KAWPOW_HEADER_SIZE)
        self.swap_with_parent()

    @with_lock
    def read_header(self, height: int) -> Optional[dict]:
        if height < 0:
            return
        if height < self.forkpoint:
            return self.parent.read_header(height)
        if height > self.height():
            return
        delta = height - self.forkpoint
        name = self.path()
        self.assert_headers_file_available(name)
        with open(name, 'rb') as f:
            f.seek(delta * POST_KAWPOW_HEADER_SIZE)
            h = f.read(POST_KAWPOW_HEADER_SIZE)
            if len(h) < POST_KAWPOW_HEADER_SIZE:
                raise Exception('Expected to read a full header. This was only {} bytes'.format(len(h)))
        if h == bytes([0]) * POST_KAWPOW_HEADER_SIZE:
            return None
        return deserialize_header(h, height)

    def header_at_tip(self) -> Optional[dict]:
        """Return latest header."""
        height = self.height()
        return self.read_header(height)

    def is_tip_stale(self) -> bool:
        STALE_DELAY = 8 * 60 * 60  # in seconds
        header = self.header_at_tip()
        if not header:
            return True
        # note: We check the timestamp only in the latest header.
        #       The Bitcoin consensus has a lot of leeway here:
        #       - needs to be greater than the median of the timestamps of the past 11 blocks, and
        #       - up to at most 2 hours into the future compared to local clock
        #       so there is ~2 hours of leeway in either direction
        if header['timestamp'] + STALE_DELAY < time.time():
            return True
        return False

    @staticmethod
    def is_dgw_height_checkpoint(height) -> Optional[int]:
        # Less than the start of saved checkpoints
        if height < constants.net.DGW_CHECKPOINTS_START:
            return None
        # Greater than the end of the saved checkpoints
        if height > constants.net.max_dgw_checkpoint() + 2016:
            return None
        height_mod = height % constants.net.DGW_CHECKPOINTS_SPACING
        # Is the first saved
        if height_mod == 0:
            return 0
        # Is the last saved
        elif height_mod == constants.net.DGW_CHECKPOINTS_SPACING - 1:
            return 1
        return None

    def get_hash(self, height: int) -> str:
        def is_height_checkpoint():
            within_cp_range = height <= nDGWActivationBlock
            at_chunk_boundary = (height + 1) % 2016 == 0
            return within_cp_range and at_chunk_boundary

        dgw_height_checkpoint = self.is_dgw_height_checkpoint(height)

        if height == -1:
            return '0000000000000000000000000000000000000000000000000000000000000000'
        elif height == 0:
            return constants.net.GENESIS
        elif is_height_checkpoint():
            index = height // 2016
            h, t = self.checkpoints[index]
            return h
        elif dgw_height_checkpoint is not None:
            index = height // constants.net.DGW_CHECKPOINTS_SPACING - constants.net.DGW_CHECKPOINTS_START // constants.net.DGW_CHECKPOINTS_SPACING
            h, t = self.dgw_checkpoints[index][dgw_height_checkpoint]
            return h
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            return hash_header(header)

    def get_target(self, height: int, chain=None) -> int:
        dgw_height_checkpoint = self.is_dgw_height_checkpoint(height)

        if constants.net.TESTNET:
            return 0
        # Before we switched to Dark Wave Gravity Difficulty,
        # We used bitcoin's method of calculating difficulty.
        # The bits of each block (the difficulty) was the same for
        # The entire 2016 block checkpoint. Note that the last block hash to target
        # pairing in checkpoints.json
        # "000000000000f0bf1b393ef1dbbf23421eba2ad09de6315dcfaabe106fcf9e7a",
        # 2716428330192056873911465544471964056901126523302699863524769792
        # is technically incorrect but necessary due to DGW activating
        # in the middle of that chunk.
        elif height < nDGWActivationBlock:
            h, t = self.checkpoints[height // 2016]
            return t
        elif dgw_height_checkpoint is not None:
            index = height // constants.net.DGW_CHECKPOINTS_SPACING - constants.net.DGW_CHECKPOINTS_START // constants.net.DGW_CHECKPOINTS_SPACING
            h, t = self.dgw_checkpoints[index][dgw_height_checkpoint]
            return t
        # There was a difficulty reset for kawpow
        elif not constants.net.TESTNET and height in range(1219736, 1219736 + 180):  # kawpow reset
            return KAWPOW_LIMIT
        # If we have a DWG header already saved to our header cache (i.e. for a reorg), get that
        elif height <= self.height():
            return self.bits_to_target(self.read_header(height)['bits'])
        else:
            # Now we no longer have cached checkpoints and need to compute our own DWG targets to verify
            # a header
            return self.get_target_dgwv3(height, chain)

    def convbignum(self, bits):
        MM = 256 * 256 * 256
        a = bits % MM
        if a < 0x8000:
            a *= 256
        target = a * pow(2, 8 * (bits // MM - 3))
        return target

    def get_target_dgwv3(self, height, chain=None) -> int:

        def get_block_reading_from_height(height):
            last = None
            try:
                last = chain.get(height)
            except:
                pass
            if last is None:
                last = self.read_header(height)
                assert last is not None
            return last

        # params
        BlockReading = get_block_reading_from_height(height - 1)
        nActualTimespan = 0
        LastBlockTime = 0
        PastBlocksMin = DGW_PASTBLOCKS
        PastBlocksMax = DGW_PASTBLOCKS
        CountBlocks = 0
        PastDifficultyAverage = 0
        PastDifficultyAveragePrev = 0

        for i in range(PastBlocksMax):
            CountBlocks += 1

            if CountBlocks <= PastBlocksMin:
                if CountBlocks == 1:
                    PastDifficultyAverage = self.convbignum(BlockReading.get('bits'))
                else:
                    bnNum = self.convbignum(BlockReading.get('bits'))
                    PastDifficultyAverage = ((PastDifficultyAveragePrev * CountBlocks) + (bnNum)) // (CountBlocks + 1)
                PastDifficultyAveragePrev = PastDifficultyAverage

            if LastBlockTime > 0:
                Diff = (LastBlockTime - BlockReading.get('timestamp'))
                nActualTimespan += Diff
            LastBlockTime = BlockReading.get('timestamp')

            BlockReading = get_block_reading_from_height((height - 1) - CountBlocks)

        bnNew = PastDifficultyAverage
        nTargetTimespan = CountBlocks * 60  # 1 min

        nActualTimespan = max(nActualTimespan, nTargetTimespan // 3)
        nActualTimespan = min(nActualTimespan, nTargetTimespan * 3)

        # retarget
        bnNew *= nActualTimespan
        bnNew //= nTargetTimespan
        bnNew = min(bnNew, MAX_TARGET)

        return bnNew

    @classmethod
    def bits_to_target(cls, bits: int) -> int:
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

    @classmethod
    def target_to_bits(cls, target: int) -> int:
        # arith_uint256::GetCompact in Bitcoin Core
        # see https://github.com/bitcoin/bitcoin/blob/7fcf53f7b4524572d1d0c9a5fdc388e87eb02416/src/arith_uint256.cpp#L223
        c = target.to_bytes(length=32, byteorder='big')
        bitsN = len(c)
        while bitsN > 0 and c[0] == 0:
            c = c[1:]
            bitsN -= 1
            if len(c) < 3:
                c += b'\x00'
        bitsBase = int.from_bytes(c[:3], byteorder='big')
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase

    def chainwork_of_header_at_height(self, height: int) -> int:
        """work done by single header at given height"""
        target = self.get_target(height)
        work = ((2 ** 256 - target - 1) // (target + 1)) + 1
        return work

    # This works theoretically
    # TODO: Take a better look at this for DWG
    @with_lock
    def get_chainwork(self, height=None) -> int:

        if height is None:
            height = max(0, self.height())
        if constants.net.TESTNET:
            # On testnet/regtest, difficulty works somewhat different.
            # It's out of scope to properly implement that.
            return height

        # With DGW checkpoints, we cannot determine the work on the blocks between
        # Since we cannot fork below the checkpoints, lets just set chainwork under them
        # to 0
        if height < constants.net.max_dgw_checkpoint() + 2016:
            return 0

        # We want to calculate chainwork from 0.
        # Lets use bitcoin chunks for arbitrary checkpoints
        last_retarget = height // 2016 * 2016 - 1
        cached_height = last_retarget
        # First get the last cached chainwork values we might have (0 if none)
        while _CHAINWORK_CACHE.get(self.get_hash(cached_height)) is None:
            if cached_height <= -1:
                break
            cached_height -= 2016
        assert cached_height >= -1, cached_height
        running_total = _CHAINWORK_CACHE[self.get_hash(cached_height)]
        # Calculate the chainwork in chunks if needed for caching
        while cached_height < last_retarget:
            work_in_chunk = 0
            for i in range(2016):
                cached_height += 1
                work_in_chunk += self.chainwork_of_header_at_height(cached_height)
            running_total += work_in_chunk
            _CHAINWORK_CACHE[self.get_hash(cached_height)] = running_total
        # Calculate the remaining chainwork
        work_in_last_partial_chunk = 0
        while cached_height < height:
            cached_height += 1
            work_in_last_partial_chunk += self.chainwork_of_header_at_height(cached_height)
        return running_total + work_in_last_partial_chunk

    def can_connect(self, header: dict, check_height: bool = True) -> bool:
        if header is None:
            return False
        height = header['block_height']
        if check_height and self.height() != height - 1:
            return False
        if height == 0:
            return hash_header(header) == constants.net.GENESIS
        try:
            prev_hash = self.get_hash(height - 1)
        except:
            return False
        if prev_hash != header.get('prev_block_hash'):
            return False
        headers = {header.get('block_height'): header}
        try:
            target = self.get_target(height, headers)
        except MissingHeader:
            return False
        try:
            self.verify_header(header, prev_hash, target)
        except BaseException as e:
            return False
        return True

    async def connect_chunk(self, start_height: int, hexdata: str) -> bool:
        assert start_height >= 0, start_height
        try:
            data = bfh(hexdata)

            # This is computationally intensive (thanks DGW)
            self.verify_chunk(start_height, data)
            
            self.save_chunk(start_height, data)
            return True
        except BaseException as e:
            self.logger.info(f'verify_chunk from height {start_height} failed: {repr(e)}')
            return False


def check_header(header: dict) -> Optional[Blockchain]:
    """Returns any Blockchain that contains header, or None."""
    if type(header) is not dict:
        return None
    with blockchains_lock:
        chains = list(blockchains.values())
    for b in chains:
        if b.check_header(header):
            return b
    return None


def can_connect(header: dict) -> Optional[Blockchain]:
    """Returns the Blockchain that has a tip that directly links up
    with header, or None.
    """
    with blockchains_lock:
        chains = list(blockchains.values())
    for b in chains:
        if b.can_connect(header):
            return b
    return None


def get_chains_that_contain_header(height: int, header_hash: str) -> Sequence[Blockchain]:
    """Returns a list of Blockchains that contain header, best chain first."""
    with blockchains_lock: chains = list(blockchains.values())
    chains = [chain for chain in chains
              if chain.check_hash(height=height, header_hash=header_hash)]
    chains = sorted(chains, key=lambda x: x.get_chainwork(), reverse=True)
    return chains
