#!/usr/bin/env python
from decimal import Decimal
import re
from typing import Dict, Union, Optional, Tuple

from .logging import get_logger
from .ravencoin import opcodes, push_script, base_encode, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC, COIN, base_decode
from . import transaction
from .util import Satoshis
from .i18n import _

DOUBLE_PUNCTUATION = "^.*[._]{2,}.*$"
LEADING_PUNCTUATION = "^[._].*$"
TRAILING_PUNCTUATION = "^.*[._]$"
RAVEN_NAMES = "^RVN$|^RAVEN$|^RAVENCOIN$|^#RVN$|^#RAVEN$|^#RAVENCOIN$"

MAIN_CHECK = "^[A-Z0-9._]{3,}$"
SUB_CHECK = "^[A-Z0-9._]+$"
UNIQUE_CHECK = "^[-A-Za-z0-9@$%&*()[\\]{}_.?:]+$"

_logger = get_logger(__name__)


# This is used in the asset_workspace to make place-holders for the coin-chooser
# We add null-bytes to the beginning to approximate a full-size tx for the coin-chooser

P2PKH_SCRIPT_SIZE = 25

def GENERATE_OWNERSHIP_PLACEHOLDER(grouping: int, asset: str) -> bytes:
    return create_owner_asset_script(bytes([grouping] * P2PKH_SCRIPT_SIZE), asset)

def GENERATE_TRANSFER_PLACEHOLDER(grouping: int, asset: str, amount: int, memo: Optional[bytes], expiry: Optional[int]):
    return create_transfer_asset_script(bytes([grouping] * P2PKH_SCRIPT_SIZE), asset, amount, memo=memo, expiry=expiry)

def GENERATE_NEW_PLACEHOLDER(grouping: int, asset: str, amount: int, divisions: int, reissuable: bool, data: Optional[bytes]):
    return create_new_asset_script(bytes([grouping] * P2PKH_SCRIPT_SIZE), asset, amount, divisions, reissuable, data)

def GENERATE_REISSUE_PLACEHOLDER(grouping: int, asset: str, amount: int, divisions: int, reissuable: bool, data: Optional[bytes]):
    return create_reissue_asset_script(bytes([grouping] * P2PKH_SCRIPT_SIZE), asset, amount, bytes([divisions]), reissuable, data)


class BadAssetScript(Exception): pass


def is_name_valid(name: str) -> bool:
    if len(name) < 3 or len(name) > 31:
        return False
    if name[0] == '$':
        # Restricted asset; only main
        return is_main_asset_name_good(name[1:]) is None
    elif name[0] == '#':
        # Qualifier asset; can have sub, no ownership
        strs = name.split('/')
        good = True
        for s in strs:
            if not good:
                break
            good = is_main_asset_name_good(s[1:]) is None
        return good
    else:
        # Normal
        if name[-1] == '!':
            # Remove ownership (this is a valid string)
            name = name[:-1]
        subs = name.split('/')
        good = is_main_asset_name_good(subs[0]) is None
        if not good or len(subs) < 2:
            return good
        for sub in subs[1:-1]:
            if not good:
                break
            good = is_sub_asset_name_good(sub) is None
        if not good:
            return good
        subs2 = subs[-1].split('#')
        if len(subs2) < 2:
            return is_sub_asset_name_good(subs2[0]) is None
        elif len(subs2) == 2:
            return is_sub_asset_name_good(subs2[0]) is None and \
                is_unique_asset_name_good(subs2[1]) is None
        else:
            # Too many #
            return False

def get_asset_vout_type(script: bytes) -> Optional[str]:
    if script[-1] != 0x75:
        return None
    ops = transaction.script_GetOp(script)
    rvn_ptr = -1
    for op, l, ptr in ops:
        if op == opcodes.OP_RVN_ASSET:
            rvn_ptr = ptr - 1
            break
    if not rvn_ptr > 0:
        return None
    if script[rvn_ptr+2:rvn_ptr+5] == b'rvn':
        rvn_ptr += 5
    else:
        rvn_ptr += 6
    type = bytes([script[rvn_ptr]])

    if type == b't':
        return _('Transfer')
    elif type in (b'q', b'o'):
        return _('Creation')
    elif type == b'r':
        return _('Reissue')

    return None
    

def try_get_message_from_asset_transfer(script: bytes) -> Optional[Tuple[str, Optional[int]]]:
    # Returns message, expiry
    if script[-1] != 0x75:
        return None
    ops = transaction.script_GetOp(script)
    rvn_ptr = -1
    for op, _, ptr in ops:
        if op == opcodes.OP_RVN_ASSET:
            rvn_ptr = ptr - 1
            break
    if not rvn_ptr > 0:
        return None
    if script[rvn_ptr+2:rvn_ptr+5] == b'rvn':
        rvn_ptr += 5
    else:
        rvn_ptr += 6
    type = bytes([script[rvn_ptr]])
    if type != b't':
        return None
    rvn_ptr += 1
    name_len = script[rvn_ptr]
    ipfs = None
    timestamp = None
    try:
        if script[rvn_ptr+1+name_len+8] != 0xff:
            ipfs = script[rvn_ptr+1+name_len+8:rvn_ptr+1+name_len+8+34]
        if script[rvn_ptr+1+name_len+8+34] != 0xff:
            timestamp = script[rvn_ptr+1+name_len+8+34:rvn_ptr+1+name_len+8+34+8]
    except Exception as e:
        return None

    if ipfs:
        ipfs = base_encode(ipfs, base=58)
    if timestamp:
        timestamp = int.from_bytes(timestamp, 'little')

    if ipfs:
        return [ipfs, timestamp]
    return None

