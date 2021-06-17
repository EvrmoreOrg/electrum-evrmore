#!/usr/bin/env python
from electrum.ravencoin import opcodes, push_script


def create_transfer_asset_script(standard: bytes, asset: str, value: int):
    asset_header = b'rvnt'.hex()
    name = push_script(asset.encode('ascii').hex())
    amt = value.to_bytes(8, byteorder="little", signed=False).hex()
    asset_portion = asset_header+name+amt
    return standard + \
        bytes([opcodes.OP_RVN_ASSET]) + \
        bytes.fromhex(push_script(asset_portion)) + \
        bytes([opcodes.OP_DROP])
