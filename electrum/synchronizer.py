#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2014 Thomas Voegtlin
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
import hashlib
from typing import Dict, List, TYPE_CHECKING, Tuple, Union
from collections import defaultdict
import logging

from aiorpcx import run_in_thread, RPCError

from . import util
from .transaction import Transaction, PartialTransaction, AssetMeta, TxOutpoint
from .util import bh2u, make_aiohttp_session, NetworkJobOnDefaultServer, random_shuffled_copy, bfh, OldTaskGroup, EvrmoreValue
from .evrmore import address_to_scripthash, is_address
from .logging import Logger
from .interface import GracefulDisconnect, NetworkTimeout

if TYPE_CHECKING:
    from .network import Network
    from .address_synchronizer import AddressSynchronizer


class SynchronizerFailure(Exception): pass


def history_status(h):
    if not h:
        return None
    status = ''
    for tx_hash, height in h:
        status += tx_hash + ':%d:' % height
    return bh2u(hashlib.sha256(status.encode('ascii')).digest())


def asset_status(asset_data: Union[Dict, AssetMeta]):
    if asset_data:
        if isinstance(asset_data, Dict):
            sat_amount = asset_data['sats_in_circulation']
            div_amt = asset_data['divisions']
            reissuable = False if asset_data['reissuable'] == 0 else True
            has_ipfs = False if asset_data['has_ipfs'] == 0 else True

            h = ''.join([str(sat_amount), str(div_amt), str(reissuable), str(has_ipfs)])
            if has_ipfs:
                h += asset_data['ipfs']

            status = bh2u(hashlib.sha256(h.encode('ascii')).digest())
        else:
            sat_amount = asset_data.circulation
            div_amt = asset_data.divisions
            reissuable = asset_data.is_reissuable
            has_ipfs = asset_data.has_ipfs

            h = ''.join([str(sat_amount), str(div_amt), str(reissuable), str(has_ipfs)])
            if has_ipfs:
                h += asset_data.ipfs_str

            status = bh2u(hashlib.sha256(h.encode('ascii')).digest())
    else:
        status = None

    return status


class SynchronizerBase(NetworkJobOnDefaultServer):
    """Subscribe over the network to a set of addresses, and monitor their statuses.
    Every time a status changes, run a coroutine provided by the subclass.
    """

    def __init__(self, network: 'Network'):
        self.asyncio_loop = network.asyncio_loop

        NetworkJobOnDefaultServer.__init__(self, network)

    def _reset(self):
        super()._reset()
        self.requested_addrs = set()
        self.requested_assets = set()
        self.scripthash_to_address = {}
        self._processed_some_notifications = False  # so that we don't miss them
        # Queues
        self.add_queue = asyncio.Queue()
        self.asset_add_queue = asyncio.Queue()
        self.status_queue = asyncio.Queue()
        self.asset_status_queue = asyncio.Queue()

    async def _run_tasks(self, *, taskgroup):
        await super()._run_tasks(taskgroup=taskgroup)
        try:
            async with taskgroup as group:
                await group.spawn(self.send_subscriptions())
                await group.spawn(self.handle_status())

                await group.spawn(self.send_asset_subscriptions())
                await group.spawn(self.handle_asset_status())

                await group.spawn(self.main())
        finally:
            # we are being cancelled now
            self.session.unsubscribe(self.status_queue)
            self.session.unsubscribe(self.asset_status_queue)

    def add(self, addr):
        asyncio.run_coroutine_threadsafe(self._add_address(addr), self.asyncio_loop)

    def add_asset(self, asset):
        asyncio.run_coroutine_threadsafe(self._add_asset(asset), self.asyncio_loop)

    async def _add_asset(self, asset: str):
        if len(asset) > 32:
            raise ValueError(f"Assets may be at most 32 characters. {asset}")
        if asset in self.requested_assets:
            return
        self.requested_assets.add(asset)
        self.asset_add_queue.put_nowait(asset)

    async def _add_address(self, addr: str):
        # note: this method is async as add_queue.put_nowait is not thread-safe.
        if not is_address(addr): raise ValueError(f"invalid bitcoin address {addr}")
        if addr in self.requested_addrs: return
        self.requested_addrs.add(addr)
        self.add_queue.put_nowait(addr)

    async def _on_address_status(self, addr, status):
        """Handle the change of the status of an address."""
        raise NotImplementedError()  # implemented by subclasses

    async def _on_asset_status(self, asset, status):
        raise NotImplementedError()

    async def send_subscriptions(self):
        async def subscribe_to_address(addr):
            h = address_to_scripthash(addr)
            self.scripthash_to_address[h] = addr
            self._requests_sent += 1
            try:
                async with self._network_request_semaphore:
                    await self.session.subscribe('blockchain.scripthash.subscribe', [h], self.status_queue)
            except RPCError as e:
                if e.message == 'history too large':  # no unique error code
                    raise GracefulDisconnect(e, log_level=logging.ERROR) from e
                raise
            self._requests_answered += 1
            self.requested_addrs.remove(addr)

        while True:
            addr = await self.add_queue.get()
            await self.taskgroup.spawn(subscribe_to_address, addr)

    async def send_asset_subscriptions(self):
        async def subscribe_to_asset(asset):
            self._requests_sent += 1
            try:
                async with self._network_request_semaphore:
                    await self.session.subscribe('blockchain.asset.subscribe', [asset], self.asset_status_queue)
            except RPCError as e:
                raise
            self._requests_answered += 1
            self.requested_assets.remove(asset)

        while True:
            asset = await self.asset_add_queue.get()
            await self.taskgroup.spawn(subscribe_to_asset, asset)

    async def handle_status(self):
        while True:
            h, status = await self.status_queue.get()
            addr = self.scripthash_to_address[h]
            await self.taskgroup.spawn(self._on_address_status, addr, status)
            self._processed_some_notifications = True

    async def handle_asset_status(self):
        while True:
            asset, status = await self.asset_status_queue.get()
            await self.taskgroup.spawn(self._on_asset_status, asset, status)

    def num_requests_sent_and_answered(self) -> Tuple[int, int]:
        return self._requests_sent, self._requests_answered

    async def main(self):
        raise NotImplementedError()  # implemented by subclasses


