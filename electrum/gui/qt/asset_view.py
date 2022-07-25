#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
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
from enum import IntEnum
import json
from typing import List, Dict, Optional
import re
import os

from PyQt5.QtCore import Qt, QModelIndex, pyqtSignal
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QMouseEvent, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QMenu, QCheckBox, QSplitter, QFrame, QVBoxLayout, 
                                QLabel, QTextEdit, QWidget, QTreeWidget, QTreeWidgetItem, QScrollArea,
                                QHeaderView)

from electrum.i18n import _
from electrum.network import Network
from electrum.util import IPFSData, get_ipfs_path, ipfs_explorer_URL, profiler, RavenValue, Satoshis, get_asyncio_loop, make_aiohttp_session
from electrum.ravencoin import COIN, is_address, base_decode
from electrum.wallet import InternalAddressCorruption
from electrum.transaction import AssetMeta
import electrum.transaction as transaction

from .util import EnterButton, MyTreeView, MONOSPACE_FONT, webopen, QHSeperationLine


VIEWABLE_MIMES = ('image/jpeg', 'image/png', 'image/gif', 'image/tiff', 'image/webp', 'image/avif',
                    'text/plain', 'application/json')


def min_num_str(num_str: str) -> str:
    while (num_str[-1] == '0' or num_str[-1] == '.') and len(num_str) > 1:
        should_exit = num_str[-1] == '.'
        num_str = num_str[:-1]
        if should_exit:
            break
    return num_str

async def try_download_ipfs(parent, ipfs, downloading, url, only_headers=True, pre_callback=None):
    network = Network.get_instance()
    proxy = network.proxy if network else None

    if ipfs in downloading:
        return

    downloading.add(ipfs)

    if pre_callback:
        pre_callback()

    ipfs_path = get_ipfs_path(parent.config, ipfs)

    total_downloaded = 0
    is_rip14 = False
    try:
        async with make_aiohttp_session(proxy, None, 30) as session:
            async with session.get(url) as resp:
                if only_headers or resp.content_type not in VIEWABLE_MIMES or \
                    (resp.content_length and resp.content_length > parent.config.get('max_ipfs_size', 1024 * 1024 * 10)):
                    info = IPFSData(ipfs, resp.content_type, resp.content_length, False)
                    parent.wallet.adb.add_ipfs_information(info)
                    downloading.discard(ipfs)
                    return
                while True:
                    chunk = await resp.content.read(1024)
                    if not chunk:
                        break
                    #if not is_rip14:
                    #    # rip0014 should be right up front
                    #    is_rip14 = b'rip0014' in chunk
                    total_downloaded += len(chunk)
                    if resp.content_length and total_downloaded > resp.content_length:
                        break
                    if total_downloaded > parent.config.get('max_ipfs_size', 1024 * 1024 * 10):
                        break
                    
                    with open(ipfs_path, 'ab') as f:
                        f.write(chunk)
    except asyncio.exceptions.TimeoutError:
        parent.wallet.adb.add_ipfs_information(IPFSData(
                    ipfs,
                    None,
                    None,
                    False
        ))
        downloading.discard(ipfs)
        return
    except Exception as e:
        downloading.discard(ipfs)
        raise e

    if (resp.content_length and total_downloaded > resp.content_length) or \
        total_downloaded > parent.config.get('max_ipfs_size', 1024 * 1024 * 10):
        os.unlink(ipfs_path)
        parent.show_error_signal.emit(_('The IPFS Gateway gave us bad data!'))
        downloading.discard(ipfs)
        return

    info = IPFSData(ipfs, resp.content_type, total_downloaded, True) #, is_rip14)
    parent.wallet.adb.add_ipfs_information(info)
    downloading.discard(ipfs)


def try_ask_to_save_all(parent):
    if not parent.config.get('ask_download_all_ipfs', True):
        return

    cb = QCheckBox(_('Do not ask again.'))
    cb_checked = False

    def on_cb(x):
        nonlocal cb_checked
        cb_checked = x == Qt.Checked

    cb.stateChanged.connect(on_cb)
    goto = parent.question(_('Would you like to automatically try to download\n'+
                            'and display IFPS data in-wallet for all assets?\n'+
                            '(This can be changed later in settings.)'),
                            title=_('Automatically Store IPFS data'), checkbox=cb)

    if cb_checked:
        parent.config.set_key('ask_download_all_ipfs', False)
    if goto:
        parent.config.set_key('download_all_ipfs', True, True)
    

