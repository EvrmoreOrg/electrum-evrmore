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
import base64
from distutils.version import StrictVersion
from enum import IntEnum
from typing import List, Dict
import re

from PyQt5.QtCore import Qt, QPersistentModelIndex, QModelIndex, QThread, pyqtSignal
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QMouseEvent
from PyQt5.QtWidgets import QAbstractItemView, QComboBox, QLabel, QMenu, QCheckBox

from electrum.i18n import _
from electrum.util import ipfs_explorer_URL, profiler, get_alternate_data, make_aiohttp_session
from electrum.plugin import run_hook
from electrum.ravencoin import is_address, base_decode
from electrum.wallet import InternalAddressCorruption
from electrum.transaction import AssetMeta
from .update_checker import VERSION_ANNOUNCEMENT_SIGNING_KEYS

from .util import MyTreeView, MONOSPACE_FONT, webopen, MySortModel
from ... import Network, ecc, constants
from ...logging import Logger


class UpdateDevMessagesThread(QThread, Logger):
    url = "https://raw.githubusercontent.com/Electrum-RVN-SIG/electrum-ravencoin/master/dev-notifications.json"

    def __init__(self, parent):
        QThread.__init__(self)
        Logger.__init__(self)
        self.network = Network.get_instance()
        self.parent = parent

    async def get_messages_info(self):
        async with make_aiohttp_session(proxy=self.network.proxy, timeout=120) as session:
            async with session.get(self.url) as result:
                signed_version_dict = await result.json(content_type=None)
                # example signed_version_dict:
                # {
                #     "version": "3.9.9",
                #     "signatures": {
                #         "1Lqm1HphuhxKZQEawzPse8gJtgjm9kUKT4": "IA+2QG3xPRn4HAIFdpu9eeaCYC7S5wS/sDxn54LJx6BdUTBpse3ibtfq8C43M7M1VfpGkD5tsdwl5C6IfpZD/gQ="
                #     }
                # }
                message = signed_version_dict['message']
                sigs = signed_version_dict['signatures']
                for address, sig in sigs.items():
                    if address not in VERSION_ANNOUNCEMENT_SIGNING_KEYS:
                        raise Exception('Address not in annoucement keys')
                    sig = base64.b64decode(sig)
                    msg = message.encode('utf-8')
                    if ecc.verify_message_with_address(address=address, sig65=sig, message=msg,
                                                       net=constants.RavencoinMainnet):
                        break
                else:
                    raise Exception('No items')
                return message.split('{:}')

    def run(self):
        if not self.network:
            return
        try:
            update_info = asyncio.run_coroutine_threadsafe(self.get_messages_info(), self.network.asyncio_loop).result()
        except Exception as e:
            self.logger.info(f"got exception: '{repr(e)}'")
        else:
            self.parent.wallet.add_message(int(update_info[0]), ('DEVELOPER\nANNOUNCEMENT', update_info[1], None))


class MessageList(MyTreeView):
    class Columns(IntEnum):
        HEIGHT = 0
        FROM = 1
        DATA = 2

    filter_columns = [Columns.HEIGHT, Columns.FROM, Columns.DATA]

    ROLE_SORT_ORDER = Qt.UserRole + 1000
    ROLE_ASSET_STR = Qt.UserRole + 1001

    def __init__(self, parent):
        super().__init__(parent, self.create_menu,
                         stretch_column=self.Columns.DATA,
                         editable_columns=[])
        self.wallet = self.parent.wallet
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.std_model = QStandardItemModel(self)
        self.proxy = MySortModel(self, sort_role=self.ROLE_SORT_ORDER)
        self.proxy.setSourceModel(self.std_model)
        self.setModel(self.proxy)
        self.update()
        self.sortByColumn(self.Columns.HEIGHT, Qt.AscendingOrder)
        self.messages = {}

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
            self.Columns.HEIGHT: _('Height'),
            self.Columns.FROM: _('From'),
            self.Columns.DATA: _('Data'),
        }
        self.update_headers(headers)

    @profiler
    def update(self):
        if self.maybe_defer_update():
            return
        self.messages = self.wallet.get_messages()
        self.proxy.setDynamicSortFilter(False)  # temp. disable re-sorting after every change
        self.std_model.clear()
        self.refresh_headers()
        set_message = None

        for height, data in self.messages.items():
            for d in data:
                is_from = d[0]
                message_txt = d[1]

                if not self.parent.config.get('get_dev_notifications', True) and d[2] is None:
                    # The source will only be null if this is a dev notification
                    continue

                # create item
                labels = [height, is_from, message_txt]
                asset_item = [QStandardItem(e) for e in labels]
                # align text and set fonts
                for i, item in enumerate(asset_item):
                    item.setTextAlignment(Qt.AlignVCenter)

                # add item
                count = self.std_model.rowCount()
                self.std_model.insertRow(count, asset_item)

        self.set_current_idx(set_message)
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

        menu = QMenu()
        self.add_copy_menu(menu, org_idx)

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
