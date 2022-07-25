# Electrum - Lightweight Bitcoin Client
# Copyright (c) 2012 Thomas Voegtlin
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
from typing import Sequence, Optional, TYPE_CHECKING

import aiorpcx

from .assets import pull_meta_from_create_or_reissue_script, BadAssetScript
from .util import bh2u, TxMinedInfo, NetworkJobOnDefaultServer, bfh
from .crypto import sha256d
from .ravencoin import hash_decode, hash_encode
from .transaction import Transaction, TxOutpoint, AssetMeta
from .blockchain import hash_header
from .interface import GracefulDisconnect
from .network import UntrustedServerReturnedError
from . import constants

if TYPE_CHECKING:
    from .network import Network
    from .address_synchronizer import AddressSynchronizer


class MerkleVerificationFailure(Exception): pass
class MissingBlockHeader(MerkleVerificationFailure): pass
class MerkleRootMismatch(MerkleVerificationFailure): pass
class InnerNodeOfSpvProofIsValidTx(MerkleVerificationFailure): pass
class AssetVerification(MerkleVerificationFailure): pass


class SPV(NetworkJobOnDefaultServer):
    """ Simple Payment Verification """

    def __init__(self, network: 'Network', wallet: 'AddressSynchronizer'):
        self.wallet = wallet
        NetworkJobOnDefaultServer.__init__(self, network)

    def _reset(self):
        super()._reset()
        self.merkle_roots = {}  # txid -> merkle root (once it has been verified)
        self.requested_merkle = set()  # txid set of pending requests
        self.requested_assets = set()  # asset string set of pending requests

    async def _run_tasks(self, *, taskgroup):
        await super()._run_tasks(taskgroup=taskgroup)
        async with taskgroup as group:
            await group.spawn(self.main)

    def diagnostic_name(self):
        return self.wallet.diagnostic_name()

    async def main(self):
        self.blockchain = self.network.blockchain()
        while True:
            await self._maybe_undo_verifications()
            await self._request_proofs()
            await self._request_asset_proofs()
            await asyncio.sleep(0.1)

    async def _request_proofs(self):
        local_height = self.blockchain.height()
        unverified = self.wallet.get_unverified_txs()

        for tx_hash, tx_height in unverified.items():
            # do not request merkle branch if we already requested it
            if tx_hash in self.requested_merkle or tx_hash in self.merkle_roots:
                continue
            # or before headers are available
            if not (0 < tx_height <= local_height):
                continue
            # if it's in the checkpoint region, we still might not have the header
            header = self.blockchain.read_header(tx_height)
            if header is None:
                if tx_height < constants.net.max_dgw_checkpoint():
                    # FIXME these requests are not counted (self._requests_sent += 1)
                    await self.taskgroup.spawn(self.interface.request_chunk(tx_height, None, can_return_early=True))
                    # await self.interface.request_chunk(tx_height, None, can_return_early=True)
                continue
            # request now
            self.logger.info(f'requested merkle {tx_hash}')
            self.requested_merkle.add(tx_hash)
            await self.taskgroup.spawn(self._request_and_verify_single_proof, tx_hash, tx_height)

    async def _request_asset_proofs(self):
        local_height = self.blockchain.height()
        unverified = self.wallet.get_unverified_asset_metas()

        async def request_and_verify_metadata_against(asset, height: int, tx_hash: str, idx: int, meta: dict):
            self._requests_sent += 1
            try:
                async with self._network_request_semaphore:
                    raw_tx = await self.interface.get_transaction(tx_hash)
            except aiorpcx.jsonrpc.RPCError as e:
                # most likely, "No such mempool or blockchain transaction"
                raise
            finally:
                self._requests_answered += 1
            tx = Transaction(raw_tx)
            if tx_hash != tx.txid():
                raise MerkleVerificationFailure(f"received tx does not match expected txid for ({tx_hash} != {tx.txid()})")
            
            no_verify = self.interface.blockchain.config.get('noverify')
            if no_verify:
                self.logger.error(f'Skipping verification for asset {asset}')
            if not no_verify:
                try:
                    await self.wallet.verifier.request_and_verfiy_proof(tx_hash, height)
                except UntrustedServerReturnedError as e:
                    self.logger.info(f'tx {tx_hash} not at height {height}')
                    raise
            try:
                vout = tx.outputs()[idx]
            except IndexError:
                raise AssetVerification(f"Non-existant vout {idx}")
            script = vout.scriptpubkey
            try:
                data = pull_meta_from_create_or_reissue_script(script)
            except (BadAssetScript, IndexError) as e:
                print(e)
                raise AssetVerification(f"Bad asset script {script})")
            if data['name'] != asset:
                raise AssetVerification(f"Not our asset! {asset} vs {data['name']}")
            for key, value in meta.items():
                if data['type'] == 'r' and key == 'sats_in_circulation':
                    if data[key] > value:
                        raise AssetVerification(f"Reissued amount is greater than the total amount: {value}, {data['name']}")
                elif data[key] != value:
                    raise AssetVerification(f"Metadata mismatch: {value} vs {data[key]}")
            return data['type']

        async def parse_and_verify(asset, result: AssetMeta):
            og = self.wallet.get_asset_meta(asset)
            if og and (og.height - constants.net.MATURE > result.height):
                raise AssetVerification(f"Server is trying to send old asset data for source (height {og.height} vs {result.height})")

            if result.source_divisions and result.div_height:
                div_height = result.div_height
                if og and og.div_height is not None and (og.div_height - constants.net.MATURE > div_height):
                    raise AssetVerification(f"Server is trying to send old asset data for division source (height {og.div_height} vs {div_height})")
                prev_txid = result.source_divisions.txid.hex()
                prev_idx = result.source_divisions.out_idx
                try:
                    await request_and_verify_metadata_against(asset, div_height, prev_txid, prev_idx,
                                                          {'divisions': result.divisions})
                except Exception as e:
                    self.logger.info(f'Failed to verify metadata for {asset}')
                    self.requested_assets.discard(asset)
                    raise GracefulDisconnect(e) from e
            
            if result.source_ipfs and result.ipfs_height:
                ipfs_height = result.ipfs_height
                if og and og.ipfs_height is not None and (og.ipfs_height - constants.net.MATURE> ipfs_height):
                    raise AssetVerification(f"Server is trying to send old asset data for ipfs source (height {og.ipfs_height} vs {ipfs_height})")
                prev_txid = result.source_ipfs.txid.hex()
                prev_idx = result.source_ipfs.out_idx
                try:
                    await request_and_verify_metadata_against(asset, ipfs_height, prev_txid, prev_idx,
                                                          {'ipfs': result.ipfs_str})
                except Exception as e:
                    self.logger.info(f'Failed to verify metadata for {asset}')
                    self.requested_assets.discard(asset)
                    raise GracefulDisconnect(e) from e
            
            d = dict()
            d['reissuable'] = result.is_reissuable
            d['has_ipfs'] = result.has_ipfs and not (result.source_ipfs or result.ipfs_height)
            d['sats_in_circulation'] = result.circulation
            if d['has_ipfs']:
                d['ipfs'] = result.ipfs_str
            d['divisions'] = result.divisions if not (result.source_divisions or result.div_height) else 0xff
            height = result.height
            txid = result.source_outpoint.txid.hex()
            idx = result.source_outpoint.out_idx
            try:
                s_type = await request_and_verify_metadata_against(asset, height, txid, idx, d)
            except Exception as e:
                self.logger.info(f'Failed to verify metadata for {asset}')
                self.requested_assets.discard(asset)
                raise GracefulDisconnect(e) from e
            
            assert s_type

            meta = AssetMeta(
                asset, 
                result.circulation,
                result.is_owner, 
                result.is_reissuable,
                result.divisions,
                result.has_ipfs,
                result.ipfs_str,
                result.height,
                result.div_height,
                result.ipfs_height,
                s_type,
                result.source_outpoint,
                result.source_divisions,
                result.source_ipfs
                )
            
            self.requested_assets.discard(asset)
            self.wallet.add_verified_asset_meta(asset, meta)

        for asset, asset_meta in unverified.items():
            largest_height = asset_meta.height
            
            # do not request merkle branch if we already requested it
            if asset in self.requested_assets:
                continue
            # or before headers are available
            if not (0 < largest_height <= local_height):
                continue
            # if it's in the checkpoint region, we still might not have the header
            header = self.blockchain.read_header(largest_height)
            if header is None:
                if largest_height < constants.net.max_dgw_checkpoint():
                    # FIXME these requests are not counted (self._requests_sent += 1)
                    await self.taskgroup.spawn(self.interface.request_chunk(largest_height, None, can_return_early=True))
                    # await self.interface.request_chunk(tx_height, None, can_return_early=True)
                continue

            if asset_meta.div_height:
                prev_header = self.blockchain.read_header(asset_meta.div_height)
                if prev_header is None:
                    if asset_meta.div_height < constants.net.max_dgw_checkpoint():
                        # FIXME these requests are not counted (self._requests_sent += 1)
                        await self.taskgroup.spawn(self.interface.request_chunk(asset_meta.div_height, None, can_return_early=True))
                        # await self.interface.request_chunk(tx_height, None, can_return_early=True)
                    continue

            if asset_meta.ipfs_height:
                prev_header = self.blockchain.read_header(asset_meta.ipfs_height)
                if prev_header is None:
                    if asset_meta.ipfs_height < constants.net.max_dgw_checkpoint():
                        # FIXME these requests are not counted (self._requests_sent += 1)
                        await self.taskgroup.spawn(self.interface.request_chunk(asset_meta.ipfs_height, None, can_return_early=True))
                        # await self.interface.request_chunk(tx_height, None, can_return_early=True)
                    continue

            # request now
            self.logger.info(f'requested asset {asset}')
            self.requested_assets.add(asset)
            await self.taskgroup.spawn(parse_and_verify(asset, asset_meta))


    async def request_and_verfiy_proof(self, tx_hash, tx_height):
        try:
            self._requests_sent += 1
            async with self._network_request_semaphore:
                merkle = await self.interface.get_merkle_for_transaction(tx_hash, tx_height)
        except aiorpcx.jsonrpc.RPCError:
            self.logger.info(f'tx {tx_hash} not at height {tx_height}')
            self.wallet.remove_unverified_tx(tx_hash, tx_height)
            self.requested_merkle.discard(tx_hash)
            return
        finally:
            self._requests_answered += 1
        # Verify the hash of the server-provided merkle branch to a
        # transaction matches the merkle root of its block
        if tx_height != merkle.get('block_height'):
            self.logger.info('requested tx_height {} differs from received tx_height {} for txid {}'
                             .format(tx_height, merkle.get('block_height'), tx_hash))
        tx_height = merkle.get('block_height')
        pos = merkle.get('pos')
        merkle_branch = merkle.get('merkle')
        # we need to wait if header sync/reorg is still ongoing, hence lock:
        async with self.network.bhi_lock:
            header = self.network.blockchain().read_header(tx_height)
        try:
            verify_tx_is_in_block(tx_hash, merkle_branch, pos, header, tx_height)
        except MerkleVerificationFailure as e:
            if self.network.config.get("skipmerklecheck"):
                self.logger.info(f"skipping merkle proof check {tx_hash}")
            else:
                self.logger.info(repr(e))
                raise GracefulDisconnect(e) from e
        return header, pos

    async def _request_and_verify_single_proof(self, tx_hash, tx_height):
        no_verify = self.blockchain.config.get('noverify')
        if no_verify:
            self.logger.error(f'Skipping merkle verification for {tx_hash}')
            timestamp = 0
            header_hash = bytes(32).hex()
            pos = 0
        else:
            try:
                header, pos = await self.request_and_verfiy_proof(tx_hash, tx_height)
            except UntrustedServerReturnedError as e:
                if not isinstance(e.original_exception, aiorpcx.jsonrpc.RPCError):
                    raise
                self.logger.info(f'tx {tx_hash} not at height {tx_height}')
                self.wallet.remove_unverified_tx(tx_hash, tx_height)
                self.requested_merkle.discard(tx_hash)
                return

            # we passed all the tests
            self.merkle_roots[tx_hash] = header.get('merkle_root')
            self.logger.info(f"verified {tx_hash}")

            header_hash = hash_header(header)
            timestamp = header.get('timestamp')
            
        self.requested_merkle.discard(tx_hash)
        tx_info = TxMinedInfo(height=tx_height,
                              timestamp=timestamp,
                              txpos=pos,
                              header_hash=header_hash)
        self.wallet.add_verified_tx(tx_hash, tx_info)

    @classmethod
    def hash_merkle_root(cls, merkle_branch: Sequence[str], tx_hash: str, leaf_pos_in_tree: int):
        """Return calculated merkle root."""
        try:
            h = hash_decode(tx_hash)
            merkle_branch_bytes = [hash_decode(item) for item in merkle_branch]
            leaf_pos_in_tree = int(leaf_pos_in_tree)  # raise if invalid
        except Exception as e:
            raise MerkleVerificationFailure(e)
        if leaf_pos_in_tree < 0:
            raise MerkleVerificationFailure('leaf_pos_in_tree must be non-negative')
        index = leaf_pos_in_tree
        for item in merkle_branch_bytes:
            if len(item) != 32:
                raise MerkleVerificationFailure('all merkle branch items have to 32 bytes long')
            inner_node = (item + h) if (index & 1) else (h + item)
            cls._raise_if_valid_tx(bh2u(inner_node))
            h = sha256d(inner_node)
            index >>= 1
        if index != 0:
            raise MerkleVerificationFailure(f'leaf_pos_in_tree too large for branch')
        return hash_encode(h)

    @classmethod
    def _raise_if_valid_tx(cls, raw_tx: str):
        # If an inner node of the merkle proof is also a valid tx, chances are, this is an attack.
        # https://lists.linuxfoundation.org/pipermail/bitcoin-dev/2018-June/016105.html
        # https://lists.linuxfoundation.org/pipermail/bitcoin-dev/attachments/20180609/9f4f5b1f/attachment-0001.pdf
        # https://bitcoin.stackexchange.com/questions/76121/how-is-the-leaf-node-weakness-in-merkle-trees-exploitable/76122#76122
        tx = Transaction(raw_tx)
        try:
            tx.deserialize()
        except:
            pass
        else:
            raise InnerNodeOfSpvProofIsValidTx()

    async def _maybe_undo_verifications(self):
        old_chain = self.blockchain
        cur_chain = self.network.blockchain()
        if cur_chain != old_chain:
            self.blockchain = cur_chain
            above_height = cur_chain.get_height_of_last_common_block_with_chain(old_chain)
            self.logger.info(f"undoing verifications above height {above_height}")
            tx_hashes = self.wallet.undo_verifications(self.blockchain, above_height)
            for tx_hash in tx_hashes:
                self.logger.info(f"redoing {tx_hash}")
                self.remove_spv_proof_for_tx(tx_hash)

    def remove_spv_proof_for_tx(self, tx_hash):
        self.merkle_roots.pop(tx_hash, None)
        self.requested_merkle.discard(tx_hash)

    def is_up_to_date(self):
        return (not self.requested_merkle
                and not self.wallet.unverified_tx)


def verify_tx_is_in_block(tx_hash: str, merkle_branch: Sequence[str],
                          leaf_pos_in_tree: int, block_header: Optional[dict],
                          block_height: int) -> None:
    """Raise MerkleVerificationFailure if verification fails."""
    if not block_header:
        raise MissingBlockHeader("merkle verification failed for {} (missing header {})"
                                 .format(tx_hash, block_height))
    if len(merkle_branch) > 30:
        raise MerkleVerificationFailure(f"merkle branch too long: {len(merkle_branch)}")
    calc_merkle_root = SPV.hash_merkle_root(merkle_branch, tx_hash, leaf_pos_in_tree)
    if block_header.get('merkle_root') != calc_merkle_root:
        raise MerkleRootMismatch("merkle verification failed for {} ({} != {})".format(
            tx_hash, block_header.get('merkle_root'), calc_merkle_root))
