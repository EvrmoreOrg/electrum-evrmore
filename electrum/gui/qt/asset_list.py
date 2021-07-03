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

from enum import IntEnum
from typing import List, Dict
import re

from PyQt5.QtCore import Qt, QPersistentModelIndex, QModelIndex
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QMouseEvent
from PyQt5.QtWidgets import QAbstractItemView, QComboBox, QLabel, QMenu, QCheckBox

from electrum.i18n import _
from electrum.util import ipfs_explorer_URL, profiler, get_alternate_data
from electrum.plugin import run_hook
from electrum.ravencoin import is_address, base_decode
from electrum.wallet import InternalAddressCorruption
from electrum.transaction import AssetMeta

from .util import MyTreeView, MONOSPACE_FONT, webopen, MySortModel


class AssetList(MyTreeView):
    class Columns(IntEnum):
        NAME = 0
        BALANCE = 1
        IPFS = 2
        REISSUABLE = 3
        DIVISIONS = 4
        OWNER = 5

    filter_columns = [Columns.NAME, Columns.BALANCE, Columns.IPFS, Columns.REISSUABLE, Columns.DIVISIONS]

    ROLE_SORT_ORDER = Qt.UserRole + 1000
    ROLE_ASSET_STR = Qt.UserRole + 1001

    def __init__(self, parent):
        super().__init__(parent, self.create_menu,
                         stretch_column=None,
                         editable_columns=[])
        self.wallet = self.parent.wallet
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.std_model = QStandardItemModel(self)
        self.proxy = MySortModel(self, sort_role=self.ROLE_SORT_ORDER)
        self.proxy.setSourceModel(self.std_model)
        self.setModel(self.proxy)
        self.update()
        self.sortByColumn(self.Columns.NAME, Qt.AscendingOrder)
        self.asset_meta = {}

    def webopen_safe(self, url):
        show_warn = self.parent.config.get('show_ipfs_warning', True)
        if show_warn:
            cb = QCheckBox(_("Don't show this message again."))
            cb_checked = False

            def on_cb(x):
                nonlocal cb_checked
                cb_checked = x == Qt.Checked

            cb.stateChanged.connect(on_cb)
            goto = self.parent.question(_('You are about to visit:\n\n'
                                          '{}\n\n'
                                          'IPFS hashes can link to anything. Please follow '
                                          'safe practices and common sense. If you are unsure '
                                          'about what\'s on the other end of an IPFS, don\'t '
                                          'visit it!\n\n'
                                          'Are you sure you want to continue?').format(url),
                                        title=_('Warning: External Data'), checkbox=cb)

            if cb_checked:
                self.parent.config.set_key('show_ipfs_warning', False)
            if goto:
                webopen(url)
        else:
            webopen(url)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        idx = self.indexAt(event.pos())
        if not idx.isValid():
            return

        # Get the IPFS from 3rd column
        hm_idx = self.model().mapToSource(self.model().index(idx.row(), 2))
        data = self.std_model.data(hm_idx)
        if data[:2] == 'Qm':  # If it starts with Qm, it's an IPFS
            url = ipfs_explorer_URL(self.parent.config, 'ipfs', data)
            self.webopen_safe(url)

    def refresh_headers(self):
        headers = {
            self.Columns.NAME: _('Name'),
            self.Columns.BALANCE: _('Amount'),
            self.Columns.IPFS: _('Asset Data'),
            self.Columns.REISSUABLE: _('Reissuable'),
            self.Columns.DIVISIONS: _('Divisions'),
            self.Columns.OWNER: _('Owner'),
        }
        self.update_headers(headers)

    @profiler
    def update(self):
        if self.maybe_defer_update():
            return
        current_asset = self.get_role_data_for_current_item(col=self.Columns.NAME, role=self.ROLE_ASSET_STR)
        addr_list = self.wallet.get_addresses()
        self.proxy.setDynamicSortFilter(False)  # temp. disable re-sorting after every change
        self.std_model.clear()
        self.asset_meta.clear()
        self.refresh_headers()
        set_asset = None

        assets = {}  # type: Dict[str, List[int, Optional[AssetMeta]]]

        for address in addr_list:
            c, u, x = self.wallet.get_addr_balance(address)
            balance = c + u + x

            # Don't display assets we no longer have
            if len(balance.assets) == 0:
                continue

            for asset, balance in balance.assets.items():
                # Don't show hidden assets
                if not self.parent.config.get('show_spam_assets', False):
                    should_continue = False
                    for regex in self.parent.asset_blacklist:
                        if re.search(regex, asset):
                            should_continue = True
                            break
                    for regex in self.parent.asset_whitelist:
                        if re.search(regex, asset):
                            should_continue = False
                            break
                    if should_continue:
                        continue

                if asset not in assets:
                    meta = self.wallet.get_asset_meta(asset)
                    assets[asset] = [balance.value, meta]
                else:
                    assets[asset][0] += balance.value

        for asset, data in assets.items():

            balance = data[0]
            meta = data[1]  # type: AssetMeta

            balance_text = self.parent.format_amount(balance, whitespaces=True)

            if self.config.get('advanced_asset_functions', False):
                if meta and meta.ipfs_str:
                    s = meta.ipfs_str
                    h, a = get_alternate_data(base_decode(s, base=58))
                    ipfs_str = '\nBASE58: {}\nHEX: {}\nLATIN-1: {}\n'.format(s, h, a)
                else:
                    ipfs_str = '\nBASE58: None\nHEX: None\nLATIN-1: None\n'
            else:
                ipfs_str = str(meta.ipfs_str) if meta else ''  # May be none

            is_reis = str(meta.is_reissuable) if meta else ''
            divs = str(meta.divisions) if meta else ''
            ownr = str(meta.is_owner) if meta else ''

            # create item
            labels = [asset, balance_text, ipfs_str, is_reis, divs, ownr]
            asset_item = [QStandardItem(e) for e in labels]
            # align text and set fonts
            for i, item in enumerate(asset_item):
                item.setTextAlignment(Qt.AlignVCenter)
                if i not in (self.Columns.NAME, self.Columns.IPFS):
                    item.setFont(QFont(MONOSPACE_FONT))
            self.set_editability(asset_item)

            # add item
            count = self.std_model.rowCount()
            self.std_model.insertRow(count, asset_item)

        self.asset_meta = assets
        self.set_current_idx(set_asset)
        self.filter()
        self.proxy.setDynamicSortFilter(True)

    def add_copy_menu(self, menu, idx):
        cc = menu.addMenu(_("Copy"))
        for column in self.Columns:
            if self.isColumnHidden(column):
                continue
            column_title = self.model().headerData(column, Qt.Horizontal)
            hm_idx = self.model().mapToSource(self.model().index(idx.row(), column))
            column_data = self.std_model.data(hm_idx)
            cc.addAction(
               column_title,
               lambda text=column_data, title=column_title:
               self.place_text_on_clipboard(text, title=title))
        return cc

    def create_menu(self, position):
        org_idx: QModelIndex = self.indexAt(position)

        hm_idx = self.model().mapToSource(self.model().index(org_idx.row(), 0))
        if not hm_idx.isValid():
            return
        asset = self.std_model.data(hm_idx)

        hm_idx = self.model().mapToSource(self.model().index(org_idx.row(), 2))
        if not hm_idx.isValid():
            return
        ipfs = self.std_model.data(hm_idx)

        menu = QMenu()
        self.add_copy_menu(menu, org_idx)

        def send_asset(asset):
            self.parent.show_send_tab()
            self.parent.to_send_combo.setCurrentIndex(self.parent.send_options.index(asset))

        menu.addAction(_('Send {}').format(asset), lambda: send_asset(asset))
        if ipfs[:2] == 'Qm':
            url = ipfs_explorer_URL(self.parent.config, 'ipfs', ipfs)
            menu.addAction(_('View IPFS'), lambda: self.webopen_safe(url))
        menu.addAction(_('View History'), lambda: self.parent.show_asset(asset))
        menu.addAction(_('Mark as spam'), lambda: self.parent.hide_asset(asset))

        menu.exec_(self.viewport().mapToGlobal(position))

    def place_text_on_clipboard(self, text: str, *, title: str = None) -> None:
        if is_address(text):
            try:
                self.wallet.check_address_for_corruption(text)
            except InternalAddressCorruption as e:
                self.parent.show_error(str(e))
                raise
        super().place_text_on_clipboard(text, title=title)

    def get_edit_key_from_coordinate(self, row, col):
        return None

    # We don't edit anything here
    def on_edited(self, idx, edit_key, *, text):
        pass