def replace_amount_in_transfer_asset_script(script: bytes, amount: int) -> bytes:
    if script[-1] != 0x75:
        raise BadAssetScript('No OP_DROP')
    ops = transaction.script_GetOp(script)
    rvn_ptr = -1
    for op, _, ptr in ops:
        if op == opcodes.OP_RVN_ASSET:
            rvn_ptr = ptr - 1
            break
    if not rvn_ptr > 0:
        raise BadAssetScript('No OP_RVN_ASSET')
    if script[rvn_ptr+2:rvn_ptr+5] == b'rvn':
        rvn_ptr += 5
    else:
        rvn_ptr += 6
    type = bytes([script[rvn_ptr]])
    if type != b't':
        raise BadAssetScript('Not an asset transfer script')
    rvn_ptr += 1
    name_len = script[rvn_ptr]
    pre_sats = script[:rvn_ptr+1+name_len]
    post_sats = script[rvn_ptr+1+name_len+8:]
    return pre_sats + amount.to_bytes(8, 'little', signed=False) + post_sats

def pull_meta_from_create_or_reissue_script(script: bytes) -> Dict:
    if script[-1] != 0x75:
        raise BadAssetScript('No OP_DROP')
    ops = transaction.script_GetOp(script)
    rvn_ptr = -1
    for op, _, ptr in ops:
        if op == opcodes.OP_RVN_ASSET:
            rvn_ptr = ptr - 1
            break
    if not rvn_ptr > 0:
        raise BadAssetScript('No OP_RVN_ASSET')
    if script[rvn_ptr+2:rvn_ptr+5] == b'rvn':
        rvn_ptr += 5
    else:
        rvn_ptr += 6
    type = bytes([script[rvn_ptr]])
    if type not in (b'q', b'r', b'o'):
        raise BadAssetScript('Not an asset creation script')

    rvn_ptr += 1
    if type == b'q':
        name_len = script[rvn_ptr]
        name = script[rvn_ptr+1:rvn_ptr+1+name_len]
        sats = script[rvn_ptr+1+name_len:rvn_ptr+1+name_len+8]
        divs = script[rvn_ptr+1+name_len+8]
        reis = script[rvn_ptr+1+name_len+8+1]
        has_i = script[rvn_ptr+1+name_len+8+1+1]
        ifps = None
        if has_i != 0:
            ifps = script[rvn_ptr+1+name_len+8+1+1+1:rvn_ptr+1+name_len+8+1+1+1+34]
        return {
            'name': name.decode('ascii'),
            'sats_in_circulation': int.from_bytes(sats, 'little'),
            'divisions': divs,
            'reissuable': False if reis == 0 else True,
            'has_ipfs': False if has_i == 0 else True,
            'ipfs': base_encode(ifps, base=58) if has_i != 0 else None,
            'type': 'q'
        }
    elif type == b'r':
        name_len = script[rvn_ptr]
        name = script[rvn_ptr + 1:rvn_ptr + 1 + name_len]
        sats = script[rvn_ptr + 1 + name_len:rvn_ptr + 1 + name_len + 8]
        divs = script[rvn_ptr + 1 + name_len + 8]
        reis = script[rvn_ptr + 1 + name_len + 8 + 1]
        ifps = None
        if rvn_ptr + 1 + name_len + 8 + 1 + 1 != len(script) - 1:
            ifps = script[rvn_ptr + 1 + name_len + 8 + 1 + 1:rvn_ptr + 1 + name_len + 8 + 1 + 1 + 34]
        return {
            'name': name.decode('ascii'),
            'sats_in_circulation': int.from_bytes(sats, 'little'),
            'divisions': divs,
            'reissuable': False if reis == 0 else True,
            'has_ipfs': False if not ifps else True,
            'ipfs': base_encode(ifps, base=58) if ifps else None,
            'type': 'r'
        }
    else:
        name_len = script[rvn_ptr]
        name = script[rvn_ptr + 1:rvn_ptr + 1 + name_len]
        return {
            'name': name.decode('ascii'),
            'sats_in_circulation': 100_000_000,
            'divisions': 0,
            'reissuable': False,
            'has_ipfs': False,
            'ipfs': None,
            'type': 'o'
        }


