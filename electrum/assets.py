#!/usr/bin/env python
import re
from typing import Dict

from .ravencoin import opcodes, push_script, base_encode, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC, COIN
from . import transaction

DOUBLE_PUNCTUATION = "^.*[._]{2,}.*$"
LEADING_PUNCTUATION = "^[._].*$"
TRAILING_PUNCTUATION = "^.*[._]$"
RAVEN_NAMES = "^RVN$|^RAVEN$|^RAVENCOIN$|^#RVN$|^#RAVEN$|^#RAVENCOIN$"

MAIN_CHECK = "^[A-Z0-9._]{3,}$"
SUB_CHECK = "^[A-Z0-9._]+$"
UNIQUE_CHECK = "^[-A-Za-z0-9@$%&*()[\\]{}_.?:]+$"


class BadAssetScript(Exception): pass


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
            'reissuable': reis,
            'has_ipfs': has_i,
            'ipfs': base_encode(ifps, base=58) if ifps else None,
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
            'reissuable': reis,
            'has_ipfs': 0 if not ifps else 1,
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
            'reissuable': 0,
            'has_ipfs': 0,
            'ipfs': None,
            'type': 'o'
        }


def create_transfer_asset_script(standard: bytes, asset: str, value: int):
    asset_header = b'rvnt'.hex()
    name = push_script(asset.encode('ascii').hex())
    amt = value.to_bytes(8, byteorder="little", signed=False).hex()
    asset_portion = asset_header+name+amt
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