class Synchronizer(SynchronizerBase):
    '''The synchronizer keeps the wallet up-to-date with its set of
    addresses and their transactions.  It subscribes over the network
    to wallet addresses, gets the wallet to generate new addresses
    when necessary, requests the transaction history of any addresses
    we don't have the full history of, and requests binary transaction
    data of any transactions the wallet doesn't have.
    '''
    def __init__(self, adb: 'AddressSynchronizer'):
        self.adb = adb
        SynchronizerBase.__init__(self, adb.network)

    def _reset(self):
        super()._reset()
        self.requested_tx = {}
        self.requested_histories = set()
        self.requested_asset_metas = set()
        self._stale_histories = dict()  # type: Dict[str, asyncio.Task]
        self._stale_metadata = dict()  # type: Dict[str, asyncio.Task]

    def diagnostic_name(self):
        return self.adb.diagnostic_name()

    def is_up_to_date(self):
        return (not self.requested_addrs
                and not self.requested_assets
                and not self.requested_asset_metas
                and not self.requested_histories
                and not self.requested_tx
                and not self._stale_histories)

    async def _on_address_status(self, addr, status):
        history = self.adb.db.get_addr_history(addr)
        if history_status(history) == status:
            return
        # No point in requesting history twice for the same announced status.
        # However if we got announced a new status, we should request history again:
        if (addr, status) in self.requested_histories:
            return
        # request address history
        self.requested_histories.add((addr, status))
        self._stale_histories.pop(addr, asyncio.Future()).cancel()
        h = address_to_scripthash(addr)
        self._requests_sent += 1
        async with self._network_request_semaphore:
            result = await self.interface.get_history_for_scripthash(h)
        self._requests_answered += 1
        self.logger.info(f"receiving history {addr} {len(result)}")
        hist = list(map(lambda item: (item['tx_hash'], item['height']), result))
        # tx_fees
        tx_fees = [(item['tx_hash'], item.get('fee')) for item in result]
        tx_fees = dict(filter(lambda x: x[1] is not None, tx_fees))
        # Check that the status corresponds to what was announced
        if history_status(hist) != status:
            # could happen naturally if history changed between getting status and history (race)
            self.logger.info(f"error: status mismatch: {addr}. we'll wait a bit for status update.")

            # The server is supposed to send a new status notification, which will trigger a new
            # get_history. We shall wait a bit for this to happen, otherwise we disconnect.
            async def disconnect_if_still_stale():
                timeout = self.network.get_network_timeout_seconds(NetworkTimeout.Generic)
                await asyncio.sleep(timeout)
                raise SynchronizerFailure(f"timeout reached waiting for addr {addr}: history still stale")

            self._stale_histories[addr] = await self.taskgroup.spawn(disconnect_if_still_stale)
        else:
            self._stale_histories.pop(addr, asyncio.Future()).cancel()
            # Store received history
            self.adb.receive_history_callback(addr, hist, tx_fees)
            # Request transactions we don't have
            await self._request_missing_txs(hist)

        # Remove request; this allows up_to_date to be True
        self.requested_histories.discard((addr, status))

    async def _on_asset_status(self, asset, status):
        self.logger.info(f'notified of asset {asset}')
        data = self.adb.db.get_asset_meta(asset)
        if asset_status(data) == status:
            return
        if (asset, status) in self.requested_asset_metas:
            return
        # Request asset meta
        self.requested_asset_metas.add((asset, status))
        self._requests_sent += 1
        async with self._network_request_semaphore:
            result = await self.network.get_meta_for_asset(asset)
        self._requests_answered += 1
        self.logger.info(f"receiving asset meta {asset} {repr(result)}")
        if not result or asset_status(result) != status:
            self.logger.info(f"error: status mismatch: {asset}. we'll wait a bit for status update.")
            async def disconnect_if_still_stale():
                timeout = self.network.get_network_timeout_seconds(NetworkTimeout.Generic)
                await asyncio.sleep(timeout)
                raise SynchronizerFailure(f"timeout reached waiting for asset {asset}: asset still stale")

            self._stale_metadata[asset] = await self.taskgroup.spawn(disconnect_if_still_stale)

        else:
            self._stale_metadata.pop(asset, asyncio.Future()).cancel()
            self.adb.recieve_asset_callback(asset, result)

        self.requested_asset_metas.discard((asset, status))

    async def _request_missing_txs(self, hist, *, allow_server_not_finding_tx=False):
        # "hist" is a list of [tx_hash, tx_height] lists
        transaction_hashes = []
        for tx_hash, tx_height in hist:
            if tx_hash in self.requested_tx:
                continue
            tx = self.adb.db.get_transaction(tx_hash)
            if tx and not isinstance(tx, PartialTransaction):
                continue  # already have complete tx
            transaction_hashes.append(tx_hash)
            self.requested_tx[tx_hash] = tx_height

        if not transaction_hashes: return
        async with OldTaskGroup() as group:
            for tx_hash in transaction_hashes:
                await group.spawn(
                    self._get_transaction(tx_hash, allow_server_not_finding_tx=allow_server_not_finding_tx))

    async def _get_transaction(self, tx_hash, *, allow_server_not_finding_tx=False):
        self._requests_sent += 1
        try:
            async with self._network_request_semaphore:
                raw_tx = await self.interface.get_transaction(tx_hash)
        except RPCError as e:
            # most likely, "No such mempool or blockchain transaction"
            if allow_server_not_finding_tx:
                self.requested_tx.pop(tx_hash)
                return
            else:
                raise
        finally:
            self._requests_answered += 1
        tx = Transaction(raw_tx)
        if tx_hash != tx.txid():
            raise SynchronizerFailure(f"received tx does not match expected txid ({tx_hash} != {tx.txid()})")
        tx_height = self.requested_tx.pop(tx_hash)
        self.adb.receive_tx_callback(tx_hash, tx, tx_height)
        self.logger.info(f"received tx {tx_hash} height: {tx_height} bytes: {len(raw_tx)}")

    async def main(self):
        self.adb.set_up_to_date(False)
        # request missing txns, if any
        for addr in random_shuffled_copy(self.adb.db.get_history()):
            history = self.adb.db.get_addr_history(addr)
            # Old electrum servers returned ['*'] when all history for the address
            # was pruned. This no longer happens but may remain in old wallets.
            if history == ['*']: continue
            await self._request_missing_txs(history, allow_server_not_finding_tx=True)
        # add addresses to bootstrap
        for addr in random_shuffled_copy(self.adb.get_addresses()):
            await self._add_address(addr)
        # Ensure we have asset meta
        assets = set(self.adb.get_assets()).union(set(sum(self.adb.get_balance(self.adb.get_addresses()), EvrmoreValue()).assets.keys()))
        for asset in assets:
            await self._add_asset(asset)
        # main loop
        while True:
            await asyncio.sleep(0.1)
            hist_done = self.is_up_to_date()
            spv_done = self.adb.verifier.is_up_to_date() if self.adb.verifier else True
            up_to_date = hist_done and spv_done
            # see if status changed
            if (up_to_date != self.adb.is_up_to_date()
                    or up_to_date and self._processed_some_notifications):
                self._processed_some_notifications = False
                self.adb.set_up_to_date(up_to_date)