def try_ask_to_save(parent, ipfs, url, viewer):
    if parent.config.get('download_all_ipfs', False) or not parent.config.get('ask_download_ipfs', True):
        return

    ipfs_information: IPFSData = parent.wallet.adb.get_ipfs_information(ipfs)
    if ipfs_information and ipfs_information.byte_length and (ipfs_information.byte_length > parent.config.get('max_ipfs_size', 1024 * 1024 * 10) or \
                                ipfs_information.mime_type not in VIEWABLE_MIMES):
        return

    if ipfs in viewer.requested_ipfses:
        return

    ipfs_path = get_ipfs_path(parent.config, ipfs)
    if os.path.exists(ipfs_path):
        return

    cb = QCheckBox(_('Do not ask again.'))
    cb_checked = False

    def on_cb(x):
        nonlocal cb_checked
        cb_checked = x == Qt.Checked

    cb.stateChanged.connect(on_cb)
    goto = parent.question(_('Would you like to try to download the data associated with\n{}\n' +
                            'and allow it to be viewable in-wallet?\n' + 
                            'Note: this trusts the IPFS gateway to return the\n' + 
                            'correct file').format(ipfs),
                            title=_('Store IPFS data'), checkbox=cb)

    if cb_checked:
        parent.config.set_key('ask_download_ipfs', False)
    if goto:
        try_ask_to_save_all(parent)
        loop = get_asyncio_loop()
        task = loop.create_task(try_download_ipfs(parent, ipfs, viewer.requested_ipfses, url, False, viewer.update_trigger.emit))
        task.add_done_callback(lambda _: viewer.update_trigger.emit())


def webopen_safe_non_ipfs(parent, url):
    show_warn = parent.config.get('show_ipfs_warning', True)
    if show_warn:
        cb = QCheckBox(_("Don't show this message again."))
        cb_checked = False

        def on_cb(x):
            nonlocal cb_checked
            cb_checked = x == Qt.Checked

        cb.stateChanged.connect(on_cb)
        goto = parent.question(_('You are about to visit:\n\n'
                                        '{}\n\n'
                                        'Please follow safe practices and common sense. If you are unsure '
                                        'about what\'s on the other end of a url, don\'t '
                                        'visit it!\n\n'
                                        'Are you sure you want to continue?').format(url),
                                    title=_('Warning: External Data'), checkbox=cb)

        if cb_checked:
            parent.config.set_key('show_ipfs_warning', False)
        if goto:
            webopen(url)
    else:
        webopen(url)


def webopen_safe(parent, ipfs, url, viewer):
    show_warn = parent.config.get('show_ipfs_warning', True)
    if show_warn:
        cb = QCheckBox(_("Don't show this message again."))
        cb_checked = False

        def on_cb(x):
            nonlocal cb_checked
            cb_checked = x == Qt.Checked

        cb.stateChanged.connect(on_cb)
        goto = parent.question(_('You are about to visit:\n\n'
                                        '{}\n\n'
                                        'IPFS hashes can link to anything. Please follow '
                                        'safe practices and common sense. If you are unsure '
                                        'about what\'s on the other end of an IPFS, don\'t '
                                        'visit it!\n\n'
                                        'Are you sure you want to continue?').format(url),
                                    title=_('Warning: External Data'), checkbox=cb)

        if cb_checked:
            parent.config.set_key('show_ipfs_warning', False)
        if goto:
            webopen(url)
            try_ask_to_save(parent, ipfs, url, viewer)
    else:
        webopen(url)
        try_ask_to_save(parent, ipfs, url, viewer)


def human_readable_size(size, decimal_places=3):
    if not isinstance(size, int):
        return _('Unknown')
    for unit in ['B','KiB','MiB','GiB','TiB']:
        if size < 1024.0:
            break
        size /= 1024.0
    return f"{size:.{decimal_places}f}{unit}"


IPFS_ROLE = Qt.UserRole + 100
TXID_ROLE = Qt.UserRole + 101
COPYABLE = Qt.UserRole + 102
KEY_DATA_ROLE = Qt.UserRole + 103
VALUE_DATA_ROLE = Qt.UserRole + 104

def associateDataOfType(element: QTreeWidgetItem, data: str):
    try:
        b58_bytes = base_decode(data, base=58)
        if b58_bytes[:2] == b'\x12\x20':
            element.setData(0, IPFS_ROLE, data)
        elif b58_bytes[:2] == b'\x54\x20':
            element.setData(0, TXID_ROLE, b58_bytes[2:].hex())
    except Exception:
        pass


