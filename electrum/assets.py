#!/usr/bin/env python
import re
from electrum.ravencoin import opcodes, push_script

DOUBLE_PUNCTUATION = "^.*[._]{2,}.*$"
LEADING_PUNCTUATION = "^[._].*$"
TRAILING_PUNCTUATION = "^.*[._]$"
RAVEN_NAMES = "^RVN$|^RAVEN$|^RAVENCOIN$|^#RVN$|^#RAVEN$|^#RAVENCOIN$"

MAIN_CHECK = "^[A-Z0-9._]{3,}$"
SUB_CHECK ="^[A-Z0-9._]+$"
UNIQUE_CHECK = "^[-A-Za-z0-9@$%&*()[\\]{}_.?:]+$"

def create_transfer_asset_script(standard: bytes, asset: str, value: int):
    asset_header = b'rvnt'.hex()
    name = push_script(asset.encode('ascii').hex())
    amt = value.to_bytes(8, byteorder="little", signed=False).hex()
    asset_portion = asset_header+name+amt
    return standard + \
        bytes([opcodes.OP_RVN_ASSET]) + \
        bytes.fromhex(push_script(asset_portion)) + \
        bytes([opcodes.OP_DROP])


def is_main_asset_name_good(name):
    """
    Returns the error as a string or None if good
    """
    if re.search(DOUBLE_PUNCTUATION, name):
        return "There is double punctuation in this name."
    if re.search(LEADING_PUNCTUATION, name):
        return "You cannot begin an asset with punctuation."
    if re.search(TRAILING_PUNCTUATION, name):
        return "You cannot end an asset with punctuation."
    if re.search(RAVEN_NAMES, name):
        return "You cannot have Ravencoin-like names."
    if re.search(MAIN_CHECK, name):
        return None
    else:
        return "SIZE"


def is_sub_asset_name_good(name):
    if re.search(DOUBLE_PUNCTUATION, name):
        return "There is double punctuation in this name."
    if re.search(LEADING_PUNCTUATION, name):
        return "You cannot begin an asset with punctuation."
    if re.search(TRAILING_PUNCTUATION, name):
        return "You cannot end an asset with punctuation."
    if re.search(SUB_CHECK, name):
        return None
    else:
        return "You may only use capital letters, numbers, '_', and '.'"


def is_unique_asset_name_good(name):
    if re.search(UNIQUE_CHECK, name):
        return None
    else:
        return "Invalid characters."