class Notifier(SynchronizerBase):
    """Watch addresses. Every time the status of an address changes,
    an HTTP POST is sent to the corresponding URL.
    """

    def __init__(self, network):
        SynchronizerBase.__init__(self, network)
        self.watched_addresses = defaultdict(list)  # type: Dict[str, List[str]]
        self._start_watching_queue = asyncio.Queue()  # type: asyncio.Queue[Tuple[str, str]]

    async def main(self):
        # resend existing subscriptions if we were restarted
        for addr in self.watched_addresses:
            await self._add_address(addr)
        # main loop
        while True:
            addr, url = await self._start_watching_queue.get()
            self.watched_addresses[addr].append(url)
            await self._add_address(addr)

    async def start_watching_addr(self, addr: str, url: str):
        await self._start_watching_queue.put((addr, url))

    async def stop_watching_addr(self, addr: str):
        self.watched_addresses.pop(addr, None)
        # TODO blockchain.scripthash.unsubscribe

    async def _on_address_status(self, addr, status):
        if addr not in self.watched_addresses:
            return
        self.logger.info(f'new status for addr {addr}')
        headers = {'content-type': 'application/json'}
        data = {'address': addr, 'status': status}
        for url in self.watched_addresses[addr]:
            try:
                async with make_aiohttp_session(proxy=self.network.proxy, headers=headers) as session:
                    async with session.post(url, json=data, headers=headers) as resp:
                        await resp.text()
            except Exception as e:
                self.logger.info(repr(e))
            else:
                self.logger.info(f'Got Response for {addr}')