class JsonViewWidget(QTreeWidget):
    MAX_DEPTH = 10

    class CopyType(IntEnum):
        NONE = 0x0
        KEY = 0x2
        VALUE = 0x1
        BOTH = int(KEY) + int(VALUE)

    def __init__(self, parent_widget: 'MetadataViewer'):
        QTreeWidget.__init__(self)
        self.parent_widget = parent_widget
        self.setHeaderLabels([_('JSON Key'), _('JSON Value')])
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.create_menu)


    def create_menu(self, position):
        item = self.currentItem()
        if not item:
            return
        menu = QMenu()

        has_any = False

        try:
            ipfs_str = item.data(0, IPFS_ROLE)
            if ipfs_str:
                has_any = True
                url = ipfs_explorer_URL(self.parent_widget.main_window.config, 'ipfs', ipfs_str)
                menu.addAction(_('View IPFS'), lambda: webopen_safe_non_ipfs(self.parent_widget.main_window, url))
        except AttributeError:
            pass

        try:
            txid_str = item.data(0, TXID_ROLE)
            if txid_str:
                has_any = True
                def open_transaction():
                    raw_tx = self.parent_widget.main_window._fetch_tx_from_network(txid_str, False)
                    if not raw_tx:
                        self.parent_widget.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                        return
                    tx = transaction.Transaction(raw_tx)
                    self.parent_widget.main_window.show_transaction(tx)
                menu.addAction(_('View Transaction'), open_transaction)
        except AttributeError:
            pass

        copy_type = int(item.data(0, COPYABLE))
        if not has_any and copy_type != 0:
            has_any = True

        if copy_type & int(self.CopyType.KEY):
            key_str = item.data(0, KEY_DATA_ROLE)
            menu.addAction(_('Copy Key'), lambda: self.parent_widget.main_window.do_copy(key_str, title=_('JSON Key')))

        if copy_type & int(self.CopyType.VALUE):
            value_str = item.data(0, VALUE_DATA_ROLE)
            menu.addAction(_('Copy Value'), lambda: self.parent_widget.main_window.do_copy(value_str, title=_('JSON Value')))

        if not has_any:
            return
        
        menu.exec_(self.viewport().mapToGlobal(position))

    def recursivelyAddJson(self, obj, parent: QTreeWidgetItem, counter: int):
        if counter >= self.MAX_DEPTH:
            item = QTreeWidgetItem(['...', ''])
            item.setData(0, COPYABLE, self.CopyType.NONE)
            parent.addChild(item)
            return
        
        if isinstance(obj, (list, tuple, set)):
            for i, element in enumerate(obj):
                if isinstance(element, (dict, list, tuple, set)):
                    item = QTreeWidgetItem([str(i), ''])
                    item.setData(0, COPYABLE, self.CopyType.NONE)
                    self.recursivelyAddJson(element, item, counter+1)
                else:
                    element_str = str(element)
                    item = QTreeWidgetItem([str(i), element_str])
                    item.setData(0, COPYABLE, self.CopyType.VALUE)
                    item.setData(0, VALUE_DATA_ROLE, element_str)
                    associateDataOfType(item, element_str)
                parent.addChild(item)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                key_str = str(k)
                if isinstance(v, (dict, list, tuple, set)):
                    item = QTreeWidgetItem([key_str, ''])
                    item.setData(0, COPYABLE, self.CopyType.KEY)
                    item.setData(0, KEY_DATA_ROLE, key_str)
                    self.recursivelyAddJson(v, item, counter+1)
                    associateDataOfType(item, key_str)
                else:
                    val_str = str(v)
                    item = QTreeWidgetItem([key_str, val_str])
                    item.setData(0, COPYABLE, self.CopyType.BOTH)
                    item.setData(0, KEY_DATA_ROLE, key_str)
                    item.setData(0, VALUE_DATA_ROLE, val_str)
                    associateDataOfType(item, key_str)
                    associateDataOfType(item, val_str)

                parent.addChild(item)
        else:
            raise ValueError(f'Asset view json recursion is not valid!: {obj.__class__}')

    def update(self, json):
        self.clear()
        
        tl_elements = []
        if isinstance(json, (list, tuple, set)):
            for i, element in enumerate(json):
                if isinstance(element, (dict, list, tuple, set)):
                    item = QTreeWidgetItem([str(i), ''])
                    item.setData(0, COPYABLE, self.CopyType.NONE)
                    self.recursivelyAddJson(element, item, 0)
                else:
                    element_str = str(element)
                    item = QTreeWidgetItem([str(i), element_str])
                    item.setData(0, COPYABLE, self.CopyType.VALUE)
                    item.setData(0, VALUE_DATA_ROLE, element_str)
                    associateDataOfType(item, element_str)

                self.addTopLevelItem(item)
                tl_elements.append(item)
        elif isinstance(json, dict):
            for k, v in json.items():
                key_str = str(k)
                if isinstance(v, (dict, list, tuple, set)):
                    item = QTreeWidgetItem([key_str, ''])
                    item.setData(0, COPYABLE, self.CopyType.KEY)
                    item.setData(0, KEY_DATA_ROLE, key_str)
                    self.recursivelyAddJson(v, item, 0)
                    associateDataOfType(item, key_str)
                else:
                    val_str = str(v)
                    item = QTreeWidgetItem([key_str, val_str])
                    item.setData(0, COPYABLE, self.CopyType.BOTH)
                    item.setData(0, KEY_DATA_ROLE, key_str)
                    item.setData(0, VALUE_DATA_ROLE, val_str)
                    associateDataOfType(item, key_str)
                    associateDataOfType(item, val_str)

                self.addTopLevelItem(item)
                tl_elements.append(item)
        else:
            raise ValueError(f'Asset view json top level is not valid!: {json.__class__}')


        def recursive_expand(item: QTreeWidgetItem):
            item.setExpanded(True)
            for i in range(item.childCount()):
                recursive_expand(item.child(i))


        for element in tl_elements:
            recursive_expand(element)

        h = self.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)

        super().update()