def create_transfer_asset_script(standard: bytes, asset: str, value: Union[int, Satoshis, Decimal, str], *, memo: Optional[bytes] = None, expiry: Optional[int] = None):
    assert not memo or len(memo) == 34
    if expiry:
        assert memo
    if isinstance(value, Satoshis):
        value = value.value
    if isinstance(value, str):
        value = 0
    asset_header = b'rvnt'.hex()
    name = push_script(asset.encode('ascii').hex())
    amt = value.to_bytes(8, byteorder="little", signed=False).hex()
    asset_portion = asset_header+name+amt + (memo.hex() if memo else '') + (expiry.to_bytes(8, 'little', signed=False) if expiry else '')
    return standard + \
        bytes([opcodes.OP_RVN_ASSET]) + \
        bytes.fromhex(push_script(asset_portion)) + \
        bytes([opcodes.OP_DROP])


def create_owner_asset_script(standard: bytes, asset: str):
    assert asset[-1] == '!'
    asset_header = b'rvno'.hex()
    name = push_script(asset.encode('ascii').hex())
    asset_portion = asset_header+name
    return standard + \
        bytes([opcodes.OP_RVN_ASSET]) + \
        bytes.fromhex(push_script(asset_portion)) + \
        bytes([opcodes.OP_DROP])


def create_reissue_asset_script(standard: bytes, asset: str, value: int, divisions: bytes, reissuable: bool, data: bytes):
    assert b'\0' <= divisions <= b'\x08' or divisions == b'\xff'
    assert value <= TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN
    assert isinstance(reissuable, bool)
    assert isinstance(data, bytes) or data is None
    asset_header = b'rvnr'.hex()
    name = push_script(asset.encode('ascii').hex())
    amt = value.to_bytes(8, byteorder='little', signed=False).hex()
    d = divisions.hex()
    r = '01' if reissuable else '00'
    asset_portion = asset_header+name+amt+d+r
    if data:
        asset_portion += data.hex()
    return standard + \
           bytes([opcodes.OP_RVN_ASSET]) + \
           bytes.fromhex(push_script(asset_portion)) + \
           bytes([opcodes.OP_DROP])


def create_new_asset_script(standard: bytes, asset: str, value: int, divisions: int, reissuable: bool, data: bytes):
    assert 0 <= divisions <= 8
    assert value <= TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN
    assert isinstance(reissuable, bool)
    assert isinstance(data, bytes) or data is None
    asset_header = b'rvnq'.hex()
    name = push_script(asset.encode('ascii').hex())
    amt = value.to_bytes(8, byteorder='little', signed=False).hex()
    d = bytes([divisions]).hex()
    r = '01' if reissuable else '00'
    h = '01' if data else '00'
    asset_portion = asset_header+name+amt+d+r+h
    if data:
        asset_portion += data.hex()
    return standard + \
           bytes([opcodes.OP_RVN_ASSET]) + \
           bytes.fromhex(push_script(asset_portion)) + \
           bytes([opcodes.OP_DROP])


def guess_asset_script_for_vin(script: bytes, asset: str, amt: int, txin, wallet) -> str:
    if wallet is None:
        _logger.warning("Using best effort pre-image script for asset: no wallet: {}".format(asset))
        
        script = create_transfer_asset_script(script, asset, amt).hex()
        return script
    else:
        # Ensure we are using the latest meta for mempool spend chaining
        meta = wallet.adb.get_unverified_asset_meta(asset)
        if not meta:
            meta = wallet.adb.get_asset_meta(asset)
        reissue_outpoints = wallet.adb.get_asset_reissue_outpoints(asset)
        
        if not meta:
            _logger.warning("Using best effort pre-image script for asset: no meta: {}".format(asset))
            script = create_transfer_asset_script(script, asset, amt).hex()
            return script
        if txin.prevout.to_str() in reissue_outpoints:
            script = reissue_outpoints[txin.prevout.to_str()]
        else:
            script = create_transfer_asset_script(script, asset, amt).hex()

    return script

def is_main_asset_name_good(name):
    """
    Returns the error as a string or None if good
    """
    if re.search(DOUBLE_PUNCTUATION, name):
        return "There is double punctuation in this main asset name."
    if re.search(LEADING_PUNCTUATION, name):
        return "You cannot begin a main asset with punctuation."
    if re.search(TRAILING_PUNCTUATION, name):
        return "You cannot end a main asset with punctuation."
    if re.search(RAVEN_NAMES, name):
        return "Main assets cannot have Ravencoin-like names."
    if re.search(MAIN_CHECK, name):
        return None
    else:
        return "SIZE"


def is_sub_asset_name_good(name):
    if re.search(DOUBLE_PUNCTUATION, name):
        return "There is double punctuation in this sub asset name."
    if re.search(LEADING_PUNCTUATION, name):
        return "You cannot begin a sub asset with punctuation."
    if re.search(TRAILING_PUNCTUATION, name):
        return "You cannot end a sub asset with punctuation."
    if re.search(SUB_CHECK, name):
        return None
    else:
        return "Sub assets may only use capital letters, numbers, '_', and '.'"


def is_unique_asset_name_good(name):
    if re.search(UNIQUE_CHECK, name):
        return None
    else:
        return "Invalid characters."