class AssetList(MyTreeView):
    class Columns(IntEnum):
        NAME = 0
        BALANCE = 1
        #IPFS = 2
        #REISSUABLE = 3
        #DIVISIONS = 4
        #OWNER = 5

    filter_columns = [Columns.NAME]

    ROLE_SORT_ORDER = Qt.UserRole + 1000
    ROLE_ASSET_STR = Qt.UserRole + 1001

    def __init__(self, view,):
        super().__init__(view.main_window, self.create_menu,
                         stretch_column=None,
                         editable_columns=[])
        self.view = view
        self.wallet = self.view.main_window.wallet

        self.first_update = False

        self.std_model = QStandardItemModel(self)
        self.setModel(self.std_model)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.update()
        self.sortByColumn(self.Columns.NAME, Qt.AscendingOrder)


    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(e)
        idx = self.indexAt(e.pos())
        if not idx.isValid():
            return
        # Get the name from 1st column
        asset = self.model().index(idx.row(), self.Columns.NAME).data(self.ROLE_ASSET_STR)
        
        unverified_meta = self.wallet.adb.get_unverified_asset_meta(asset)
        meta = self.wallet.adb.get_asset_meta(asset)
       
        if unverified_meta or meta:
            self.view.data_viewer.update_view(meta, unverified_meta)

        return super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        idx = self.indexAt(event.pos())
        if not idx.isValid():
            return

        asset = self.model().index(idx.row(), self.Columns.NAME).data(self.ROLE_ASSET_STR)

        meta = self.wallet.adb.get_unverified_asset_meta(asset)
        if not meta or not self.view.main_window.config.get('use_mempool_metadata', True):
            meta = self.wallet.adb.get_asset_meta(asset)

        if not meta or not meta.ipfs_str:
            return

        try:
            raw_ipfs = base_decode(meta.ipfs_str, base=58)
        except Exception:
            raw_ipfs = b''
        if raw_ipfs[:2] == b'\x12\x20':
            url = ipfs_explorer_URL(self.view.main_window.config, 'ipfs', meta.ipfs_str)
            webopen_safe(self.view.main_window, meta.ipfs_str, url, self.view)
        elif raw_ipfs[:2] == b'\x54\x20' and len(raw_ipfs) == 34:
            raw_tx = self.view.main_window._fetch_tx_from_network(raw_ipfs[2:].hex(), False)
            if not raw_tx:
                self.view.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                return
            tx = transaction.Transaction(raw_tx)
            self.view.main_window.show_transaction(tx)


    def refresh_headers(self):
        headers = {
            self.Columns.NAME: _('Name'),
            self.Columns.BALANCE: _('My Amount'),
            #self.Columns.IPFS: _('Asset Data'),
            #self.Columns.REISSUABLE: _('Reissuable'),
            #self.Columns.DIVISIONS: _('Divisions'),
            #self.Columns.OWNER: _('Owner'),
        }
        self.update_headers(headers)

    @profiler
    def update(self):
        if self.maybe_defer_update():
            return

        self.std_model.clear()
        self.refresh_headers()
        set_asset = None

        assets = {}  # type: Dict[str, List[int, Optional[AssetMeta]]]

        c, u, x = self.wallet.get_balance()
        confirmed_balance: RavenValue = c
        unconfirmed_balance: RavenValue = u + x

        all_assets = {x for x in confirmed_balance.assets.keys()}
        all_assets.update({x for x in unconfirmed_balance.assets.keys()})

        for asset in sorted(all_assets):
            # Don't show hidden assets
            if not self.view.main_window.config.get('show_spam_assets', False):
                should_continue = False
                for regex in self.view.main_window.asset_blacklist:
                    if re.search(regex, asset):
                        should_continue = True
                        break
                for regex in self.view.main_window.asset_whitelist:
                    if re.search(regex, asset):
                        should_continue = False
                        break
                if should_continue:
                    continue

            asset_confirmed_balance = confirmed_balance.assets.get(asset, Satoshis(0)).value
            asset_unconfirmed_balance = unconfirmed_balance.assets.get(asset, Satoshis(0)).value

            if asset_confirmed_balance == 0 and asset_unconfirmed_balance == 0:
                continue

            confirmed_balance_text = self.view.main_window.format_amount(asset_confirmed_balance, whitespaces=True)
            unconfirmed_balance_text = self.view.main_window.format_amount(asset_unconfirmed_balance, whitespaces=True)

            balance_text = confirmed_balance_text
            if asset_unconfirmed_balance > 0:
                balance_text += f' ({unconfirmed_balance_text} unconfirmed)'

            labels = [asset, balance_text]
            asset_item = [QStandardItem(e) for e in labels]
            # align text and set fonts
            for i, item in enumerate(asset_item):
                item.setTextAlignment(Qt.AlignVCenter)
                if i not in (self.Columns.NAME,):
                    item.setFont(QFont(MONOSPACE_FONT))
            self.set_editability(asset_item)

            # add item
            count = self.std_model.rowCount()
            asset_item[self.Columns.NAME].setData(asset, self.ROLE_ASSET_STR)
            self.std_model.insertRow(count, asset_item)

        self.asset_meta = assets
        self.set_current_idx(set_asset)
        self.filter()

    def create_menu(self, position):
        org_idx: QModelIndex = self.indexAt(position)

        if not org_idx.isValid():
            return
        
        menu = QMenu()
        self.add_copy_menu(menu, org_idx)

        asset = self.model().index(org_idx.row(), self.Columns.NAME).data(self.ROLE_ASSET_STR)
        
        def send_asset(asset):
            self.view.main_window.show_send_tab()
            self.view.main_window.send_tab.to_send_combo.setCurrentIndex(self.view.main_window.send_options.index(asset))

        menu.addAction(_('Send {}').format(asset), lambda: send_asset(asset))
        
        meta: AssetMeta = self.wallet.adb.get_asset_meta(asset)

        if meta and meta.ipfs_str:
            try:
                raw_ipfs = base_decode(meta.ipfs_str, base=58)
            except Exception:
                raw_ipfs = b''
            if raw_ipfs[:2] == b'\x12\x20':
                url = ipfs_explorer_URL(self.view.main_window.config, 'ipfs', meta.ipfs_str)
                menu.addAction(_('View IPFS'), lambda: webopen_safe(self.view.main_window, meta.ipfs_str, url, self.view))
            elif raw_ipfs[:2] == b'\x54\x20' and len(raw_ipfs) == 34:
                def open_transaction():
                    raw_tx = self.view.main_window._fetch_tx_from_network(raw_ipfs[2:].hex(), False)
                    if not raw_tx:
                        self.view.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                        return
                    tx = transaction.Transaction(raw_tx)
                    self.view.main_window.show_transaction(tx)
                menu.addAction(_('View Transaction'), open_transaction)
        
        menu.addAction(_('View History'), lambda: self.view.main_window.show_asset(asset))
        menu.addAction(_('Mark as spam'), lambda: self.view.main_window.hide_asset(asset))

        menu.exec_(self.viewport().mapToGlobal(position))

    def place_text_on_clipboard(self, text: str, *, title: str = None) -> None:
        if is_address(text):
            try:
                self.wallet.check_address_for_corruption(text)
            except InternalAddressCorruption as e:
                self.view.main_window.show_error(str(e))
                raise
        super().place_text_on_clipboard(text, title=title)

    def get_edit_key_from_coordinate(self, row, col):
        return None

    # We don't edit anything here
    def on_edited(self, idx, edit_key, *, text):
        pass


# TODO: This needs serious clean-up/refactoring

class MetadataViewer(QFrame):

    TYPE_BLURBS = {
        'standard': _('This is a standard asset.'),
        'owner': _('This is an ownership asset. It is used to manage it\'s corresponding asset.'),
        'message': _('This is a message channel asset. It is primarially used to broadcast IPFS messages.'),
        'unique': _('This is a unique asset. Only one of it may ever exist and it cannot be reissued.'),
        'restricted': _('This is a restricted asset. It may only be sent to certain addresses.'),
        'qualifier': _('This is a qualifying asset. It is used to dictate what addresses may receive restricted assets.'),
    }

    update_trigger = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)

        self.main_window = parent.main_window
        self.wallet = parent.main_window.wallet
        self.current_meta = None

        self.update_trigger.connect(lambda: self.update_view(self.current_meta, None))

        self.requested_ipfses = set()

        self.header = QLabel('<h3>{}</h3>'.format(_('Asset Metadata')))
        self.header.setToolTip(_('Asset metadata is validated client-side,\nhowever, servers may broadcast old data or make-up data\n in the mempool.'))
        self.header.setAlignment(Qt.AlignCenter)

        self.generic_information = QLabel()
        self.generic_information.setToolTip(_('Client-side validation of total created is limited'))
        
        self.type_blurb = QLabel()
        self.type_blurb.setWordWrap(True)

        self.ipfs_info = QLabel()
        self.ipfs_text = QTextEdit()
        self.ipfs_text.setReadOnly(True)
        self.ipfs_text.setVisible(False)
        self.ipfs_text.setFixedHeight(50)

        self.ipfs_predicted = QLabel()
        self.ipfs_predicted.setVisible(False)

        self.ipfs_image = QLabel()
        self.ipfs_image.setVisible(False)
        self.ipfs_image.setAlignment(Qt.AlignCenter)

        self.ipfs_data_text = QTextEdit()
        
        def resize_on_text():
            size = self.ipfs_data_text.document().size().toSize()
            self.ipfs_data_text.setFixedHeight(min(200, size.height() +  3))

        self.ipfs_data_text.textChanged.connect(resize_on_text)
        self.ipfs_data_text.setReadOnly(True)
        self.ipfs_data_text.setVisible(False)

        self.ipfs_json = JsonViewWidget(self)
        self.ipfs_json.setVisible(False)

        def view_ipfs():
            url = ipfs_explorer_URL(self.main_window.config, 'ipfs', self.current_meta.ipfs_str)
            webopen_safe(self.main_window, self.current_meta.ipfs_str, url, self)
        self.view_ipfs_button = EnterButton(_('View IPFS In Browser'), view_ipfs)
        self.view_ipfs_button.setVisible(False)
        def view_tx():
            raw_ipfs = base_decode(self.current_meta.ipfs_str, base=58)
            raw_tx = self.main_window._fetch_tx_from_network(raw_ipfs[2:].hex(), False)
            if not raw_tx:
                self.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                return
            tx = transaction.Transaction(raw_tx)
            
            self.main_window.show_transaction(tx)
        self.view_tx_button = EnterButton(_('View Transaction'), view_tx)
        self.view_tx_button.setVisible(False)

        self.ipfs_layout = QVBoxLayout()
        self.ipfs_layout.addWidget(self.ipfs_info)
        self.ipfs_layout.addWidget(self.ipfs_text)
        self.ipfs_layout.addWidget(self.ipfs_predicted)
        self.ipfs_layout.addWidget(self.ipfs_image)
        self.ipfs_layout.addWidget(self.ipfs_data_text)
        self.ipfs_layout.addWidget(self.ipfs_json)
        self.ipfs_layout.addWidget(self.view_ipfs_button)
        self.ipfs_layout.addWidget(self.view_tx_button)

        self.source_text = QLabel()
        self.source_text.setWordWrap(True)
        self.source_txid = QTextEdit()
        self.source_txid.setReadOnly(True)
        self.source_txid.setFixedHeight(50)
        self.source_txid.setVisible(False)
        def view_source_tx():
            raw_tx = self.main_window._fetch_tx_from_network(self.current_meta.source_outpoint.txid.hex(), False)
            if not raw_tx:
                self.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                return
            tx = transaction.Transaction(raw_tx)
            self.main_window.show_transaction(tx)
        self.view_source_tx_button = EnterButton(_('View Transaction'), view_source_tx)
        self.view_source_tx_button.setVisible(False)

        self.source_layout = QVBoxLayout()
        self.source_layout.addWidget(self.source_text)
        self.source_layout.addWidget(self.source_txid)
        self.source_layout.addWidget(self.view_source_tx_button)

        self.ipfs_source_text = QLabel()
        self.ipfs_source_text.setWordWrap(True)
        self.ipfs_source_text.setVisible(False)
        self.ipfs_source_txid = QTextEdit()
        self.ipfs_source_txid.setReadOnly(True)
        self.ipfs_source_txid.setFixedHeight(50)
        self.ipfs_source_txid.setVisible(False)
        def view_ipfs_source_tx():
            raw_tx = self.main_window._fetch_tx_from_network(self.current_meta.source_ipfs.txid.hex(), False)
            if not raw_tx:
                self.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                return
            tx = transaction.Transaction(raw_tx)
            self.main_window.show_transaction(tx)
        self.view_ipfs_source_tx_button = EnterButton(_('View Transaction'), view_ipfs_source_tx)
        self.view_ipfs_source_tx_button.setVisible(False)

        self.ipfs_source_layout = QVBoxLayout()
        self.ipfs_source_layout.addWidget(self.ipfs_source_text)
        self.ipfs_source_layout.addWidget(self.ipfs_source_txid)
        self.ipfs_source_layout.addWidget(self.view_ipfs_source_tx_button)

        self.div_source_text = QLabel()
        self.div_source_text.setWordWrap(True)
        self.div_source_text.setVisible(False)
        self.div_source_txid = QTextEdit()
        self.div_source_txid.setReadOnly(True)
        self.div_source_txid.setFixedHeight(50)
        self.div_source_txid.setVisible(False)
        def view_div_source_tx():
            raw_tx = self.main_window._fetch_tx_from_network(self.current_meta.source_divisions.txid.hex(), False)
            if not raw_tx:
                self.main_window.show_message(_("This transaction is not on the Ravencoin blockchain."))
                return
            tx = transaction.Transaction(raw_tx)
            self.main_window.show_transaction(tx)
        self.view_div_source_tx_button = EnterButton(_('View Transaction'), view_div_source_tx)
        self.view_div_source_tx_button.setVisible(False)

        self.div_source_layout = QVBoxLayout()
        self.div_source_layout.addWidget(self.div_source_text)
        self.div_source_layout.addWidget(self.div_source_txid)
        self.div_source_layout.addWidget(self.view_div_source_tx_button)

        v_layout = QVBoxLayout()
        self.setLayout(v_layout)
        scroll = QScrollArea(self)
        v_layout.addWidget(scroll)
        scroll.setWidgetResizable(True)
        scroll_content = QWidget(scroll)
        scrollLayout = QVBoxLayout(scroll_content)
        scroll_content.setLayout(scrollLayout)
        
        scrollLayout.addWidget(self.header)
        scrollLayout.addWidget(self.generic_information)
        scrollLayout.addWidget(QHSeperationLine())
        scrollLayout.addLayout(self.ipfs_layout)
        scrollLayout.addWidget(QHSeperationLine())
        scrollLayout.addWidget(self.type_blurb)
        scrollLayout.addWidget(QHSeperationLine())
        scrollLayout.addLayout(self.source_layout)
        scrollLayout.addLayout(self.ipfs_source_layout)
        scrollLayout.addLayout(self.div_source_layout)
        scrollLayout.setSpacing(10)

        scroll.setWidget(scroll_content)

    def update_view(self, confirmed_meta: Optional[AssetMeta], unverified_meta: Optional[AssetMeta]):

        meta = confirmed_meta if not self.main_window.config.get('use_mempool_metadata', True) else (unverified_meta or confirmed_meta)
        self.current_meta = meta

        async def get_data_on_ipfs(requested_ipfs: str, url: str):
            await try_download_ipfs(self.main_window, requested_ipfs, self.requested_ipfses, url, not self.main_window.config.get('download_all_ipfs', False), self.update_trigger.emit)
            return requested_ipfs

        if meta.height > 0:
            self.header.setText('<h3>{}</h3>'.format(_('Asset Metadata')))
        else:
            self.header.setText('<h3>{}</h3>'.format(_('Asset Metadata (Unconfirmed)')))

        self.generic_information.setText('<b>Name:</b> {}<br><b>Smallest Unit:</b> {}<br><b>Reissuable:</b> {}<br><b>Total Created:</b> {}'.format(
            meta.name,
            pow(10, -meta.divisions),
            meta.is_reissuable,
            min_num_str(str(round(meta.circulation / COIN, meta.divisions)))
        ))

        if meta.name[-1] == '!':
            blurb_type = 'owner'
        elif '~' in meta.name:
            blurb_type = 'message'
        elif meta.name[0] == '#':
            blurb_type = 'qualifier'
        elif meta.name[0] == '$':
            blurb_type = 'restricted'
        elif '#' in meta.name:
            blurb_type = 'unique'
        else:
            blurb_type = 'standard'
        self.type_blurb.setText(self.TYPE_BLURBS[blurb_type])

        self.view_ipfs_button.setVisible(False)
        self.view_tx_button.setVisible(False)
        self.ipfs_predicted.setVisible(False)
        self.ipfs_image.setVisible(False)
        self.ipfs_data_text.setVisible(False)
        self.ipfs_json.setVisible(False)

        if meta.ipfs_str:
            self.ipfs_text.setVisible(True)

            raw_ipfs = base_decode(meta.ipfs_str, base=58)
            if raw_ipfs[:2] == b'\x12\x20':
                ipfs_text = _('This asset has an associated IPFS hash:')
                self.ipfs_text.setText(meta.ipfs_str)
                self.ipfs_predicted.setVisible(True)
                self.view_ipfs_button.setVisible(True)
                self.ipfs_predicted.setText(_('Loading data...'))

                ipfs_data: IPFSData = self.wallet.adb.get_ipfs_information(meta.ipfs_str)
                if ipfs_data:
                    self.ipfs_predicted.setText('Predicted Content Type: {}\nPredicted Size: {}'.format(ipfs_data.mime_type or _('Unknown'), human_readable_size(ipfs_data.byte_length)))
                    if ipfs_data.is_cached:
                        if ipfs_data.mime_type and 'image' in ipfs_data.mime_type:

                            ipfs_path = get_ipfs_path(self.main_window.config, meta.ipfs_str)

                            if os.path.exists(ipfs_path):
                                self.ipfs_predicted.setVisible(False)
                                # TODO: This should trigger on resize, not just one-off
                                pixmap = QPixmap(ipfs_path)
                                if pixmap.width() > self.width() - 50:
                                    pixmap = pixmap.scaled(self.width() - 50, pixmap.height(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
                                self.ipfs_image.setPixmap(pixmap)
                                self.ipfs_image.setVisible(True)

                        elif ipfs_data.mime_type and 'text/plain' == ipfs_data.mime_type:
                            ipfs_path = get_ipfs_path(self.main_window.config, meta.ipfs_str)

                            if os.path.exists(ipfs_path):
                                self.ipfs_predicted.setVisible(False)
                                self.ipfs_data_text.setVisible(True)
                                with open(ipfs_path, 'r') as f:
                                    self.ipfs_data_text.setText(f.read())

                        elif ipfs_data.mime_type and 'json' in ipfs_data.mime_type:
                            ipfs_path = get_ipfs_path(self.main_window.config, meta.ipfs_str)
                        
                            if os.path.exists(ipfs_path):
                                self.ipfs_predicted.setVisible(False)
                                self.ipfs_json.setVisible(True)
                                with open(ipfs_path, 'r') as f:
                                    self.ipfs_json.update(json.load(f))

                    elif meta.ipfs_str in self.requested_ipfses:
                        self.ipfs_predicted.setText(_('Loading data...'))
                    else:
                        if meta.ipfs_str not in self.requested_ipfses and \
                            self.main_window.config.get('download_all_ipfs', False) and \
                                ipfs_data.mime_type and ('image' in ipfs_data.mime_type or 'text/plain' == ipfs_data.mime_type or 'json' in ipfs_data.mime_type) and \
                                    ipfs_data.byte_length and ipfs_data.byte_length <= self.main_window.config.get('max_ipfs_size', 1024 * 1024 * 10):
                            loop = get_asyncio_loop()
                            url = ipfs_explorer_URL(self.main_window.config, 'ipfs', meta.ipfs_str)
                            task = loop.create_task(get_data_on_ipfs(meta.ipfs_str, url))
                            def maybe_update_view(task: asyncio.Task):
                                if task.result() == self.current_meta.ipfs_str:
                                    self.update_trigger.emit()
                            task.add_done_callback(maybe_update_view)

                elif meta.ipfs_str not in self.requested_ipfses:
                    loop = get_asyncio_loop()
                    url = ipfs_explorer_URL(self.main_window.config, 'ipfs', meta.ipfs_str)
                    task = loop.create_task(get_data_on_ipfs(meta.ipfs_str, url))
                    def maybe_update_view(task: asyncio.Task):
                        if task.result() == self.current_meta.ipfs_str:
                            self.update_trigger.emit()
                    task.add_done_callback(maybe_update_view)

            elif raw_ipfs[:2] == b'\x54\x20':
                ipfs_text = _('This asset has an associated txid:')
                self.ipfs_text.setText(raw_ipfs[2:].hex())
                self.view_tx_button.setVisible(True)
            else:
                ipfs_text = _('This asset has associated base58:')
                self.ipfs_text.setText(meta.ipfs_str)
        
        else:
            ipfs_text = _('This asset has no associated IPFS hash.')
            self.ipfs_text.setVisible(False)

        self.ipfs_info.setText(ipfs_text)

        # All assets have a source
        source_text = _('This asset\'s metadata was last modified ')
        if meta.height > 0:
            source_text += _('in block {}').format(meta.height)
        else:
            source_text += _('in the mempool')
        self.source_text.setText(source_text)
        self.source_txid.setText(meta.source_outpoint.txid.hex())
        self.source_txid.setVisible(True)
        self.view_source_tx_button.setVisible(True)

        if meta.source_ipfs:
            self.ipfs_source_text.setVisible(True)
            self.ipfs_source_txid.setVisible(True)
            self.view_ipfs_source_tx_button.setVisible(True)

            ipfs_source_text = _('This asset\'s IPFS hash was last modified ')
            if meta.ipfs_height > 0:
                ipfs_source_text += _('in block {}').format(meta.ipfs_height)
            else:
                ipfs_source_text += _('in the mempool')
            self.ipfs_source_text.setText(ipfs_source_text)
            self.ipfs_source_txid.setText(meta.source_ipfs.txid.hex())            
        else:
            self.ipfs_source_text.setVisible(False)
            self.ipfs_source_txid.setVisible(False)
            self.view_ipfs_source_tx_button.setVisible(False)

        
        if meta.source_divisions:
            self.div_source_text.setVisible(True)
            self.div_source_txid.setVisible(True)
            self.view_div_source_tx_button.setVisible(True)

            div_source_text = _('This asset\'s divisions were last modified ')
            if meta.div_height > 0:
                div_source_text += _('in block {}').format(meta.div_height)
            else:
                div_source_text += _('in the mempool')
            self.div_source_text.setText(div_source_text)
            self.div_source_txid.setText(meta.source_divisions.txid.hex())            
        else:
            self.div_source_text.setVisible(False)
            self.div_source_txid.setVisible(False)
            self.view_div_source_tx_button.setVisible(False)

class AssetView(QSplitter):
    def __init__(self, window):
        super().__init__(window)
        self.main_window = window
        self.data_viewer = MetadataViewer(self)
        self.asset_list = AssetList(self)
        self.asset_list.setMinimumWidth(300)
        self.data_viewer.setMinimumWidth(400)
        self.setChildrenCollapsible(False)
        self.addWidget(self.asset_list)
        self.addWidget(self.data_viewer)
        

    def update(self):
        self.asset_list.update()
        if self.data_viewer.current_meta:
            new_meta = self.main_window.wallet.adb.get_asset_meta(self.data_viewer.current_meta.name)
            if new_meta:
                self.data_viewer.update_view(new_meta, None)
