#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@gitorious
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
import sys
import time
import threading
import os
import traceback
import json
import weakref
import csv
from decimal import Decimal
import base64
from functools import partial
import queue
import asyncio
from typing import Optional, TYPE_CHECKING, Sequence, List, Union, Dict, Set
import concurrent.futures

from PyQt5.QtGui import QPixmap, QKeySequence, QIcon, QCursor, QFont
from PyQt5.QtCore import Qt, QRect, QStringListModel, QSize, pyqtSignal
from PyQt5.QtWidgets import (QMessageBox, QSystemTrayIcon, QTabWidget,
                             QMenuBar, QFileDialog, QCheckBox, QLabel,
                             QVBoxLayout, QGridLayout, QLineEdit,
                             QHBoxLayout, QPushButton, QScrollArea, QTextEdit,
                             QShortcut, QMainWindow, QInputDialog,
                             QWidget, QSizePolicy, QStatusBar, QToolTip,
                             QMenu, QAction, QStackedWidget, QToolButton)

import electrum
from electrum.blockchain import DGW_PASTBLOCKS, hash_header
from electrum.gui import messages
from electrum import (keystore, ecc, constants, util, evrmore, commands,
                      paymentrequest, lnutil)
from electrum.evrmore import COIN, is_address, base_decode, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC, address_to_scripthash
from electrum.plugin import run_hook, BasePlugin
from electrum.i18n import _
from electrum.util import (EvrmoreValue, format_time, get_asyncio_loop,
                           UserCancelled, profiler,
                           bh2u, bfh, InvalidPassword,
                           UserFacingException,
                           get_new_wallet_name, send_exception_to_crash_reporter,
                           AddTransactionException, BITCOIN_BIP21_URI_SCHEME)
from electrum.invoices import PR_PAID, Invoice
from electrum.transaction import (Transaction, PartialTxInput,
                                  PartialTransaction, PartialTxOutput)
from electrum.wallet import (Multisig_Wallet, Abstract_Wallet,
                             sweep_preparations, InternalAddressCorruption,
                             CannotCPFP)
from electrum.version import ELECTRUM_VERSION
from electrum.network import Network, UntrustedServerReturnedError, NetworkException
from electrum.exchange_rate import FxThread
from electrum.simple_config import SimpleConfig
from electrum.logging import Logger
from electrum.lnutil import ln_dummy_address, extract_nodeid, ConnStringFormatError
from electrum.lnaddr import lndecode

from .asset_workspace import AssetCreateWorkspace, AssetReissueWorkspace

from .exception_window import Exception_Hook
from .amountedit import EVRAmountEdit
from .qrcodewidget import QRDialog
from .qrtextedit import ShowQRTextEdit, ScanQRTextEdit, ScanShowQRTextEdit
from .transaction_dialog import show_transaction
from .fee_slider import FeeSlider, FeeComboBox
from .util import (read_QIcon, ColorScheme, text_dialog, icon_path, WaitingDialog,
                   WindowModalDialog, ChoicesLayout, HelpLabel, Buttons,
                   OkButton, InfoButton, WWLabel, TaskThread, CancelButton,
                   CloseButton, HelpButton, MessageBoxMixin, EnterButton,
                   import_meta_gui, export_meta_gui,
                   filename_field, address_field, char_width_in_lineedit, webopen,
                   TRANSACTION_FILE_EXTENSION_FILTER_ANY, MONOSPACE_FONT,
                   getOpenFileName, getSaveFileName, BlockingWaitingDialog)
from .util import ButtonsLineEdit, ShowQRLineEdit
from .util import QtEventListener, qt_event_listener, event_listener
from .installwizard import WIF_HELP_TEXT
from .history_list import HistoryList, HistoryModel
from .update_checker import UpdateCheck, UpdateCheckThread
from .channels_list import ChannelsList
from .confirm_tx_dialog import ConfirmTxDialog
from .rbf_dialog import BumpFeeDialog, DSCancelDialog
from ...assets import is_main_asset_name_good, is_sub_asset_name_good, is_unique_asset_name_good
from .qrreader import scan_qrcode
from electrum import assets
from .swap_dialog import SwapDialog
from .balance_dialog import BalanceToolButton, COLOR_FROZEN, COLOR_UNMATURED, COLOR_UNCONFIRMED, COLOR_CONFIRMED, COLOR_LIGHTNING, COLOR_FROZEN_LIGHTNING

if TYPE_CHECKING:
    from . import ElectrumGui

LN_NUM_PAYMENT_ATTEMPTS = 10


class StatusBarButton(QToolButton):
    # note: this class has a custom stylesheet applied in stylesheet_patcher.py
    def __init__(self, icon, tooltip, func):
        QToolButton.__init__(self)
        self.setText('')
        self.setIcon(icon)
        self.setToolTip(tooltip)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setAutoRaise(True)
        self.setMaximumWidth(25)
        self.clicked.connect(self.onPress)
        self.func = func
        self.setIconSize(QSize(25, 25))
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def onPress(self, checked=False):
        '''Drops the unwanted PyQt5 "checked" argument'''
        self.func()

    def keyPressEvent(self, e):
        if e.key() in [Qt.Key_Return, Qt.Key_Enter]:
            self.func()


def protected(func):
    '''Password request wrapper.  The password is passed to the function
        as the 'password' named argument.  "None" indicates either an
        unencrypted wallet, or the user cancelled the password request.
        An empty input is passed as the empty string.'''

    def request_password(self, *args, **kwargs):
        parent = self.top_level_window()
        password = None
        while self.wallet.has_keystore_encryption():
            password = self.password_dialog(parent=parent)
            if password is None:
                # User cancelled password input
                return
            try:
                self.wallet.check_password(password)
                break
            except Exception as e:
                self.show_error(str(e), parent=parent)
                continue

        kwargs['password'] = password
        return func(self, *args, **kwargs)

    return request_password


class ElectrumWindow(QMainWindow, MessageBoxMixin, Logger, QtEventListener):

    computing_privkeys_signal = pyqtSignal()
    show_privkeys_signal = pyqtSignal()
    show_error_signal = pyqtSignal(str)

    def __init__(self, gui_object: 'ElectrumGui', wallet: Abstract_Wallet):
        QMainWindow.__init__(self)
        self.gui_object = gui_object
        self.config = config = gui_object.config  # type: SimpleConfig
        self.gui_thread = gui_object.gui_thread
        assert wallet, "no wallet"
        self.wallet = wallet
        # if wallet.has_lightning():
        #    self.wallet.config.set_key('show_channels_tab', True)

        self.asset_blacklist = self.wallet.config.get('asset_blacklist', [])
        self.asset_whitelist = self.wallet.config.get('asset_whitelist', [])

        # Tracks sendable things
        self.send_options = ['EVR']  # type: List[str]

        Exception_Hook.maybe_setup(config=self.config, wallet=self.wallet)

        self.network = gui_object.daemon.network  # type: Network
        self.fx = gui_object.daemon.fx  # type: FxThread
        self.contacts = wallet.contacts
        self.tray = gui_object.tray
        self.app = gui_object.app
        self._cleaned_up = False
        self.qr_window = None
        self.pluginsdialog = None
        self.showing_cert_mismatch_error = False
        self.tl_windows = []
        Logger.__init__(self)

        self._coroutines_scheduled = {}  # type: Dict[concurrent.futures.Future, str]
        self.thread = TaskThread(self, self.on_error)

        self.tx_notification_queue = queue.Queue()
        self.tx_notification_last_time = 0

        self.create_status_bar()
        self.need_update = threading.Event()

        self.completions = QStringListModel()

        coincontrol_sb = self.create_coincontrol_statusbar()

        self.tabs = tabs = QTabWidget(self)
        # We depend on the utxo tab in the send tab now
        # Circular dependencies ensue: TODO: Fix
        self.utxo_list = None
        self.send_tab = self.create_send_tab()
        self.update_sendable_and_send_tab()
        self.receive_tab = self.create_receive_tab()
        self.addresses_tab = self.create_addresses_tab()
        self.utxo_tab = self.create_utxo_tab()
        self.assets_tab = self.create_assets_tab()
        self.console_tab = self.create_console_tab()
        self.contacts_tab = self.create_contacts_tab()
        # self.messages_tab = self.create_messages_tab()
        # self.swap_tab = self.create_swap_tab()
        # self.channels_tab = self.create_channels_tab()

        self.history_tab = self.create_history_tab()
        history_tab_widget = QWidget()
        self.history_tab_layout = QVBoxLayout()
        self.history_tab_layout.setAlignment(Qt.AlignCenter)
        self.history_tab_layout.addWidget(self.history_tab)
        history_tab_widget.setLayout(self.history_tab_layout)
        tabs.addTab(history_tab_widget, read_QIcon("tab_history.png"), _('History'))

        tabs.addTab(self.assets_tab, read_QIcon('tab_assets.png'), _('Assets'))
        tabs.addTab(self.send_tab, read_QIcon("tab_send.png"), _('Send'))
        tabs.addTab(self.receive_tab, read_QIcon("tab_receive.png"), _('Receive'))
        #tabs.addTab(self.swap_tab, read_QIcon("tab_swap.png"), _('Atomic Swap'))

        def add_optional_tab(tabs, tab, icon, description, name, default=False):
            tab.tab_icon = icon
            tab.tab_description = description
            tab.tab_pos = len(tabs)
            tab.tab_name = name
            if self.config.get('show_{}_tab'.format(name), default):
                tabs.addTab(tab, icon, description.replace("&", ""))

        #add_optional_tab(tabs, self.messages_tab, read_QIcon("tab_message.png"), _("Messages"), "messages")
        add_optional_tab(tabs, self.addresses_tab, read_QIcon("tab_addresses.png"), _("&Addresses"), "addresses")
        # add_optional_tab(tabs, self.channels_tab, read_QIcon("lightning.png"), _("Channels"), "channels")
        add_optional_tab(tabs, self.utxo_tab, read_QIcon("tab_coins.png"), _("Co&ins"), "utxo")
        add_optional_tab(tabs, self.contacts_tab, read_QIcon("tab_contacts.png"), _("Con&tacts"), "contacts")
        add_optional_tab(tabs, self.console_tab, read_QIcon("tab_console.png"), _("Con&sole"), "console")

        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        central_widget = QScrollArea()
        vbox = QVBoxLayout(central_widget)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.addWidget(tabs)
        vbox.addWidget(coincontrol_sb)

        self.setCentralWidget(central_widget)

        self.setMinimumWidth(640)
        self.setMinimumHeight(400)
        if self.config.get("is_maximized"):
            self.showMaximized()

        self.setWindowIcon(read_QIcon("electrum-evrmore.png"))
        self.init_menubar()

        wrtabs = weakref.proxy(tabs)
        QShortcut(QKeySequence("Ctrl+W"), self, self.close)
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
        QShortcut(QKeySequence("Ctrl+R"), self, self.update_wallet)
        QShortcut(QKeySequence("F5"), self, self.update_wallet)
        QShortcut(QKeySequence("Ctrl+PgUp"), self,
                  lambda: wrtabs.setCurrentIndex((wrtabs.currentIndex() - 1) % wrtabs.count()))
        QShortcut(QKeySequence("Ctrl+PgDown"), self,
                  lambda: wrtabs.setCurrentIndex((wrtabs.currentIndex() + 1) % wrtabs.count()))

        for i in range(wrtabs.count()):
            QShortcut(QKeySequence("Alt+" + str(i + 1)), self, lambda i=i: wrtabs.setCurrentIndex(i))

        self.app.refresh_tabs_signal.connect(self.refresh_tabs)
        self.app.refresh_amount_edits_signal.connect(self.refresh_amount_edits)
        self.app.update_status_signal.connect(self.update_status)
        self.app.update_fiat_signal.connect(self.update_fiat)

        self.show_error_signal.connect(self.show_error)
        self.history_list.setFocus()

        # network callbacks
        self.register_callbacks()
        # banner may already be there
        if self.network and self.network.banner:
            self.console.showMessage(self.network.banner)

        # update fee slider in case we missed the callback
        # self.fee_slider.update()
        self.load_wallet(wallet)
        gui_object.timer.timeout.connect(self.timer_actions)
        self.contacts.fetch_openalias(self.config)

        # If the option hasn't been set yet
        if config.get('check_updates') is None:
            choice = self.question(title="Electrum - " + _("Enable update check"),
                                   msg=_(
                                       "For security reasons we advise that you always use the latest version of Electrum.") + " " +
                                       _("Would you like to be notified when there is a newer version of Electrum available?"))
            config.set_key('check_updates', bool(choice), save=True)

        self._update_check_thread = None
        if config.get('check_updates', False):
            # The references to both the thread and the window need to be stored somewhere
            # to prevent GC from getting in our way.
            def on_version_received(v):
                if UpdateCheck.is_newer(v):
                    self.update_check_button.setText(_("Update to Electrum-Evrmore {} is available").format(v))
                    self.update_check_button.clicked.connect(lambda: self.show_update_check(v))
                    self.update_check_button.show()

            self._update_check_thread = UpdateCheckThread()
            self._update_check_thread.checked.connect(on_version_received)
            self._update_check_thread.start()

            #self._dev_notification_thread = None
            #if config.get('get_dev_notifications', True):
            #    self._dev_notification_thread = UpdateDevMessagesThread(self)
            #    self._dev_notification_thread.start()

    def run_coroutine_from_thread(self, coro, name, on_result=None):
        if self._cleaned_up:
            self.logger.warning(f"stopping or already stopped but run_coroutine_from_thread was called.")
            return
        async def wrapper():
            try:
                res = await coro
            except Exception as e:
                self.logger.exception("exception in coro scheduled via window.wallet")
                self.show_error_signal.emit(repr(e))
            else:
                if on_result:
                    on_result(res)
            finally:
                self._coroutines_scheduled.pop(fut)
                self.need_update.set()

        fut = asyncio.run_coroutine_threadsafe(wrapper(), self.network.asyncio_loop)
        self._coroutines_scheduled[fut] = name
        self.need_update.set()

    def on_fx_history(self):
        self.history_model.refresh('fx_history')
        self.address_list.refresh_all()

    def on_fx_quotes(self):
        self.update_status()
        # Refresh edits with the new rate
        edit = self.send_tab.fiat_send_e if self.send_tab.fiat_send_e.is_last_edited else self.send_tab.amount_e
        edit.textEdited.emit(edit.text())
        edit = self.receive_tab.fiat_receive_e if self.receive_tab.fiat_receive_e.is_last_edited else self.receive_tab.receive_amount_e
        edit.textEdited.emit(edit.text())
        # History tab needs updating if it used spot
        if self.fx.history_used_spot:
            self.history_model.refresh('fx_quotes')
        self.address_list.refresh_all()

    def toggle_tab(self, tab):
        show = not self.config.get('show_{}_tab'.format(tab.tab_name), False)
        self.config.set_key('show_{}_tab'.format(tab.tab_name), show)
        item_text = (_("Hide {}") if show else _("Show {}")).format(tab.tab_description)
        tab.menu_action.setText(item_text)
        if show:
            # Find out where to place the tab
            index = len(self.tabs)
            for i in range(len(self.tabs)):
                try:
                    if tab.tab_pos < self.tabs.widget(i).tab_pos:
                        index = i
                        break
                except AttributeError:
                    pass
            self.tabs.insertTab(index, tab, tab.tab_icon, tab.tab_description.replace("&", ""))
        else:
            i = self.tabs.indexOf(tab)
            self.tabs.removeTab(i)

    def push_top_level_window(self, window):
        '''Used for e.g. tx dialog box to ensure new dialogs are appropriately
        parented.  This used to be done by explicitly providing the parent
        window, but that isn't something hardware wallet prompts know.'''
        self.tl_windows.append(window)

    def pop_top_level_window(self, window):
        self.tl_windows.remove(window)

    def top_level_window(self, test_func=None):
        '''Do the right thing in the presence of tx dialog windows'''
        override = self.tl_windows[-1] if self.tl_windows else None
        if override and test_func and not test_func(override):
            override = None  # only override if ok for test_func
        return self.top_level_window_recurse(override, test_func)

    def diagnostic_name(self):
        # return '{}:{}'.format(self.__class__.__name__, self.wallet.diagnostic_name())
        return self.wallet.diagnostic_name()

    def is_hidden(self):
        return self.isMinimized() or self.isHidden()

    def show_or_hide(self):
        if self.is_hidden():
            self.bring_to_top()
        else:
            self.hide()

    def bring_to_top(self):
        self.show()
        self.raise_()

    def on_error(self, exc_info):
        e = exc_info[1]
        if isinstance(e, (UserCancelled, concurrent.futures.CancelledError)):
            pass
        elif isinstance(e, UserFacingException):
            self.show_error(str(e))
        else:
            # TODO would be nice if we just sent these to the crash reporter...
            #      anything we don't want to send there, we should explicitly catch
            # send_exception_to_crash_reporter(e)
            try:
                self.logger.error("on_error", exc_info=exc_info)
            except OSError:
                pass  # see #4418
            self.show_error(repr(e))

    @event_listener
    def on_event_wallet_updated(self, wallet):
        if wallet == self.wallet:
            self.need_update.set()

    @event_listener
    def on_event_new_transaction(self, wallet, tx):
        if wallet == self.wallet:
            self.tx_notification_queue.put(tx)

    @qt_event_listener
    def on_event_status(self):
        self.update_status()

    @qt_event_listener
    def on_event_network_updated(self, *args):
        self.update_status()

    @qt_event_listener
    def on_event_blockchain_updated(self, *args):
        # update the number of confirmations in history
        self.refresh_tabs()

    @qt_event_listener
    def on_event_on_quotes(self, *args):
        self.on_fx_quotes()

    @qt_event_listener
    def on_event_on_history(self, *args):
        self.on_fx_history()

    @qt_event_listener
    def on_event_gossip_db_loaded(self, *args):
        pass
        # self.channels_list.gossip_db_loaded.emit(*args)

    @qt_event_listener
    def on_event_channels_updated(self, *args):
        wallet = args[0]
        if wallet == self.wallet:
            pass
            #self.channels_list.update_rows.emit(*args)

    @qt_event_listener
    def on_event_channel(self, *args):
        wallet = args[0]
        if wallet == self.wallet:
            # self.channels_list.update_single_row.emit(*args)
            self.update_status()

    @qt_event_listener
    def on_event_banner(self, *args):
        self.console.showMessage(args[0])

    @qt_event_listener
    def on_event_verified(self, *args):
        wallet, tx_hash, tx_mined_status = args
        if wallet == self.wallet:
            self.history_model.update_tx_mined_status(tx_hash, tx_mined_status)

    @qt_event_listener
    def on_event_fee_histogram(self, *args):
        self.history_model.on_fee_histogram()

    @qt_event_listener
    def on_event_ln_gossip_sync_progress(self, *args):
        self.update_lightning_icon()

    @qt_event_listener
    def on_event_cert_mismatch(self, *args):
        self.show_cert_mismatch_error()

    def close_wallet(self):
        if self.wallet:
            self.logger.info(f'close_wallet {self.wallet.storage.path}')
        run_hook('close_wallet', self.wallet)

    @profiler
    def load_wallet(self, wallet: Abstract_Wallet):
        self.update_recently_visited(wallet.storage.path)
        if wallet.has_lightning():
            util.trigger_callback('channels_updated', wallet)
        self.need_update.set()
        # Once GUI has been initialized check if we want to announce something since the callback has been called before the GUI was initialized
        # update menus
        self.seed_menu.setEnabled(self.wallet.has_seed())
        self.update_lock_icon()
        self.update_buttons_on_seed()
        self.update_console()
        self.receive_tab.do_clear()
        self.receive_tab.request_list.update()
        #self.channels_list.update()
        self.tabs.show()
        self.init_geometry()
        if self.config.get('hide_gui') and self.gui_object.tray.isVisible():
            self.hide()
        else:
            self.show()
        self.watching_only_changed()
        run_hook('load_wallet', wallet, self)
        try:
            wallet.try_detecting_internal_addresses_corruption()
        except InternalAddressCorruption as e:
            self.show_error(str(e))
            send_exception_to_crash_reporter(e)

    def init_geometry(self):
        winpos = self.wallet.db.get("winpos-qt")
        try:
            screen = self.app.desktop().screenGeometry()
            assert screen.contains(QRect(*winpos))
            if not self.isMaximized():
                self.setGeometry(*winpos)
        except:
            self.logger.info("using default geometry")
            if not self.isMaximized():
                self.setGeometry(100, 100, 950, 550)
        self.setMinimumSize(950, 550)

    @classmethod
    def get_app_name_and_version_str(cls) -> str:
        name = "Electrum Evrmore"
        if constants.net.TESTNET:
            name += " " + constants.net.NET_NAME.capitalize()
        return f"{name} {ELECTRUM_VERSION}"

    def watching_only_changed(self):
        name_and_version = self.get_app_name_and_version_str()
        title = f"{name_and_version}  -  {self.wallet.basename()}"
        extra = [self.wallet.db.get('wallet_type', '?')]
        if self.wallet.is_watching_only():
            extra.append(_('watching only'))
        title += '  [%s]' % ', '.join(extra)
        self.setWindowTitle(title)
        self.password_menu.setEnabled(self.wallet.may_have_password())
        self.import_privkey_menu.setVisible(self.wallet.can_import_privkey())
        self.import_address_menu.setVisible(self.wallet.can_import_address())
        self.export_menu.setEnabled(self.wallet.can_export())

    def warn_if_watching_only(self):
        if self.wallet.is_watching_only():
            msg = ' '.join([
                _("This wallet is watching-only."),
                _("This means you will not be able to spend EVR with it."),
                _("Make sure you own the seed phrase or the private keys, before you request EVR to be sent to this wallet.")
            ])
            self.show_warning(msg, title=_('Watch-only wallet'))

    def warn_if_hardware(self):
        if not self.wallet.keystore or self.wallet.keystore.get_type_text()[:2] != 'hw':
            return
        if self.config.get('dont_show_hardware_warning', False):
            return
        msg = ''.join([
            _("This is a hardware wallet."), '\n',
            _("Mining to this wallet may cause you problems. If mining, ensure you make your mining payouts sporadic"), '\n',
            _("or mine to an electrum software wallet and transfer to hardware."), '\n',
            _("Additionally, asset operations are currently unavaliable for hardware. Any assets you send to hardware"), '\n',
            _("will be stuck until asset operations are built into the hardware.")
        ])
        cb = QCheckBox(_("Don't show this again."))
        cb_checked = False

        def on_cb(x):
            nonlocal cb_checked
            cb_checked = x == Qt.Checked

        cb.stateChanged.connect(on_cb)
        self.show_warning(msg, title=_('Hardware Wallet'), checkbox=cb)
        if cb_checked:
            self.config.set_key('dont_show_hardware_warning', True)

    def warn_if_testnet(self):
        if not constants.net.TESTNET:
            return
        # user might have opted out already
        if self.config.get('dont_show_testnet_warning', False):
            return
        # only show once per process lifecycle
        if getattr(self.gui_object, '_warned_testnet', False):
            return
        self.gui_object._warned_testnet = True
        msg = ''.join([
            _("You are in testnet mode."), ' ',
            _("Testnet coins are worthless."), '\n',
            _("Testnet is separate from the main Evrmore network. It is used for testing.")
        ])
        cb = QCheckBox(_("Don't show this again."))
        cb_checked = False

        def on_cb(x):
            nonlocal cb_checked
            cb_checked = x == Qt.Checked

        cb.stateChanged.connect(on_cb)
        self.show_warning(msg, title=_('Testnet'), checkbox=cb)
        if cb_checked:
            self.config.set_key('dont_show_testnet_warning', True)

    def open_wallet(self):
        try:
            wallet_folder = self.get_wallet_folder()
        except FileNotFoundError as e:
            self.show_error(str(e))
            return
        filename, __ = QFileDialog.getOpenFileName(self, "Select your wallet file", wallet_folder)
        if not filename:
            return
        self.gui_object.new_window(filename)

    def select_backup_dir(self, b):
        name = self.config.get('backup_dir', '')
        dirname = QFileDialog.getExistingDirectory(self, "Select your wallet backup directory", name)
        if dirname:
            self.config.set_key('backup_dir', dirname)
            self.backup_dir_e.setText(dirname)

    def backup_wallet(self):
        d = WindowModalDialog(self, _("File Backup"))
        vbox = QVBoxLayout(d)
        grid = QGridLayout()
        backup_help = ""
        backup_dir = self.config.get('backup_dir')
        backup_dir_label = HelpLabel(_('Backup directory') + ':', backup_help)
        msg = _('Please select a backup directory')
        if self.wallet.has_lightning() and self.wallet.lnworker.channels:
            msg += '\n\n' + ' '.join([
                _("Note that lightning channels will be converted to channel backups."),
                _("You cannot use channel backups to perform lightning payments."),
                _("Channel backups can only be used to request your channels to be closed.")
            ])
        self.backup_dir_e = QPushButton(backup_dir)
        self.backup_dir_e.clicked.connect(self.select_backup_dir)
        grid.addWidget(backup_dir_label, 1, 0)
        grid.addWidget(self.backup_dir_e, 1, 1)
        vbox.addLayout(grid)
        vbox.addWidget(WWLabel(msg))
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if not d.exec_():
            return False
        backup_dir = self.config.get_backup_dir()
        if backup_dir is None:
            self.show_message(_("You need to configure a backup directory in your preferences"),
                              title=_("Backup not configured"))
            return
        try:
            new_path = self.wallet.save_backup(backup_dir)
        except BaseException as reason:
            self.show_critical(
                _("Electrum was unable to copy your wallet file to the specified location.") + "\n" + str(reason),
                title=_("Unable to create backup"))
            return
        msg = _("A copy of your wallet file was created in") + " '%s'" % str(new_path)
        self.show_message(msg, title=_("Wallet backup created"))
        return True

    def update_recently_visited(self, filename):
        recent = self.config.get('recently_open', [])
        try:
            sorted(recent)
        except:
            recent = []
        if filename in recent:
            recent.remove(filename)
        recent.insert(0, filename)
        recent = [path for path in recent if os.path.exists(path)]
        recent = recent[:5]
        self.config.set_key('recently_open', recent)
        self.recently_visited_menu.clear()
        for i, k in enumerate(sorted(recent)):
            b = os.path.basename(k)

            def loader(k):
                return lambda: self.gui_object.new_window(k)
            self.recently_visited_menu.addAction(b, loader(k)).setShortcut(QKeySequence("Ctrl+%d"%(i+1)))
        self.recently_visited_menu.setEnabled(bool(len(recent)))

    def get_wallet_folder(self):
        return os.path.dirname(os.path.abspath(self.wallet.storage.path))

    def new_wallet(self):
        try:
            wallet_folder = self.get_wallet_folder()
        except FileNotFoundError as e:
            self.show_error(str(e))
            return
        try:
            filename = get_new_wallet_name(wallet_folder)
        except OSError as e:
            self.logger.exception("")
            self.show_error(repr(e))
            path = self.config.get_fallback_wallet_path()
        else:
            path = os.path.join(wallet_folder, filename)
        self.gui_object.start_new_window(path, uri=None, force_wizard=True)

    def init_menubar(self):
        menubar = QMenuBar()

        file_menu = menubar.addMenu(_("&File"))
        self.recently_visited_menu = file_menu.addMenu(_("&Recently open"))
        file_menu.addAction(_("&Open"), self.open_wallet).setShortcut(QKeySequence.Open)
        file_menu.addAction(_("&New/Restore"), self.new_wallet).setShortcut(QKeySequence.New)
        file_menu.addAction(_("&Save backup"), self.backup_wallet).setShortcut(QKeySequence.SaveAs)
        file_menu.addAction(_("Delete"), self.remove_wallet)
        file_menu.addSeparator()
        file_menu.addAction(_("&Quit"), self.close)

        wallet_menu = menubar.addMenu(_("&Wallet"))
        wallet_menu.addAction(_("&Information"), self.show_wallet_info)
        wallet_menu.addSeparator()
        self.password_menu = wallet_menu.addAction(_("&Password"), self.change_password_dialog)
        self.seed_menu = wallet_menu.addAction(_("&Seed"), self.show_seed_dialog)
        self.private_keys_menu = wallet_menu.addMenu(_("&Private keys"))
        self.private_keys_menu.addAction(_("&Sweep"), self.sweep_key_dialog)
        self.import_privkey_menu = self.private_keys_menu.addAction(_("&Import"), self.do_import_privkey)
        self.export_menu = self.private_keys_menu.addAction(_("&Export"), self.export_privkeys_dialog)
        self.import_address_menu = wallet_menu.addAction(_("Import addresses"), self.import_addresses)
        wallet_menu.addSeparator()

        addresses_menu = wallet_menu.addMenu(_("&Addresses"))
        addresses_menu.addAction(_("&Filter"), lambda: self.address_list.toggle_toolbar(self.config))
        labels_menu = wallet_menu.addMenu(_("&Labels"))
        labels_menu.addAction(_("&Import"), self.do_import_labels)
        labels_menu.addAction(_("&Export"), self.do_export_labels)
        history_menu = wallet_menu.addMenu(_("&History"))
        history_menu.addAction(_("&Filter"), lambda: self.history_list.toggle_toolbar(self.config))
        history_menu.addAction(_("&Summary"), self.history_list.show_summary)
        #history_menu.addAction(_("&Plot"), self.history_list.plot_history_dialog)
        history_menu.addAction(_("&Export"), self.history_list.export_history_dialog)
        contacts_menu = wallet_menu.addMenu(_("Contacts"))
        contacts_menu.addAction(_("&New"), self.new_contact_dialog)
        contacts_menu.addAction(_("Import"), lambda: self.import_contacts())
        contacts_menu.addAction(_("Export"), lambda: self.export_contacts())
        invoices_menu = wallet_menu.addMenu(_("Invoices"))
        invoices_menu.addAction(_("Import"), lambda: self.import_invoices())
        invoices_menu.addAction(_("Export"), lambda: self.export_invoices())
        requests_menu = wallet_menu.addMenu(_("Requests"))
        requests_menu.addAction(_("Import"), lambda: self.import_requests())
        requests_menu.addAction(_("Export"), lambda: self.export_requests())

        wallet_menu.addSeparator()
        wallet_menu.addAction(_("Find"), self.toggle_search).setShortcut(QKeySequence("Ctrl+F"))

        def add_toggle_action(view_menu, tab, default=False):
            is_shown = self.config.get('show_{}_tab'.format(tab.tab_name), default)
            item_name = (_("Hide") if is_shown else _("Show")) + " " + tab.tab_description
            tab.menu_action = view_menu.addAction(item_name, lambda: self.toggle_tab(tab))

        view_menu = menubar.addMenu(_("&View"))
        # add_toggle_action(view_menu, self.messages_tab)
        add_toggle_action(view_menu, self.addresses_tab)
        add_toggle_action(view_menu, self.utxo_tab)
        # add_toggle_action(view_menu, self.channels_tab)
        add_toggle_action(view_menu, self.contacts_tab)
        add_toggle_action(view_menu, self.console_tab)

        tools_menu = menubar.addMenu(_("&Tools"))  # type: QMenu
        preferences_action = tools_menu.addAction(_("Preferences"), self.settings_dialog)  # type: QAction
        if sys.platform == 'darwin':
            # "Settings"/"Preferences" are all reserved keywords in macOS.
            # preferences_action will get picked up based on name (and put into a standardized location,
            # and given a standard reserved hotkey)
            # Hence, this menu item will be at a "uniform location re macOS processes"
            preferences_action.setMenuRole(QAction.PreferencesRole)  # make sure OS recognizes it as preferences
            # Add another preferences item, to also have a "uniform location for Electrum between different OSes"
            tools_menu.addAction(_("Electrum preferences"), self.settings_dialog)

        tools_menu.addAction(_("&Network"), self.gui_object.show_network_dialog).setEnabled(bool(self.network))
        if self.network and self.network.local_watchtower:
            tools_menu.addAction(_("Local &Watchtower"), self.gui_object.show_watchtower_dialog)
        tools_menu.addAction(_("&Plugins"), self.plugins_dialog)
        # Cannot be closed on mac; disabled for now
        # tools_menu.addAction(_("&Log viewer"), self.logview_dialog)
        tools_menu.addSeparator()
        tools_menu.addAction(_("&Sign/verify message"), self.sign_verify_message)
        tools_menu.addAction(_("&Encrypt/decrypt message"), self.encrypt_message)
        tools_menu.addSeparator()

        paytomany_menu = tools_menu.addAction(_("&Pay to many"), self.send_tab.paytomany)
        tools_menu.addAction(_("&Show QR code in separate window"), self.toggle_qr_window)

        raw_transaction_menu = tools_menu.addMenu(_("&Load transaction"))
        raw_transaction_menu.addAction(_("&From file"), self.do_process_from_file)
        raw_transaction_menu.addAction(_("&From text"), self.do_process_from_text)
        raw_transaction_menu.addAction(_("&From the blockchain"), self.do_process_from_txid)
        raw_transaction_menu.addAction(_("&From QR code"), self.read_tx_from_qrcode)
        self.raw_transaction_menu = raw_transaction_menu
        run_hook('init_menubar_tools', self, tools_menu)

        help_menu = menubar.addMenu(_("&Help"))
        help_menu.addAction(_("&About"), self.show_about)
        help_menu.addAction(_("&Check for updates"), self.show_update_check)
        # help_menu.addAction("&RVN Electrum Wiki", lambda: webopen("https://raven.wiki/wiki/Electrum"))
        help_menu.addAction("&Evrmorecoin.org", lambda: webopen("https://evrmorecoin.org"))
        help_menu.addSeparator()
        help_menu.addAction(_("&Documentation"), lambda: webopen("http://docs.electrum.org/")).setShortcut(
            QKeySequence.HelpContents)
        # if not constants.net.TESTNET:
        #    help_menu.addAction(_("&Bitcoin Paper"), self.show_bitcoin_paper)
        help_menu.addAction(_("&Report Bug"), self.show_report_bug)
        help_menu.addSeparator()
        help_menu.addAction(_("&Donate to server"), self.donate_to_server)

        self.setMenuBar(menubar)

    def donate_to_server(self):
        d = self.network.get_donation_address()
        if d:
            host = self.network.get_parameters().server.host
            self.handle_payment_identifier('evrmore:%s?message=donation for %s' % (d, host))
        else:
            self.show_error(_('No donation address for this server'))

    def show_about(self):
        QMessageBox.about(self, "Electrum",
                          (_("Version") + " %s" % ELECTRUM_VERSION + "\n\n" +
                           _("Electrum's focus is speed, with low resource usage and simplifying Evrmore.") + " " +
                           _("You do not need to perform regular backups, because your wallet can be "
                             "recovered from a secret phrase that you can memorize or write on paper.") + " " +
                           _("Startup times are instant because it operates in conjunction with high-performance "
                             "servers that handle the most complicated parts of the Evrmore system.") + "\n\n" +
                           _("Uses icons from the Icons8 icon pack (icons8.com).")))

    def show_bitcoin_paper(self):
        filename = os.path.join(self.config.path, 'bitcoin.pdf')
        if not os.path.exists(filename):
            s = self._fetch_tx_from_network("54e48e5f5c656b26c3bca14a8c95aa583d07ebe84dde3b7dd4a78f4e4186e713")
            if not s:
                return
            s = s.split("0100000000000000")[1:-1]
            out = ''.join(x[6:136] + x[138:268] + x[270:400] if len(x) > 136 else x[6:] for x in s)[16:-20]
            with open(filename, 'wb') as f:
                f.write(bytes.fromhex(out))
        webopen('file:///' + filename)

    def show_update_check(self, version=None):
        self.gui_object._update_check = UpdateCheck(latest_version=version)

    def show_report_bug(self):
        msg = ' '.join([
            _("Please report any bugs as issues on github:<br/>"),
            f'''<a href="{constants.GIT_REPO_ISSUES_URL}">{constants.GIT_REPO_ISSUES_URL}</a><br/><br/>''',
            _("Before reporting a bug, upgrade to the most recent version of Electrum (latest release or git HEAD), and include the version number in your report."),
            _("Try to explain not only what the bug is, but how it occurs.")
        ])
        self.show_message(msg, title="Electrum - " + _("Reporting Bugs"), rich_text=True)

    def notify_transactions(self):
        if self.tx_notification_queue.qsize() == 0:
            return
        if not self.wallet.is_up_to_date():
            return  # no notifications while syncing
        now = time.time()
        rate_limit = 20  # seconds
        if self.tx_notification_last_time + rate_limit > now:
            return
        self.tx_notification_last_time = now
        self.logger.info("Notifying GUI about new transactions")
        txns = []
        while True:
            try:
                txns.append(self.tx_notification_queue.get_nowait())
            except queue.Empty:
                break
        # Combine the transactions if there are at least three
        if len(txns) >= 3:
            total_amount = EvrmoreValue()
            for tx in txns:
                tx_wallet_delta = self.wallet.get_wallet_delta(tx)
                if not tx_wallet_delta.is_relevant:
                    continue
                total_amount += tx_wallet_delta.delta
            recv = ''
            evr = total_amount.evr_value
            assets = total_amount.assets
            recv += self.format_amount_and_units(evr)
            if assets:
                recv += ', '
                assets = ['{}: {}'.format(asset, self.config.format_amount(val)) for asset, val in assets.items()]
                recv += ', '.join(assets)
            self.notify(_("{} new transactions: Total amount received in the new transactions {}")
                        .format(len(txns), recv))
        else:
            for tx in txns:
                tx_wallet_delta = self.wallet.get_wallet_delta(tx)
                if not tx_wallet_delta.is_relevant:
                    continue
                recv = ''
                evr = tx_wallet_delta.delta.evr_value
                assets = tx_wallet_delta.delta.assets
                recv += self.format_amount_and_units(evr)
                if assets:
                    recv += ', '
                    assets = ['{}: {}'.format(asset, self.config.format_amount(val)) for asset, val in assets.items()]
                    recv += ', '.join(assets)
                self.notify(_("New transaction: {}").format(recv))

    def notify(self, message):
        if self.tray:
            try:
                # this requires Qt 5.9
                self.tray.showMessage("Electrum", message, read_QIcon("electrum_dark_icon"), 20000)
            except TypeError:
                self.tray.showMessage("Electrum", message, QSystemTrayIcon.Information, 20000)

    def timer_actions(self):
        # refresh invoices and requests because they show ETA
        self.receive_tab.request_list.refresh_all()
        self.send_tab.invoice_list.refresh_all()
        # Note this runs in the GUI thread
        if self.need_update.is_set():
            self.need_update.clear()
            self.update_wallet()
        elif not self.wallet.is_up_to_date():
            # this updates "synchronizing" progress
            self.update_status()

        # resolve aliases
        # FIXME this might do blocking network calls that has a timeout of several seconds
        self.send_tab.payto_e.on_timer_check_text()
        self.notify_transactions()

    def format_amount(self, amount_sat: int, is_diff=False, whitespaces=False) -> str:
        """Formats amount as string, converting to desired unit.
        E.g. 500_000 -> '0.005'
        """
        return self.config.format_amount(amount_sat, is_diff=is_diff, whitespaces=whitespaces)

    def format_amount_and_units(self, amount_sat, *, timestamp: int = None) -> str:
        """Returns string with both bitcoin and fiat amounts, in desired units.
        E.g. 500_000 -> '0.005 BTC (191.42 EUR)'
        """

        suffix = ''
        if isinstance(amount_sat, EvrmoreValue):
            if amount_sat.assets:
                suffix = f' ({len(amount_sat.assets)} asset' + ('s' if len(amount_sat.assets) > 1 else '') + ')'
            amount_sat = amount_sat.evr_value.value
        text = self.config.format_amount_and_units(amount_sat)
        fiat = self.fx.format_amount_and_units(amount_sat, timestamp=timestamp) if self.fx else None
        if text and fiat:
            text += f' ({fiat})'
        if suffix:
            text += suffix
        return text

    def format_fiat_and_units(self, amount_sat) -> str:
        """Returns string of FX fiat amount, in desired units.
        E.g. 500_000 -> '191.42 EUR'
        """
        return self.fx.format_amount_and_units(amount_sat) if self.fx else ''

    def format_fee_rate(self, fee_rate):
        return self.config.format_fee_rate(fee_rate)

    def get_decimal_point(self):
        return self.config.get_decimal_point()

    def base_unit(self):
        return self.config.get_base_unit()

    def connect_fields(self, btc_e, fiat_e):

        def edit_changed(edit):
            if edit.follows:
                return
            edit.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
            fiat_e.is_last_edited = (edit == fiat_e)
            amount = edit.get_amount()
            rate = self.fx.exchange_rate() if self.fx else Decimal('NaN')
            if rate.is_nan() or amount is None:
                if edit is fiat_e:
                    btc_e.setText("")
                else:
                    fiat_e.setText("")
            else:
                if edit is fiat_e:
                    btc_e.follows = True
                    btc_e.setAmount(int(amount / Decimal(rate) * COIN))
                    btc_e.setStyleSheet(ColorScheme.BLUE.as_stylesheet())
                    btc_e.follows = False
                else:
                    fiat_e.follows = True
                    fiat_e.setText(self.fx.ccy_amount_str(
                        amount * Decimal(rate) / COIN, False))
                    fiat_e.setStyleSheet(ColorScheme.BLUE.as_stylesheet())
                    fiat_e.follows = False

        btc_e.follows = False
        fiat_e.follows = False
        fiat_e.textChanged.connect(partial(edit_changed, fiat_e))
        btc_e.textChanged.connect(partial(edit_changed, btc_e))
        fiat_e.is_last_edited = False

    def update_status(self):
        if not self.wallet:
            return

        network_text = ""
        balance_text = ""

        status_text = ''
        local_height = self.network.get_local_height()
        server_height = self.network.get_server_height()
        if self.network is None:
            network_text = _("Offline")
            icon = read_QIcon("status_disconnected.png")

        elif self.network.is_connected():
            server_lag = local_height - server_height
            fork_str = "_fork" if len(self.network.get_blockchains()) > 1 else ""
            # Server height can be 0 after switching to a new server
            # until we get a headers subscription request response.
            # Display the synchronizing message in that case.

            if not self.wallet.is_up_to_date() or server_height == 0:
                num_sent, num_answered = self.wallet.adb.get_history_sync_state_details()
                network_text = ("{} ({}/{})"
                                .format(_("Synchronizing..."), num_answered, num_sent))
                icon = read_QIcon("status_waiting.png")

            elif server_lag > 1:
                network_text = _("Server is lagging ({} blocks)").format(server_lag)
                icon = read_QIcon("status_lagging%s.png" % fork_str)
            else:
                network_text = _("Connected")
                confirmed, unconfirmed, unmatured, frozen, lightning, f_lightning = self.wallet.get_balances_for_piechart()
                self.balance_label.update_list([
                    (_('Frozen'), COLOR_FROZEN, frozen),
                    (_('Unmatured'), COLOR_UNMATURED, unmatured),
                    (_('Unconfirmed'), COLOR_UNCONFIRMED, unconfirmed),
                    (_('On-chain'), COLOR_CONFIRMED, confirmed),
                    #(_('Lightning'), COLOR_LIGHTNING, lightning),
                    #(_('Lightning frozen'), COLOR_FROZEN_LIGHTNING, f_lightning),
                ])
                balance = confirmed + unconfirmed + unmatured + frozen + lightning + f_lightning
                balance_text =  _("Balance") + ": %s "%(self.format_amount_and_units(balance))
                # append fiat balance and price
                if self.fx.is_enabled():
                    balance_text += self.fx.get_fiat_status_text(balance,
                        self.base_unit(), self.get_decimal_point()) or ''
                if not self.network.proxy:
                    icon = read_QIcon("status_connected%s.png" % fork_str)
                else:
                    icon = read_QIcon("status_connected_proxy%s.png" % fork_str)
        else:
            if self.network.proxy:
                network_text = "{} ({})".format(_("Not connected"), _("proxy enabled"))
            else:
                network_text = _("Not connected")
            icon = read_QIcon("status_disconnected.png")

        if self.tray:
            # note: don't include balance in systray tooltip, as some OSes persist tooltips,
            #       hence "leaking" the wallet balance (see #5665)
            name_and_version = self.get_app_name_and_version_str()
            self.tray.setToolTip(f"{name_and_version} ({network_text})")
        self.balance_label.setText(balance_text or network_text)
        if self.status_button:
            self.status_button.setIcon(icon)

        num_tasks = self.num_tasks()
        if num_tasks == 0:
            name = ''
        elif num_tasks == 1:
            name = list(self._coroutines_scheduled.values())[0]  + '...'
        else:
            name = "%d"%num_tasks + _('tasks')  + '...'
        self.tasks_label.setText(name)
        self.tasks_label.setVisible(num_tasks > 0)

    def num_tasks(self):
        # For the moment, all the coroutines in this set are outgoing LN payments,
        # so we can use this to disable buttons for rebalance/swap suggestions
        return len(self._coroutines_scheduled)

    def update_wallet(self):
        self.update_status()
        if self.wallet.is_up_to_date() or not self.network or not self.network.is_connected():
            self.update_tabs()

    def update_tabs(self, wallet=None):
        if wallet is None:
            wallet = self.wallet
        if wallet != self.wallet:
            return
        self.history_model.refresh('update_tabs')
        self.receive_tab.request_list.update()
        self.receive_tab.update_current_request()
        self.send_tab.invoice_list.update()
        self.address_list.update()
        self.asset_view.update()
        self.utxo_list.update()
        self.contact_list.update()
        #self.channels_list.update_rows.emit(wallet)
        self.update_completions()
        self.update_sendable_and_send_tab()
        if self.wallet.wallet_type not in ('imported, xpub', 'hw'):
            self.create_workspace.refresh_owners()
            self.reissue_workspace.refresh_owners()

    def refresh_tabs(self, wallet=None):
        self.history_model.refresh('refresh_tabs')
        self.receive_tab.request_list.refresh_all()
        self.send_tab.invoice_list.refresh_all()
        self.address_list.refresh_all()
        self.utxo_list.refresh_all()
        self.contact_list.refresh_all()
        #self.channels_list.update_rows.emit(self.wallet)

    #def create_channels_tab(self):
    #    self.channels_list = ChannelsList(self)
    #    t = self.channels_list.get_toolbar()
    #    return self.create_list_tab(self.channels_list, t)

    def create_history_tab(self):
        self.history_model = HistoryModel(self)
        self.history_list = l = HistoryList(self, self.history_model)
        self.history_model.set_view(self.history_list)
        l.searchable_list = l
        toolbar = l.create_toolbar(self.config)
        tab = self.create_list_tab(l, toolbar)
        toolbar_shown = bool(self.config.get('show_toolbar_history', False))
        l.show_toolbar(toolbar_shown)
        return tab

    def show_address(self, addr):
        from . import address_dialog
        d = address_dialog.AddressDialog(self, addr)
        d.exec_()

    def show_asset(self, asset):
        from . import asset_dialog
        d = asset_dialog.AssetDialog(self, asset)
        d.exec_()

    def hide_asset(self, asset):
        self.asset_blacklist.append('^' + asset + '$')
        self.config.set_key('asset_blacklist', self.asset_blacklist, True)
        self.asset_view.update()
        self.history_model.refresh('Marked asset as spam')
        self.history_list.hm.refresh('modified asset blacklist', force=True)

    def show_channel_details(self, chan):
        from .channel_details import ChannelDetailsDialog
        ChannelDetailsDialog(self, chan).show()

    def show_transaction(self, tx, *, tx_desc=None):
        '''tx_desc is set only for txs created in the Send tab'''
        show_transaction(tx, parent=self, desc=tx_desc)

    def show_lightning_transaction(self, tx_item):
        from .lightning_tx_dialog import LightningTxDialog
        d = LightningTxDialog(self, tx_item)
        d.show()

    def create_receive_tab(self):
        from .receive_tab import ReceiveTab
        return ReceiveTab(self)

    def do_copy(self, content: str, *, title: str = None) -> None:
        self.app.clipboard().setText(content)
        if title is None:
            tooltip_text = _("Text copied to clipboard").format(title)
        else:
            tooltip_text = _("{} copied to clipboard").format(title)
        QToolTip.showText(QCursor.pos(), tooltip_text, self)

    def toggle_qr_window(self):
        from . import qrwindow
        if not self.qr_window:
            self.qr_window = qrwindow.QR_Window(self)
            self.qr_window.setVisible(True)
            self.qr_window_geometry = self.qr_window.geometry()
        else:
            if not self.qr_window.isVisible():
                self.qr_window.setVisible(True)
                self.qr_window.setGeometry(self.qr_window_geometry)
            else:
                self.qr_window_geometry = self.qr_window.geometry()
                self.qr_window.setVisible(False)

    def show_send_tab(self):
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.send_tab))

    def show_receive_tab(self):
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.receive_tab))

    '''
    def create_swap_tab(self):
        self.current_swap_coro = None
        self.current_swap_psbt = None

        self.current_swap_in = None
        self.current_swap_out = None

        # Redeem transaction

        w1 = QWidget()
        vbox = QVBoxLayout(w1)

        self.swap_info = info = QLabel()

        async def query_and_parse_tx(tx: PartialTransaction):
            input_values = []
            for input in tx.inputs():
                v = input.value_sats()
                if v:
                    input_values.append(v)
                else:
                    try:
                        old_tx_raw = await self.network.interface.get_transaction(input.prevout.txid.hex(), timeout=2)
                        # We do not need to verify because if this is invalid, it won't be accepted on the chain
                        old_tx = Transaction(old_tx_raw)
                        outpoint = old_tx.outputs()[input.prevout.out_idx]
                        a = outpoint.asset
                        try:
                            # Best effort check if still valid
                            hashX = address_to_scripthash(outpoint.address)
                            if a:
                                unspent_for_addr = await self.network.interface.listunspentassets_for_scripthash(hashX)
                            else:
                                unspent_for_addr = await self.network.interface.listunspent_for_scripthash(hashX)
                            unspents = set(TxOutpoint.from_str(f'{d["tx_hash"]}:{d["tx_pos"]}') for d in unspent_for_addr)
                            if input.prevout not in unspents:
                                info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                                info.setText(_('This partial transaction has already been redeemed!'))
                                return
                        except Exception:
                            pass
                        input_values.append(EvrmoreValue(0, {a: outpoint.value}) if a else EvrmoreValue(outpoint.value))
                    except Exception as e:
                        self.logger.exception('')
                        input_values.append(None)
            
            if not all(input_values):
                info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                info.setText(_('Unable to query information for what you will receive. Try using another Electrum server.'))
                return

            output_values = []
            for output in tx.outputs():
                a = output.asset
                output_values.append(EvrmoreValue(0, {a: output.value}) if a else EvrmoreValue(output.value))

            if not all(output_values):
                info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                info.setText(_('Unable to query information for what you will receive. Try using another Electrum server.'))
                return
                  
            self.current_swap_in = input_values
            total_in = sum(input_values, EvrmoreValue())
            self.current_swap_out = total_out = sum(output_values, EvrmoreValue())

            info.setText(_(f'You will receive: {total_in}\nYou will spend: {total_out}\nYou will handle the transaction fees.'))

        # Input
        def parse_psbt(w):
            self.current_swap_in = None
            self.current_swap_out = None
            self.current_swap_psbt = None
            if self.current_swap_coro:
                self.current_swap_coro.cancel()
            info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
            raw = w.toPlainText()
            if len(raw) == 0:
                info.setText('')
                return
            try:
                self.current_swap_psbt = psbt = PartialTransaction.from_tx(Transaction(raw, wallet=self.wallet), strip=False)
                if any([i.sighash for i in psbt.inputs() if i.sighash != SIGHASH.SINGLE_ANYONECANPAY]):
                    raise Exception('Not SINGLE_ANYONECANPAY')
                info.setText(_('Waiting for data...'))
                self.run_coroutine_from_thread(query_and_parse_tx(psbt), _('Querying PSBT'), is_swap=True)
            except Exception as e:
                info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                info.setText(_('Invalid Signed Partial'))

        self.swap_input_text = input_text = QTextEdit()
        input_text.setAcceptRichText(False)
        input_label = QLabel(_('Signed Partial:'))
        input_text.textChanged.connect(partial(parse_psbt, input_text))

        input = QWidget()
        input_l = QHBoxLayout(input)

        input_l.addWidget(input_label)
        input_l.addWidget(input_text)

        input.setLayout(input_l)


        # Execute

        def make_payment():
            if not self.current_swap_in or \
                not self.current_swap_out or \
                not self.current_swap_psbt:
                return

            coins = []
            if self.current_swap_out.evr_value != 0:
                coins += self.get_coins()
            for asset in self.current_swap_out.assets.keys():
                coins += self.get_coins(asset=asset)

            inputs = self.current_swap_psbt.inputs()[:]

            for input, amount in zip(inputs, self.current_swap_in):
                input._trusted_value_sats = amount

            outputs = self.current_swap_psbt.outputs()[:]

            addr = self.wallet.get_receiving_address()
            
            total_in = sum(self.current_swap_in, EvrmoreValue())

            if total_in.evr_value != 0:
                outputs.append(PartialTxOutput.from_address_and_value(addr, total_in.evr_value))
            for a, v in total_in.assets.items(): 
                outputs.append(PartialTxOutput.from_address_and_value(addr, v, asset=a))

            self.pay_onchain_dialog(coins, outputs, mandatory_inputs=inputs, freeze_locktime=self.current_swap_psbt.locktime, for_swap=True)

        button = EnterButton(_("Redeem"), make_payment)
        button.setMaximumWidth(100)

        vbox.addWidget(input, 4)
        vbox.addWidget(info, 6)
        vbox.addWidget(button, 1)

        w1.setLayout(vbox)

        # Create swap

        internal_tab = QTabWidget()

        # Buy asset
        w2 = QWidget()
        vbox2 = QVBoxLayout(w2)

        buy_error = QLabel()
        buy_error.setStyleSheet(ColorScheme.RED.as_stylesheet())
        buy_amt = EVRAmountEdit(self.get_decimal_point)

        def on_edit(w):
            buy_error.setText('')    
            raw = w.text()
            if len(raw) >= 3 and not assets.is_name_valid(raw):
                buy_error.setText('Invalid asset name!')

        buy_text = SizedFreezableLineEdit(width=buy_amt._width)
        buy_text.textChanged.connect(partial(on_edit, buy_text))

        buy_label = QLabel(_("Wanted Asset:"))
        vbox2.addWidget(buy_label, 1)
        vbox2.addWidget(buy_text, 1)
        vbox2.addWidget(buy_error, 1)

        buy_amt_label = QLabel(_("EVR Offered:"))
        vbox2.addWidget(buy_amt_label, 1)
        vbox2.addWidget(buy_amt, 1)

        def generate():
            if not buy_text.text():
                return
            offer = buy_amt.get_amount()
            if offer <= 0:
                return
            coins = self.get_coins()
            for c in coins:
                if c.value_sats().evr_value == offer and self.question(
                    _("You already have an unspent transaction output with this amount. "
                      "Would you like use this UTXO?"),
                    title=_("UTXO")
                ):
                    self.create_and_freeze_swap(c, )
                    return
                    
            addr = self.wallet.get_new_sweep_address_for_channel()
            self.pay_onchain_dialog(coins, [PartialTxOutput.from_address_and_value(addr, offer)])

        button1 = EnterButton(_("Generate Signed Partial"), generate)
        button1.setMaximumWidth(300)
        vbox2.addWidget(button1, 1)
        vbox2.addWidget(QWidget(), 4)

        w2.setLayout(vbox2)

        w3 = QWidget()
        vbox3 = QVBoxLayout(w3)
        l = QTextEdit()

        def test():
            from electrum.evrmore import address_to_script
            from electrum.transaction import Satoshis
            
            i1 = PartialTxInput(prevout=TxOutpoint(bytes.fromhex('7e602949d4c149b433f368046c0c478e2557a20ced1b394bf10045ff358768fb'), 0), is_coinbase_output=False)
            i2 = PartialTxInput(prevout=TxOutpoint(bytes.fromhex('126d25a4c779cc4e92853ea872aa23d20afbc87bb99a8136bc0fc1c472bf9356'), 1), is_coinbase_output=False)
            
            i1.script_type = 'p2pkh'
            i2.script_type = 'p2pkh'

            i1._trusted_value_sats = EvrmoreValue(449500)
            i2._trusted_value_sats = EvrmoreValue(350000)

            self.wallet._add_input_sig_info(i1, 'RV1265roRyHUYuLerLfit8QmyiWDrZtbv2', only_der_suffix=None)
            self.wallet._add_input_sig_info(i2, 'R9TonskbB14efLXRM7ZdcCDxe7nbMM9BxK', only_der_suffix=None)

            ins = [
                i1,
                i2
            ]
            outs = [
                PartialTxOutput(scriptpubkey=bfh(address_to_script('RWxATTuFXo82CwgixG7Q6npT7JBvDM3Jw9')),value=Satoshis(500000)),
                PartialTxOutput(scriptpubkey=bfh(address_to_script('RDcookhkBm7F95dfvd4Pv8EYosNVr3mSDM')),value=Satoshis(299100)),
            ]
            tx = PartialTransaction.from_io(ins, outs, locktime=1901785, version=2, for_swap=True)
            self.wallet.sign_transaction(tx, None)
            l.setText(tx.serialize_to_network())

        button2 = EnterButton("Test", test)

        vbox3.addWidget(l)
        vbox3.addWidget(button2)

        w3.setLayout(vbox3)

        w4 = QWidget()
        vbox4 = QVBoxLayout(w4)
        w4.setLayout(vbox4)

        internal_tab.addTab(w2, _("Buy Asset"))
        internal_tab.addTab(w3, _("Sell Asset"))  
        internal_tab.addTab(w4, _("Trade Assets"))  

        # List swaps
        w5 = QWidget()
        vbox5 = QVBoxLayout(w5)
        w5.setLayout(vbox5)
        
        self.internal_swap_tabs = tabwidget = QTabWidget()
        tabwidget.addTab(w1, _("Redeem Swap"))
        tabwidget.addTab(internal_tab, _("Create Swap"))
        tabwidget.addTab(w5, _("My Swaps"))
        return tabwidget
    '''
    def create_assets_tab(self):

        from .asset_view import AssetView
        self.asset_view = l = AssetView(self)

        add_management_tabs = True
        if self.wallet.wallet_type not in ('xpub',):
            self.create_workspace = create_w = AssetCreateWorkspace(self,
                                                                self.confirm_asset_creation)

            self.reissue_workspace = reissue_w = AssetReissueWorkspace(self,
                                                                   self.confirm_asset_reissue)
        else:
            add_management_tabs = False
            self.create_workspace = create_w = QLabel()
            self.reissue_workspace = reissue_w = QLabel()

        layout = QGridLayout()
        w = QWidget()
        w.setLayout(layout)
        self.asset_tabs = tabwidget = QTabWidget()

        tabwidget.addTab(l, _("My Assets"))
        if add_management_tabs:
            tabwidget.addTab(create_w, _("Create Asset"))
            tabwidget.addTab(reissue_w, _("Reissue Asset"))
        layout.addWidget(tabwidget, 0, 0)
        return w

    def confirm_asset_reissue(self):
        error = self.reissue_workspace.verify_valid()

        if error:
            self.show_warning(_('Invalid asset metadata:\n'
                                '{}').format(error))
            return

        def show_non_reissuable_warning():
            if self.reissue_workspace.should_warn_on_non_reissuable():
                cb = QCheckBox(_("Don't show this message again."))
                cb_checked = False

                def on_cb(x):
                    nonlocal cb_checked
                    cb_checked = x == Qt.Checked

                cb.stateChanged.connect(on_cb)
                goto = self.question(_('You will not be able to change '
                                       'this asset in the future.\n'
                                       'Are you sure you want to continue?'),
                                     title=_('Warning: Non reissuable asset'), checkbox=cb)

                if cb_checked:
                    self.config.set_key('warn_asset_non_reissuable', False)
                if goto:
                    return True
                else:
                    return False
            else:
                return True

        if not show_non_reissuable_warning():
            return

        norm, new = self.reissue_workspace.get_output()

        self.send_tab.pay_onchain_dialog(
            list(set(self.get_coins(asset=self.reissue_workspace.get_owner()))),
            norm,
            coinbase_outputs=new,
        )

        self.reissue_workspace.reset_workspace()

    def confirm_asset_creation(self):

        error = self.create_workspace.verify_valid()

        if error:
            self.show_warning(_('Invalid asset metadata:\n'
                                '{}').format(error))
            return

        def show_non_reissuable_warning():
            if self.create_workspace.should_warn_on_non_reissuable():
                cb = QCheckBox(_("Don't show this message again."))
                cb_checked = False

                def on_cb(x):
                    nonlocal cb_checked
                    cb_checked = x == Qt.Checked

                cb.stateChanged.connect(on_cb)
                goto = self.question(_('You will not be able to change '
                                              'this asset in the future.\n'
                                              'Are you sure you want to continue?'),
                                            title=_('Warning: Non reissuable asset'), checkbox=cb)

                if cb_checked:
                    self.config.set_key('warn_asset_non_reissuable', False)
                if goto:
                    return True
                else:
                    return False
            else:
                return True

        if not show_non_reissuable_warning():
            return

        norm, new = self.create_workspace.get_output()

        self.send_tab.pay_onchain_dialog(
            list(set(self.get_coins(asset=self.create_workspace.get_owner()))),
            norm,
            coinbase_outputs=new,
        )

        self.create_workspace.reset_workspace()
    '''
    def get_asset_from_spend_tab(self) -> Optional[str]:
        combo_index = self.to_send_combo.currentIndex()
        if combo_index != 0:
            return self.send_options[combo_index]
        return None

    def create_and_freeze_swap(self, vin: PartialTxInput, vout: EvrmoreValue):
        self.set_frozen_state_of_coins([vin])
        self.wallet.set_label(vin.prevout.txid.hex(), _("Atomic Swap"))
        

    def spend_max(self):
        if run_hook('abort_send', self):
            return
        asset = self.get_asset_from_spend_tab()
        outputs = self.payto_e.get_outputs(True)
        if not outputs:
            return
        make_tx = lambda fee_est: self.wallet.make_unsigned_transaction(
            coins=self.get_coins(asset=asset),
            outputs=outputs,
            fee=fee_est,
            is_sweep=False)

        try:
            try:
                tx = make_tx(None)
            except (NotEnoughFunds, NoDynamicFeeEstimates) as e:
                # Check if we had enough funds excluding fees,
                # if so, still provide opportunity to set lower fees.
                tx = make_tx(0)
        except NotEnoughFunds as e:
            self.max_button.setChecked(False)
            text = self.get_text_not_enough_funds_mentioning_frozen()
            self.show_error(text)
            return

        self.max_button.setChecked(True)
        amount = tx.output_value()
        __, x_fee_amount = run_hook('get_tx_extra_fee', self.wallet, tx) or (None, 0)
        amount_after_all_fees = amount - EvrmoreValue(x_fee_amount)
        assets = amount_after_all_fees.assets
        if len(assets) == 0:
            to_show = amount_after_all_fees.evr_value.value
        else:
            __, v = list(assets.items())[0]
            to_show = v.value
        self.amount_e.setAmount(to_show)
        # show tooltip explaining max amount
        mining_fee = tx.get_fee()
        mining_fee_str = self.format_amount_and_units(mining_fee.evr_value.value)
        msg = _("Mining fee: {} (can be adjusted on next screen)").format(mining_fee_str)
        if x_fee_amount:
            twofactor_fee_str = self.format_amount_and_units(x_fee_amount)
            msg += "\n" + _("2fa fee: {} (for the next batch of transactions)").format(twofactor_fee_str)
        frozen_bal = self.get_frozen_balance_str()
        if frozen_bal:
            msg += "\n" + _("Some coins are frozen: {} (can be unfrozen in the Addresses or in the Coins tab)").format(
                frozen_bal)
        QToolTip.showText(self.max_button.mapToGlobal(QPoint(0, 0)), msg)
    '''
    def create_send_tab(self):
        from .send_tab import SendTab
        return SendTab(self)

    def update_sendable_and_send_tab(self):
        # Don't interrupt if we don't need to
        coins = self.get_manually_selected_coins() if self.utxo_list else None
        if coins:
            selected_value = sum((x.value_sats() for x in coins), EvrmoreValue())
            list_evr = selected_value.evr_value > 0
            selectable_assets = list(selected_value.assets.keys())
        else:
            list_evr, selectable_assets = self.wallet.get_non_frozen_assets()

        new_send_options = ([util.decimal_point_to_base_unit_name(self.get_decimal_point())] if list_evr else []) + \
                            sorted(selectable_assets)

        if not new_send_options:
            new_send_options = [util.decimal_point_to_base_unit_name(self.get_decimal_point())]

        diff = set(new_send_options) ^ set(self.send_options)
        if self.send_options and not diff:
            return

        current_selection = self.get_asset_from_spend_tab()
        self.send_tab.to_send_combo.clear()
        self.send_options = new_send_options
        self.send_tab.to_send_combo.addItems(self.send_options)
        if current_selection and current_selection not in diff:
            # The set is quick
            # the current selection is from the old set
            # if was in old set & in diff = not in new set
            self.send_tab.to_send_combo.setCurrentIndex(self.send_options.index(current_selection))

    def get_asset_from_spend_tab(self) -> Optional[str]:
        combo_index = self.send_tab.to_send_combo.currentIndex()
        if combo_index > 0:
            return self.send_options[combo_index]
        return None

    def get_contact_payto(self, key):
        _type, label = self.contacts.get(key)
        return label + '  <' + key + '>' if _type == 'address' else key

    def update_completions(self):
        l = [self.get_contact_payto(key) for key in self.contacts.keys()]
        self.completions.setStringList(l)

    @protected
    def protect(self, func, args, password):
        return func(*args, password)

    def run_swap_dialog(self, is_reverse=None, recv_amount_sat=None, channels=None):
        if not self.network:
            self.show_error(_("You are offline."))
            return
        def get_pairs_thread():
            self.network.run_from_another_thread(self.wallet.lnworker.swap_manager.get_pairs())
        BlockingWaitingDialog(self, _('Please wait...'), get_pairs_thread)
        d = SwapDialog(self, is_reverse=is_reverse, recv_amount_sat=recv_amount_sat, channels=channels)
        return d.run()

    @qt_event_listener
    def on_event_request_status(self, wallet, key, status):
        if wallet != self.wallet:
            return
        req = self.wallet.get_request(key)
        if req is None:
            return
        if status == PR_PAID:
            # FIXME notification should only be shown if request was not PAID before
            msg = _('Payment received:')
            amount = req.get_amount_sat()
            if amount:
                msg += ' ' + self.format_amount_and_units(amount)
            msg += '\n' + req.get_message()
            self.notify(msg)
            self.receive_tab.request_list.delete_item(key)
            self.receive_tab.receive_tabs.setVisible(False)
            self.need_update.set()
        else:
            self.receive_tab.request_list.refresh_item(key)

    @qt_event_listener
    def on_event_invoice_status(self, wallet, key):
        if wallet != self.wallet:
            return
        invoice = self.wallet.get_invoice(key)
        if invoice is None:
            return
        status = self.wallet.get_invoice_status(invoice)
        if status == PR_PAID:
            self.send_tab.invoice_list.delete_item(key)
        else:
            self.send_tab.invoice_list.refresh_item(key)

    @qt_event_listener
    def on_event_payment_succeeded(self, wallet, key):
        # sent by lnworker, redundant with invoice_status
        if wallet != self.wallet:
            return
        description = self.wallet.get_label(key)
        self.notify(_('Payment sent') + '\n\n' + description)
        self.need_update.set()

    @qt_event_listener
    def on_event_payment_failed(self, wallet, key, reason):
        if wallet != self.wallet:
            return
        invoice = self.wallet.get_invoice(key)
        if invoice and invoice.is_lightning() and invoice.get_address():
            if self.question(_('Payment failed') + '\n\n' + reason + '\n\n'+ 'Fallback to onchain payment?'):
                self.send_tab.pay_onchain_dialog(self.get_coins(), invoice.get_outputs())
        else:
            self.show_error(_('Payment failed') + '\n\n' + reason)

    def get_coins(self, *, nonlocal_only=False, asset: Optional[str] = None) -> Sequence[PartialTxInput]:
        coins = self.get_manually_selected_coins()
        if coins is not None:
            return coins
        else:
            return self.wallet.get_spendable_coins(None, nonlocal_only=nonlocal_only, asset=asset)

    def get_manually_selected_coins(self) -> Optional[Sequence[PartialTxInput]]:
        """Return a list of selected coins or None.
        Note: None means selection is not being used,
              while an empty sequence means the user specifically selected that.
        """
        return self.utxo_list.get_spend_list()

    def broadcast_or_show(self, tx: Transaction):
        if not tx.is_complete():
            self.show_transaction(tx)
            return
        if not self.network:
            self.show_error(_("You can't broadcast a transaction without a live network connection."))
            self.show_transaction(tx)
            return

        self.broadcast_transaction(tx)

    def broadcast_transaction(self, tx: Transaction):
        self.send_tab.broadcast_transaction(tx)

    @protected
    def sign_tx(self, tx, *, callback, external_keypairs, password, mixed=False):
        self.sign_tx_with_password(tx, callback=callback, password=password, external_keypairs=external_keypairs, mixed=mixed)

    def sign_tx_with_password(self, tx: PartialTransaction, *, callback, password, external_keypairs=None, mixed=False):
        '''Sign the transaction in a separate thread.  When done, calls
        the callback with a success code of True or False.
        '''

        def on_success(result):
            callback(True)

        def on_failure(exc_info):
            self.on_error(exc_info)
            callback(False)

        on_success = run_hook('tc_sign_wrapper', self.wallet, tx, on_success, on_failure) or on_success

        def sign(tx, external_keypairs, password):
            if external_keypairs:
            # can sign directly
                tx.sign(external_keypairs)
            if not external_keypairs or mixed:
                self.wallet.sign_transaction(tx, password)

        task = partial(sign, tx, external_keypairs, password)

        msg = _('Signing transaction...')
        WaitingDialog(self, msg, task, on_success, on_failure)

    def mktx_for_open_channel(self, *, funding_sat, node_id):
        coins = self.get_coins(nonlocal_only=True)
        make_tx = lambda fee_est: self.wallet.lnworker.mktx_for_open_channel(
            coins=coins,
            funding_sat=funding_sat,
            node_id=node_id,
            fee_est=fee_est)
        return make_tx

    def open_channel(self, connect_str, funding_sat, push_amt):
        try:
            node_id, rest = extract_nodeid(connect_str)
        except ConnStringFormatError as e:
            self.show_error(str(e))
            return
        if self.wallet.lnworker.has_conflicting_backup_with(node_id):
            msg = messages.MGS_CONFLICTING_BACKUP_INSTANCE
            if not self.question(msg):
                return
        # use ConfirmTxDialog
        # we need to know the fee before we broadcast, because the txid is required
        make_tx = self.mktx_for_open_channel(funding_sat=funding_sat, node_id=node_id)
        d = ConfirmTxDialog(window=self, make_tx=make_tx, output_value=funding_sat, is_sweep=False)
        # disable preview button because the user must not broadcast tx before establishment_flow
        d.preview_button.setEnabled(False)
        cancelled, is_send, password, funding_tx = d.run()
        if not is_send:
            return
        if cancelled:
            return
        # read funding_sat from tx; converts '!' to int value
        funding_sat = funding_tx.output_value_for_address(ln_dummy_address())

        def task():
            return self.wallet.lnworker.open_channel(
                connect_str=connect_str,
                funding_tx=funding_tx,
                funding_sat=funding_sat,
                push_amt_sat=push_amt,
                password=password)

        def on_failure(exc_info):
            type_, e, traceback = exc_info
            #self.logger.error("Could not open channel", exc_info=exc_info)
            self.show_error(_('Could not open channel: {}').format(repr(e)))

        WaitingDialog(self, _('Opening channel...'), task, self.on_open_channel_success, on_failure)

    def on_open_channel_success(self, args):
        chan, funding_tx = args
        lnworker = self.wallet.lnworker
        if not chan.has_onchain_backup():
            data = lnworker.export_channel_backup(chan.channel_id)
            help_text = _(messages.MSG_CREATED_NON_RECOVERABLE_CHANNEL)
            help_text += '\n\n' + _('Alternatively, you can save a backup of your wallet file from the File menu')
            self.show_qrcode(
                data, _('Save channel backup'),
                help_text=help_text,
                show_copy_text_btn=True)
        n = chan.constraints.funding_txn_minimum_depth
        message = '\n'.join([
            _('Channel established.'),
            _('Remote peer ID') + ':' + chan.node_id.hex(),
            _('This channel will be usable after {} confirmations').format(n)
        ])
        if not funding_tx.is_complete():
            message += '\n\n' + _('Please sign and broadcast the funding transaction')
            self.show_message(message)
            self.show_transaction(funding_tx)
        else:
            self.show_message(message)

    def query_choice(self, msg, choices):
        # Needed by QtHandler for hardware wallets
        dialog = WindowModalDialog(self.top_level_window(), title='Question')
        dialog.setMinimumWidth(400)
        clayout = ChoicesLayout(msg, choices)
        vbox = QVBoxLayout(dialog)
        vbox.addLayout(clayout.layout())
        vbox.addLayout(Buttons(CancelButton(dialog), OkButton(dialog)))
        if not dialog.exec_():
            return None
        return clayout.selected_index()

    def handle_payment_identifier(self, *args, **kwargs):
        self.send_tab.handle_payment_identifier(*args, **kwargs)

    def set_frozen_state_of_addresses(self, addrs, freeze: bool):
        self.wallet.set_frozen_state_of_addresses(addrs, freeze)
        self.address_list.refresh_all()
        self.utxo_list.refresh_all()
        self.address_list.selectionModel().clearSelection()

    def set_frozen_state_of_coins(self, utxos: Sequence[PartialTxInput], freeze: bool):
        utxos_str = {utxo.prevout.to_str() for utxo in utxos}
        self.wallet.set_frozen_state_of_coins(utxos_str, freeze)
        self.utxo_list.refresh_all()
        self.utxo_list.selectionModel().clearSelection()

    def create_list_tab(self, l, toolbar=None):
        w = QWidget()
        w.searchable_list = l
        vbox = QVBoxLayout()
        w.setLayout(vbox)
        # vbox.setContentsMargins(0, 0, 0, 0)
        # vbox.setSpacing(0)
        if toolbar:
            vbox.addLayout(toolbar)
        vbox.addWidget(l)
        return w

    def create_addresses_tab(self):
        from .address_list import AddressList
        self.address_list = l = AddressList(self)
        toolbar = l.create_toolbar(self.config)
        tab = self.create_list_tab(l, toolbar)
        toolbar_shown = bool(self.config.get('show_toolbar_addresses', True))
        l.show_toolbar(toolbar_shown)
        return tab

    def create_utxo_tab(self):
        from .utxo_list import UTXOList
        self.utxo_list = UTXOList(self)
        return self.create_list_tab(self.utxo_list)

    def create_contacts_tab(self):
        from .contact_list import ContactList
        self.contact_list = l = ContactList(self)
        return self.create_list_tab(l)

    def remove_address(self, addr):
        if not self.question(_("Do you want to remove {} from your wallet?").format(addr)):
            return
        try:
            self.wallet.delete_address(addr)
        except UserFacingException as e:
            self.show_error(str(e))
        else:
            self.need_update.set()  # history, addresses, coins
            self.receive_tab.do_clear()

    def payto_contacts(self, labels):
        self.send_tab.payto_contacts(labels)

    def set_contact(self, label, address):
        if not is_address(address):
            self.show_error(_('Invalid Address'))
            self.contact_list.update()  # Displays original unchanged value
            return False
        self.contacts[address] = ('address', label)
        self.contact_list.update()
        self.history_list.update()
        self.update_completions()
        return True

    def delete_contacts(self, labels):
        if not self.question(_("Remove {} from your list of contacts?")
                                     .format(" + ".join(labels))):
            return
        for label in labels:
            self.contacts.pop(label)
        self.history_list.update()
        self.contact_list.update()
        self.update_completions()

    def show_onchain_invoice(self, invoice: Invoice):
        amount_str = self.format_amount(invoice.get_amount_sat()) + ' ' + self.base_unit()
        d = WindowModalDialog(self, _("Onchain Invoice"))
        vbox = QVBoxLayout(d)
        grid = QGridLayout()
        grid.addWidget(QLabel(_("Amount") + ':'), 1, 0)
        grid.addWidget(QLabel(amount_str), 1, 1)
        if len(invoice.outputs) == 1:
            grid.addWidget(QLabel(_("Address") + ':'), 2, 0)
            grid.addWidget(QLabel(invoice.get_address()), 2, 1)
        else:
            outputs_str = '\n'.join(
                map(lambda x: str(x.address) + ' : ' + self.format_amount(x.value) + (self.base_unit() if not x.asset else (' ' + x.asset)), invoice.outputs))
            grid.addWidget(QLabel(_("Outputs") + ':'), 2, 0)
            grid.addWidget(QLabel(outputs_str), 2, 1)
        grid.addWidget(QLabel(_("Description") + ':'), 3, 0)
        grid.addWidget(QLabel(invoice.message), 3, 1)
        if invoice.exp:
            grid.addWidget(QLabel(_("Expires") + ':'), 4, 0)
            grid.addWidget(QLabel(format_time(invoice.exp + invoice.time)), 4, 1)
        if invoice.bip70:
            pr = paymentrequest.PaymentRequest(bytes.fromhex(invoice.bip70))
            pr.verify(self.contacts)
            grid.addWidget(QLabel(_("Requestor") + ':'), 5, 0)
            grid.addWidget(QLabel(pr.get_requestor()), 5, 1)
            grid.addWidget(QLabel(_("Signature") + ':'), 6, 0)
            grid.addWidget(QLabel(pr.get_verify_status()), 6, 1)

            def do_export():
                name = pr.get_name_for_export() or "payment_request"
                name = f"{name}.bip70"
                fn = getSaveFileName(
                    parent=self,
                    title=_("Save invoice to file"),
                    filename=name,
                    filter="*.bip70",
                    config=self.config,
                )
                if not fn:
                    return
                with open(fn, 'wb') as f:
                    data = f.write(pr.raw)
                self.show_message(_('BIP70 invoice saved as {}').format(fn))

            exportButton = EnterButton(_('Export'), do_export)
            buttons = Buttons(exportButton, CloseButton(d))
        else:
            buttons = Buttons(CloseButton(d))
        vbox.addLayout(grid)
        vbox.addLayout(buttons)
        d.exec_()

    def show_lightning_invoice(self, invoice: Invoice):
        lnaddr = lndecode(invoice.lightning_invoice)
        d = WindowModalDialog(self, _("Lightning Invoice"))
        vbox = QVBoxLayout(d)
        grid = QGridLayout()
        pubkey_e = ShowQRLineEdit(lnaddr.pubkey.serialize().hex(), self.config, title=_("Public Key"))
        pubkey_e.setMinimumWidth(700)
        grid.addWidget(QLabel(_("Public Key") + ':'), 0, 0)
        grid.addWidget(pubkey_e, 0, 1)
        grid.addWidget(QLabel(_("Amount") + ':'), 1, 0)
        amount_str = self.format_amount(invoice.get_amount_sat()) + ' ' + self.base_unit()
        grid.addWidget(QLabel(amount_str), 1, 1)
        grid.addWidget(QLabel(_("Description") + ':'), 2, 0)
        grid.addWidget(QLabel(invoice.message), 2, 1)
        grid.addWidget(QLabel(_("Creation time") + ':'), 3, 0)
        grid.addWidget(QLabel(format_time(invoice.time)), 3, 1)
        if invoice.exp:
            grid.addWidget(QLabel(_("Expiration time") + ':'), 4, 0)
            grid.addWidget(QLabel(format_time(invoice.time + invoice.exp)), 4, 1)
        grid.addWidget(QLabel(_('Features') + ':'), 5, 0)
        grid.addWidget(QLabel('\n'.join(lnaddr.get_features().get_names())), 5, 1)
        payhash_e = ShowQRLineEdit(lnaddr.paymenthash.hex(), self.config, title=_("Payment Hash"))
        grid.addWidget(QLabel(_("Payment Hash") + ':'), 6, 0)
        grid.addWidget(payhash_e, 6, 1)
        invoice_e = ShowQRTextEdit(config=self.config)
        invoice_e.setFont(QFont(MONOSPACE_FONT))
        invoice_e.addCopyButton()
        invoice_e.setText(invoice.lightning_invoice)
        grid.addWidget(QLabel(_('Text') + ':'), 7, 0)
        grid.addWidget(invoice_e, 7, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CloseButton(d),))
        d.exec_()

    def create_console_tab(self):
        from .console import Console
        self.console = console = Console()
        return console

    def update_console(self):
        console = self.console
        console.history = self.wallet.db.get("qt-console-history", [])
        console.history_index = len(console.history)

        console.updateNamespace({
            'wallet': self.wallet,
            'network': self.network,
            'plugins': self.gui_object.plugins,
            'window': self,
            'config': self.config,
            'electrum': electrum,
            'daemon': self.gui_object.daemon,
            'util': util,
            'bitcoin': evrmore,
            'lnutil': lnutil,
        })

        c = commands.Commands(
            config=self.config,
            daemon=self.gui_object.daemon,
            network=self.network,
            callback=lambda: self.console.set_json(True))
        methods = {}

        def mkfunc(f, method):
            return lambda *args, **kwargs: f(method,
                                             args,
                                             self.password_dialog,
                                             **{**kwargs, 'wallet': self.wallet})

        for m in dir(c):
            if m[0] == '_' or m in ['network', 'wallet', 'config', 'daemon']: continue
            methods[m] = mkfunc(c._run, m)

        console.updateNamespace(methods)

    def show_balance_dialog(self):
        balance = sum(self.wallet.get_balances_for_piechart(), EvrmoreValue())
        if balance == EvrmoreValue():
            return
        from .balance_dialog import BalanceDialog
        d = BalanceDialog(self, wallet=self.wallet)
        d.run()

    def create_status_bar(self):
        sb = QStatusBar()
        sb.setFixedHeight(35)
        self.balance_label = BalanceToolButton()
        self.balance_label.setText("Loading wallet...")
        self.balance_label.setAutoRaise(True)
        self.balance_label.clicked.connect(self.show_balance_dialog)
        sb.addWidget(self.balance_label)

        # remove border of all items in status bar
        self.setStyleSheet("QStatusBar::item { border: 0px;} ")

        self.search_box = QLineEdit()
        self.search_box.textChanged.connect(self.do_search)
        self.search_box.hide()
        sb.addPermanentWidget(self.search_box)

        self.update_check_button = QPushButton("")
        self.update_check_button.setFlat(True)
        self.update_check_button.setCursor(QCursor(Qt.PointingHandCursor))
        self.update_check_button.setIcon(read_QIcon("update.png"))
        self.update_check_button.hide()
        sb.addPermanentWidget(self.update_check_button)

        self.tasks_label = QLabel('')
        sb.addPermanentWidget(self.tasks_label)

        self.password_button = StatusBarButton(QIcon(), _("Password"), self.change_password_dialog)
        sb.addPermanentWidget(self.password_button)

        sb.addPermanentWidget(StatusBarButton(read_QIcon("preferences.png"), _("Preferences"), self.settings_dialog))
        self.seed_button = StatusBarButton(read_QIcon("seed.png"), _("Seed"), self.show_seed_dialog)
        sb.addPermanentWidget(self.seed_button)
        self.lightning_button = StatusBarButton(read_QIcon("lightning.png"), _("Lightning Network"),
                                                self.gui_object.show_lightning_dialog)
        sb.addPermanentWidget(self.lightning_button)
        self.update_lightning_icon()
        self.status_button = None
        if self.network:
            self.status_button = StatusBarButton(read_QIcon("status_disconnected.png"), _("Network"),
                                                 self.gui_object.show_network_dialog)
            sb.addPermanentWidget(self.status_button)
        run_hook('create_status_bar', sb)
        self.setStatusBar(sb)

    def create_coincontrol_statusbar(self):
        self.coincontrol_sb = sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        # sb.setFixedHeight(3 * char_width_in_lineedit())
        sb.setStyleSheet('QStatusBar::item {border: None;} '
                         + ColorScheme.GREEN.as_stylesheet(True))

        self.coincontrol_label = QLabel()
        self.coincontrol_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.coincontrol_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sb.addWidget(self.coincontrol_label)

        clear_cc_button = EnterButton(_('Reset'), lambda: self.utxo_list.set_spend_list(None))
        clear_cc_button.setStyleSheet("margin-right: 5px;")
        sb.addPermanentWidget(clear_cc_button)

        sb.setVisible(False)
        return sb

    def set_coincontrol_msg(self, msg: Optional[str]) -> None:
        if not msg:
            self.coincontrol_label.setText("")
            self.coincontrol_sb.setVisible(False)
            return
        self.coincontrol_label.setText(msg)
        self.coincontrol_sb.setVisible(True)

    def update_lightning_icon(self):
        if not self.wallet.has_lightning():
            self.lightning_button.setVisible(False)
            return
        if self.network is None or self.network.channel_db is None:
            self.lightning_button.setVisible(False)
            return
        self.lightning_button.setVisible(True)

        cur, total, progress_percent = self.network.lngossip.get_sync_progress_estimate()
        # self.logger.debug(f"updating lngossip sync progress estimate: cur={cur}, total={total}")
        progress_str = "??%"
        if progress_percent is not None:
            progress_str = f"{progress_percent}%"
        if progress_percent and progress_percent >= 100:
            self.lightning_button.setMaximumWidth(25)
            self.lightning_button.setText('')
            self.lightning_button.setToolTip(_("The Lightning Network graph is fully synced."))
        else:
            self.lightning_button.setMaximumWidth(25 + 5 * char_width_in_lineedit())
            self.lightning_button.setText(progress_str)
            self.lightning_button.setToolTip(_("The Lightning Network graph is syncing...\n"
                                               "Payments are more likely to succeed with a more complete graph."))

    def update_lock_icon(self):
        icon = read_QIcon("lock.png") if self.wallet.has_password() else read_QIcon("unlock.png")
        self.password_button.setIcon(icon)

    def update_buttons_on_seed(self):
        self.seed_button.setVisible(self.wallet.has_seed())
        self.password_button.setVisible(self.wallet.may_have_password())

    def change_password_dialog(self):
        from electrum.storage import StorageEncryptionVersion
        if self.wallet.get_available_storage_encryption_version() == StorageEncryptionVersion.XPUB_PASSWORD:
            from .password_dialog import ChangePasswordDialogForHW
            d = ChangePasswordDialogForHW(self, self.wallet)
            ok, encrypt_file = d.run()
            if not ok:
                return

            def on_password(hw_dev_pw):
                old_password = hw_dev_pw if self.wallet.has_password() else None
                new_password = hw_dev_pw if encrypt_file else None
                self._update_wallet_password(
                    old_password=old_password, new_password=new_password, encrypt_storage=encrypt_file)

            self.thread.add(
                self.wallet.keystore.get_password_for_storage_encryption,
                on_success=on_password)
        else:
            from .password_dialog import ChangePasswordDialogForSW
            d = ChangePasswordDialogForSW(self, self.wallet)
            ok, old_password, new_password, encrypt_file = d.run()
            if not ok:
                return
            self._update_wallet_password(
                old_password=old_password, new_password=new_password, encrypt_storage=encrypt_file)

    def _update_wallet_password(self, *, old_password, new_password, encrypt_storage: bool):
        try:
            self.wallet.update_password(old_password, new_password, encrypt_storage=encrypt_storage)
        except InvalidPassword as e:
            self.show_error(str(e))
            return
        except BaseException:
            self.logger.exception('Failed to update password')
            self.show_error(_('Failed to update password'))
            return
        msg = _('Password was updated successfully') if self.wallet.has_password() else _(
            'Password is disabled, this wallet is not protected')
        self.show_message(msg, title=_("Success"))
        self.update_lock_icon()

    def toggle_search(self):
        self.search_box.setHidden(not self.search_box.isHidden())
        if not self.search_box.isHidden():
            self.search_box.setFocus(1)
        else:
            self.do_search('')

    def do_search(self, t):
        tab = self.tabs.currentWidget()
        if hasattr(tab, 'searchable_list'):
            tab.searchable_list.filter(t)

    def new_contact_dialog(self):
        d = WindowModalDialog(self, _("New Contact"))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_('New Contact') + ':'))
        grid = QGridLayout()
        line1 = QLineEdit()
        line1.setFixedWidth(32 * char_width_in_lineedit())
        line2 = QLineEdit()
        line2.setFixedWidth(32 * char_width_in_lineedit())
        grid.addWidget(QLabel(_("Address")), 1, 0)
        grid.addWidget(line1, 1, 1)
        grid.addWidget(QLabel(_("Name")), 2, 0)
        grid.addWidget(line2, 2, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if d.exec_():
            self.set_contact(line2.text(), line1.text())

    def init_lightning_dialog(self, dialog):
        assert not self.wallet.has_lightning()
        if self.wallet.can_have_deterministic_lightning():
            msg = _(
                "Lightning is not enabled because this wallet was created with an old version of Electrum. "
                "Create lightning keys?")
        else:
            msg = _(
                "Warning: this wallet type does not support channel recovery from seed. "
                "You will need to backup your wallet everytime you create a new channel. "
                "Create lightning keys?")
        if self.question(msg):
            self._init_lightning_dialog(dialog=dialog)

    @protected
    def _init_lightning_dialog(self, *, dialog, password):
        dialog.close()
        self.wallet.init_lightning(password=password)
        self.update_lightning_icon()
        self.show_message(_('Lightning keys have been initialized.'))

    def show_wallet_info(self):
        dialog = WindowModalDialog(self, _("Wallet Information"))
        dialog.setMinimumSize(800, 100)
        vbox = QVBoxLayout()
        wallet_type = self.wallet.db.get('wallet_type', '')
        if self.wallet.is_watching_only():
            wallet_type += ' [{}]'.format(_('watching-only'))
        seed_available = _('False')
        if self.wallet.has_seed():
            seed_available = _('True')
            ks = self.wallet.keystore
            assert isinstance(ks, keystore.Deterministic_KeyStore)
            seed_available += f" ({ks.get_seed_type()})"
        keystore_types = [k.get_type_text() for k in self.wallet.get_keystores()]
        grid = QGridLayout()
        basename = os.path.basename(self.wallet.storage.path)
        grid.addWidget(WWLabel(_("Wallet name")+ ':'), 0, 0)
        grid.addWidget(WWLabel(basename), 0, 1)
        grid.addWidget(WWLabel(_("Wallet type")+ ':'), 1, 0)
        grid.addWidget(WWLabel(wallet_type), 1, 1)
        grid.addWidget(WWLabel(_("Script type")+ ':'), 2, 0)
        grid.addWidget(WWLabel(self.wallet.txin_type), 2, 1)
        grid.addWidget(WWLabel(_("Seed available") + ':'), 3, 0)
        grid.addWidget(WWLabel(str(seed_available)), 3, 1)
        if len(keystore_types) <= 1:
            grid.addWidget(WWLabel(_("Keystore type") + ':'), 4, 0)
            ks_type = str(keystore_types[0]) if keystore_types else _('No keystore')
            grid.addWidget(WWLabel(ks_type), 4, 1)
        # lightning
        grid.addWidget(WWLabel(_('Lightning') + ':'), 5, 0)
        from .util import IconLabel
        if self.wallet.has_lightning():
            if self.wallet.lnworker.has_deterministic_node_id():
                grid.addWidget(WWLabel(_('Enabled')), 5, 1)
            else:
                label = IconLabel(text='Enabled, non-recoverable channels')
                label.setIcon(read_QIcon('nocloud'))
                grid.addWidget(label, 5, 1)
                if self.wallet.db.get('seed_type') == 'segwit':
                    msg = _(
                        "Your channels cannot be recovered from seed, because they were created with an old version of Electrum. "
                        "This means that you must save a backup of your wallet everytime you create a new channel.\n\n"
                        "If you want this wallet to have recoverable channels, you must close your existing channels and restore this wallet from seed")
                else:
                    msg = _("Your channels cannot be recovered from seed. "
                            "This means that you must save a backup of your wallet everytime you create a new channel.\n\n"
                            "If you want to have recoverable channels, you must create a new wallet with an Electrum seed")
                grid.addWidget(HelpButton(msg), 5, 3)
            grid.addWidget(WWLabel(_('Lightning Node ID:')), 7, 0)
            nodeid_text = self.wallet.lnworker.node_keypair.pubkey.hex()
            nodeid_e = ShowQRLineEdit(nodeid_text, self.config, title=_("Node ID"))
            grid.addWidget(nodeid_e, 8, 0, 1, 4)
        else:
            if self.wallet.can_have_lightning():
                grid.addWidget(WWLabel('Not enabled'), 5, 1)
                button = QPushButton(_("Enable"))
                button.pressed.connect(lambda: self.init_lightning_dialog(dialog))
                grid.addWidget(button, 5, 3)
            else:
                grid.addWidget(WWLabel(_("Not available for this wallet.")), 5, 1)
                grid.addWidget(HelpButton(_("Lightning is currently restricted to HD wallets with p2wpkh addresses.")), 5, 2)
        vbox.addLayout(grid)

        labels_clayout = None

        if self.wallet.is_deterministic():
            keystores = self.wallet.get_keystores()

            ks_stack = QStackedWidget()

            def select_ks(index):
                ks_stack.setCurrentIndex(index)

            # only show the combobox in case multiple accounts are available
            if len(keystores) > 1:
                def label(idx, ks):
                    if isinstance(self.wallet, Multisig_Wallet) and hasattr(ks, 'label'):
                        return _("cosigner") + f' {idx + 1}: {ks.get_type_text()} {ks.label}'
                    else:
                        return _("keystore") + f' {idx + 1}'

                labels = [label(idx, ks) for idx, ks in enumerate(self.wallet.get_keystores())]

                on_click = lambda clayout: select_ks(clayout.selected_index())
                labels_clayout = ChoicesLayout(_("Select keystore"), labels, on_click)
                vbox.addLayout(labels_clayout.layout())

            for ks in keystores:
                ks_w = QWidget()
                ks_vbox = QVBoxLayout()
                ks_vbox.setContentsMargins(0, 0, 0, 0)
                ks_w.setLayout(ks_vbox)

                mpk_text = ShowQRTextEdit(ks.get_master_public_key(), config=self.config)
                mpk_text.setMaximumHeight(150)
                mpk_text.addCopyButton()
                run_hook('show_xpub_button', mpk_text, ks)
                ks_vbox.addWidget(WWLabel(_("Master Public Key")))
                ks_vbox.addWidget(mpk_text)

                der_path_hbox = QHBoxLayout()
                der_path_hbox.setContentsMargins(0, 0, 0, 0)
                der_path_hbox.addWidget(WWLabel(_("Derivation path") + ':'))
                der_path_text = WWLabel(ks.get_derivation_prefix() or _("unknown"))
                der_path_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
                der_path_hbox.addWidget(der_path_text)
                der_path_hbox.addStretch()
                ks_vbox.addLayout(der_path_hbox)

                bip32fp_hbox = QHBoxLayout()
                bip32fp_hbox.setContentsMargins(0, 0, 0, 0)
                bip32fp_hbox.addWidget(QLabel("BIP32 root fingerprint:"))
                bip32fp_text = WWLabel(ks.get_root_fingerprint() or _("unknown"))
                bip32fp_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
                bip32fp_hbox.addWidget(bip32fp_text)
                bip32fp_hbox.addStretch()
                ks_vbox.addLayout(bip32fp_hbox)

                ks_stack.addWidget(ks_w)

            select_ks(0)
            vbox.addWidget(ks_stack)

        vbox.addStretch(1)
        btn_export_info = run_hook('wallet_info_buttons', self, dialog)
        btn_close = CloseButton(dialog)
        btns = Buttons(btn_export_info, btn_close)
        vbox.addLayout(btns)
        dialog.setLayout(vbox)
        dialog.exec_()

    def remove_wallet(self):
        if self.question('\n'.join([
            _('Delete wallet file?'),
            "%s" % self.wallet.storage.path,
            _('If your wallet contains funds, make sure you have saved its seed.')])):
            self._delete_wallet()

    @protected
    def _delete_wallet(self, password):
        wallet_path = self.wallet.storage.path
        basename = os.path.basename(wallet_path)
        r = self.gui_object.daemon.delete_wallet(wallet_path)
        self.close()
        if r:
            self.show_error(_("Wallet removed: {}").format(basename))
        else:
            self.show_error(_("Wallet file not found: {}").format(basename))

    @protected
    def show_seed_dialog(self, password):
        if not self.wallet.has_seed():
            self.show_message(_('This wallet has no seed'))
            return
        keystore = self.wallet.get_keystore()
        try:
            seed = keystore.get_seed(password)
            passphrase = keystore.get_passphrase(password)
        except BaseException as e:
            self.show_error(repr(e))
            return
        from .seed_dialog import SeedDialog
        d = SeedDialog(self, seed, passphrase, config=self.config)
        d.exec_()

    def show_qrcode(self, data, title=_("QR code"), parent=None, *,
                    help_text=None, show_copy_text_btn=False):
        if not data:
            return
        d = QRDialog(
            data=data,
            parent=parent or self,
            title=title,
            help_text=help_text,
            show_copy_text_btn=show_copy_text_btn,
            config=self.config,
        )
        d.exec_()

    @protected
    def show_private_key(self, address, password):
        if not address:
            return
        try:
            pk = self.wallet.export_private_key(address, password)
        except Exception as e:
            self.logger.exception('')
            self.show_message(repr(e))
            return
        xtype = evrmore.deserialize_privkey(pk)[0]
        d = WindowModalDialog(self, _("Private key"))
        d.setMinimumSize(600, 150)
        vbox = QVBoxLayout()
        vbox.addWidget(QLabel(_("Address") + ': ' + address))
        vbox.addWidget(QLabel(_("Script type") + ': ' + xtype))
        vbox.addWidget(QLabel(_("Private key") + ':'))
        keys_e = ShowQRTextEdit(text=pk, config=self.config)
        keys_e.addCopyButton()
        vbox.addWidget(keys_e)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.setLayout(vbox)
        d.exec_()

    msg_sign = _("Signing with an address actually means signing with the corresponding "
                 "private key, and verifying with the corresponding public key. The "
                 "address you have entered does not have a unique public key, so these "
                 "operations cannot be performed.") + '\n\n' + \
               _('The operation is undefined. Not just in Electrum, but in general.')

    @protected
    def do_sign(self, address, message, signature, password):
        address = address.text().strip()
        message = message.toPlainText().strip()
        if not evrmore.is_address(address):
            self.show_message(_('Invalid Evrmore address.'))
            return
        if self.wallet.is_watching_only():
            self.show_message(_('This is a watching-only wallet.'))
            return
        if not self.wallet.is_mine(address):
            self.show_message(_('Address not in wallet.'))
            return
        txin_type = self.wallet.get_txin_type(address)
        if txin_type not in ['p2pkh', 'p2wpkh', 'p2wpkh-p2sh']:
            self.show_message(_('Cannot sign messages with this type of address:') + \
                              ' ' + txin_type + '\n\n' + self.msg_sign)
            return
        task = partial(self.wallet.sign_message, address, message, password)

        def show_signed_message(sig):
            try:
                signature.setText(base64.b64encode(sig).decode('ascii'))
            except RuntimeError:
                # (signature) wrapped C/C++ object has been deleted
                pass

        self.thread.add(task, on_success=show_signed_message)

    def do_verify(self, address, message, signature):
        address = address.text().strip()
        message = message.toPlainText().strip().encode('utf-8')
        if not evrmore.is_address(address):
            self.show_message(_('Invalid Evrmore address.'))
            return
        try:
            # This can throw on invalid base64
            sig = base64.b64decode(str(signature.toPlainText()))
            verified = ecc.verify_message_with_address(address, sig, message)
        except Exception as e:
            verified = False
        if verified:
            self.show_message(_("Signature verified"))
        else:
            self.show_error(_("Wrong signature"))

    def sign_verify_message(self, address=''):
        d = WindowModalDialog(self, _('Sign/verify Message'))
        d.setMinimumSize(610, 290)

        layout = QGridLayout(d)

        message_e = QTextEdit()
        message_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Message')), 1, 0)
        layout.addWidget(message_e, 1, 1)
        layout.setRowStretch(2, 3)

        address_e = QLineEdit()
        address_e.setText(address)
        layout.addWidget(QLabel(_('Address')), 2, 0)
        layout.addWidget(address_e, 2, 1)

        signature_e = ScanShowQRTextEdit(config=self.config)
        layout.addWidget(QLabel(_('Signature')), 3, 0)
        layout.addWidget(signature_e, 3, 1)
        layout.setRowStretch(3, 1)

        hbox = QHBoxLayout()

        b = QPushButton(_("Sign"))
        b.clicked.connect(lambda: self.do_sign(address_e, message_e, signature_e))
        hbox.addWidget(b)

        b = QPushButton(_("Verify"))
        b.clicked.connect(lambda: self.do_verify(address_e, message_e, signature_e))
        hbox.addWidget(b)

        b = QPushButton(_("Close"))
        b.clicked.connect(d.accept)
        hbox.addWidget(b)
        layout.addLayout(hbox, 4, 1)
        d.exec_()

    @protected
    def do_decrypt(self, message_e, pubkey_e, encrypted_e, password):
        if self.wallet.is_watching_only():
            self.show_message(_('This is a watching-only wallet.'))
            return
        cyphertext = encrypted_e.toPlainText()
        task = partial(self.wallet.decrypt_message, pubkey_e.text(), cyphertext, password)

        def setText(text):
            try:
                message_e.setText(text.decode('utf-8'))
            except RuntimeError:
                # (message_e) wrapped C/C++ object has been deleted
                pass

        self.thread.add(task, on_success=setText)

    def do_encrypt(self, message_e, pubkey_e, encrypted_e):
        message = message_e.toPlainText()
        message = message.encode('utf-8')
        try:
            public_key = ecc.ECPubkey(bfh(pubkey_e.text()))
        except BaseException as e:
            self.logger.exception('Invalid Public key')
            self.show_warning(_('Invalid Public key'))
            return
        encrypted = public_key.encrypt_message(message)
        encrypted_e.setText(encrypted.decode('ascii'))

    def encrypt_message(self, address=''):
        d = WindowModalDialog(self, _('Encrypt/decrypt Message'))
        d.setMinimumSize(610, 490)

        layout = QGridLayout(d)

        message_e = QTextEdit()
        message_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Message')), 1, 0)
        layout.addWidget(message_e, 1, 1)
        layout.setRowStretch(2, 3)

        pubkey_e = QLineEdit()
        if address:
            pubkey = self.wallet.get_public_key(address)
            pubkey_e.setText(pubkey)
        layout.addWidget(QLabel(_('Public key')), 2, 0)
        layout.addWidget(pubkey_e, 2, 1)

        encrypted_e = QTextEdit()
        encrypted_e.setAcceptRichText(False)
        layout.addWidget(QLabel(_('Encrypted')), 3, 0)
        layout.addWidget(encrypted_e, 3, 1)
        layout.setRowStretch(3, 1)

        hbox = QHBoxLayout()
        b = QPushButton(_("Encrypt"))
        b.clicked.connect(lambda: self.do_encrypt(message_e, pubkey_e, encrypted_e))
        hbox.addWidget(b)

        b = QPushButton(_("Decrypt"))
        b.clicked.connect(lambda: self.do_decrypt(message_e, pubkey_e, encrypted_e))
        hbox.addWidget(b)

        b = QPushButton(_("Close"))
        b.clicked.connect(d.accept)
        hbox.addWidget(b)

        layout.addLayout(hbox, 4, 1)
        d.exec_()

    def password_dialog(self, msg=None, parent=None):
        from .password_dialog import PasswordDialog
        parent = parent or self
        d = PasswordDialog(parent, msg)
        return d.run()

    def tx_from_text(self, data: Union[str, bytes]) -> Union[None, 'PartialTransaction', 'Transaction']:
        from electrum.transaction import tx_from_any
        try:
            return tx_from_any(data)
        except BaseException as e:
            self.show_critical(_("Electrum was unable to parse your transaction") + ":\n" + repr(e))
            return

    def import_channel_backup(self, encrypted: str):
        if not self.question('Import channel backup?'):
            return
        try:
            self.wallet.lnworker.import_channel_backup(encrypted)
        except Exception as e:
            self.show_error("failed to import backup" + '\n' + str(e))
            return

    def read_tx_from_qrcode(self):
        def cb(success: bool, error: str, data):
            if not success:
                if error:
                    self.show_error(error)
                return
            if not data:
                return
            # if the user scanned a bitcoin URI
            if data.lower().startswith(BITCOIN_BIP21_URI_SCHEME + ':'):
                self.handle_payment_identifier(data)
                return
            if data.lower().startswith('channel_backup:'):
                self.import_channel_backup(data)
                return
            # else if the user scanned an offline signed tx
            tx = self.tx_from_text(data)
            if not tx:
                return
            self.show_transaction(tx)

        scan_qrcode(parent=self.top_level_window(), config=self.config, callback=cb)

    def read_tx_from_file(self) -> Optional[Transaction]:
        fileName = getOpenFileName(
            parent=self,
            title=_("Select your transaction file"),
            filter=TRANSACTION_FILE_EXTENSION_FILTER_ANY,
            config=self.config,
        )
        if not fileName:
            return
        try:
            with open(fileName, "rb") as f:
                file_content = f.read()  # type: Union[str, bytes]
        except (ValueError, IOError, os.error) as reason:
            self.show_critical(_("Electrum was unable to open your transaction file") + "\n" + str(reason),
                               title=_("Unable to read file or no transaction found"))
            return
        return self.tx_from_text(file_content)

    def do_process_from_text(self):
        text = text_dialog(
            parent=self,
            title=_('Input raw transaction'),
            header_layout=_("Transaction:"),
            ok_label=_("Load transaction"),
            config=self.config,
        )
        if not text:
            return
        tx = self.tx_from_text(text)
        if tx:
            self.show_transaction(tx)

    def do_process_from_text_channel_backup(self):
        text = text_dialog(
            parent=self,
            title=_('Input channel backup'),
            header_layout=_("Channel Backup:"),
            ok_label=_("Load backup"),
            config=self.config,
        )
        if not text:
            return
        if text.startswith('channel_backup:'):
            self.import_channel_backup(text)

    def do_process_from_file(self):
        tx = self.read_tx_from_file()
        if tx:
            self.show_transaction(tx)

    def do_process_from_txid(self):
        from electrum import transaction
        txid, ok = QInputDialog.getText(self, _('Lookup transaction'), _('Transaction ID') + ':')
        if ok and txid:
            txid = str(txid).strip()
            raw_tx = self._fetch_tx_from_network(txid)
            if not raw_tx:
                return
            tx = transaction.Transaction(raw_tx)
            self.show_transaction(tx)

    def _fetch_tx_from_network(self, txid: str, show_message = True) -> Optional[str]:
        if not self.network:
            self.show_message(_("You are offline."))
            return
        try:
            raw_tx = self.network.run_from_another_thread(
                self.network.get_transaction(txid, timeout=10))
        except UntrustedServerReturnedError as e:
            if show_message:
                self.logger.info(f"Error getting transaction from network: {repr(e)}")
                self.show_message(_("Error getting transaction from network") + ":\n" + e.get_message_for_gui())
            return
        except Exception as e:
            if show_message:
                self.show_message(_("Error getting transaction from network") + ":\n" + repr(e))
            return
        return raw_tx

    @protected
    def export_privkeys_dialog(self, password):
        if self.wallet.is_watching_only():
            self.show_message(_("This is a watching-only wallet"))
            return

        if isinstance(self.wallet, Multisig_Wallet):
            self.show_message(_('WARNING: This is a multi-signature wallet.') + '\n' +
                              _('It cannot be "backed up" by simply exporting these private keys.'))

        d = WindowModalDialog(self, _('Private keys'))
        d.setMinimumSize(980, 300)
        vbox = QVBoxLayout(d)

        msg = "%s\n%s\n%s" % (_("WARNING: ALL your private keys are secret."),
                              _("Exposing a single private key can compromise your entire wallet!"),
                              _("In particular, DO NOT use 'redeem private key' services proposed by third parties."))
        vbox.addWidget(QLabel(msg))

        e = QTextEdit()
        e.setReadOnly(True)
        vbox.addWidget(e)

        defaultname = 'electrum-private-keys.csv'
        select_msg = _('Select file to export your private keys to')
        hbox, filename_e, csv_button = filename_field(self, self.config, defaultname, select_msg)
        vbox.addLayout(hbox)

        b = OkButton(d, _('Export'))
        b.setEnabled(False)
        vbox.addLayout(Buttons(CancelButton(d), b))

        private_keys = {}
        addresses = self.wallet.get_addresses()
        done = False
        cancelled = False

        def privkeys_thread():
            for addr in addresses:
                time.sleep(0.1)
                if done or cancelled:
                    break
                privkey = self.wallet.export_private_key(addr, password)
                private_keys[addr] = privkey
                self.computing_privkeys_signal.emit()
            if not cancelled:
                self.computing_privkeys_signal.disconnect()
                self.show_privkeys_signal.emit()

        def show_privkeys():
            s = "\n".join(map(lambda x: x[0] + "\t" + x[1], private_keys.items()))
            e.setText(s)
            b.setEnabled(True)
            self.show_privkeys_signal.disconnect()
            nonlocal done
            done = True

        def on_dialog_closed(*args):
            nonlocal done
            nonlocal cancelled
            if not done:
                cancelled = True
                self.computing_privkeys_signal.disconnect()
                self.show_privkeys_signal.disconnect()

        self.computing_privkeys_signal.connect(
            lambda: e.setText("Please wait... %d/%d" % (len(private_keys), len(addresses))))
        self.show_privkeys_signal.connect(show_privkeys)
        d.finished.connect(on_dialog_closed)
        threading.Thread(target=privkeys_thread).start()

        if not d.exec_():
            done = True
            return

        filename = filename_e.text()
        if not filename:
            return

        try:
            self.do_export_privkeys(filename, private_keys, csv_button.isChecked())
        except (IOError, os.error) as reason:
            txt = "\n".join([
                _("Electrum was unable to produce a private key-export."),
                str(reason)
            ])
            self.show_critical(txt, title=_("Unable to create csv"))

        except Exception as e:
            self.show_message(repr(e))
            return

        self.show_message(_("Private keys exported."))

    def do_export_privkeys(self, fileName, pklist, is_csv):
        with open(fileName, "w+") as f:
            os.chmod(fileName, 0o600)
            if is_csv:
                transaction = csv.writer(f)
                transaction.writerow(["address", "private_key"])
                for addr, pk in pklist.items():
                    transaction.writerow(["%34s" % addr, pk])
            else:
                f.write(json.dumps(pklist, indent=4))

    def do_import_labels(self):
        def on_import():
            self.need_update.set()

        import_meta_gui(self, _('labels'), self.wallet.import_labels, on_import)

    def do_export_labels(self):
        export_meta_gui(self, _('labels'), self.wallet.export_labels)

    def import_invoices(self):
        import_meta_gui(self, _('invoices'), self.wallet.import_invoices, self.send_tab.invoice_list.update)

    def export_invoices(self):
        export_meta_gui(self, _('invoices'), self.wallet.export_invoices)

    def import_requests(self):
        import_meta_gui(self, _('requests'), self.wallet.import_requests, self.receive_tab.request_list.update)

    def export_requests(self):
        export_meta_gui(self, _('requests'), self.wallet.export_requests)

    def import_contacts(self):
        import_meta_gui(self, _('contacts'), self.contacts.import_file, self.contact_list.update)

    def export_contacts(self):
        export_meta_gui(self, _('contacts'), self.contacts.export_file)

    def sweep_key_dialog(self):
        d = WindowModalDialog(self, title=_('Sweep private keys'))
        d.setMinimumSize(600, 300)
        vbox = QVBoxLayout(d)
        hbox_top = QHBoxLayout()
        hbox_top.addWidget(QLabel(_("EVR currently in your wallet will be used for the fee to sweep assets\n"
                                    "if there is no EVR held in the private keys.\n"
                                    "Enter private keys:")))
        hbox_top.addWidget(InfoButton(WIF_HELP_TEXT), alignment=Qt.AlignRight)
        vbox.addLayout(hbox_top)
        keys_e = ScanQRTextEdit(allow_multi=True, config=self.config)
        keys_e.setTabChangesFocus(True)
        vbox.addWidget(keys_e)

        addresses = self.wallet.get_unused_addresses()
        if not addresses:
            try:
                addresses = self.wallet.get_receiving_addresses()
            except AttributeError:
                addresses = self.wallet.get_addresses()
        h, address_e = address_field(addresses)
        vbox.addLayout(h)

        vbox.addStretch(1)
        button = OkButton(d, _('Sweep'))
    
        vbox.addLayout(Buttons(CancelButton(d), button))
        button.setEnabled(False)

        def get_address():
            addr = str(address_e.text()).strip()
            if evrmore.is_address(addr):
                return addr

        def get_pk(*, raise_on_error=False):
            text = str(keys_e.toPlainText())
            return keystore.get_private_keys(text, raise_on_error=raise_on_error)

        def on_edit():
            valid_privkeys = False
            try:
                valid_privkeys = get_pk(raise_on_error=True) is not None
            except Exception as e:
                button.setToolTip(f'{_("Error")}: {repr(e)}')
            else:
                button.setToolTip('')
            button.setEnabled(get_address() is not None and valid_privkeys)

        on_address = lambda text: address_e.setStyleSheet(
            (ColorScheme.DEFAULT if get_address() else ColorScheme.RED).as_stylesheet())
        keys_e.textChanged.connect(on_edit)
        address_e.textChanged.connect(on_edit)
        address_e.textChanged.connect(on_address)
        on_address(str(address_e.text()))
        if not d.exec_():
            return
        # user pressed "sweep"
        addr = get_address()
        try:
            self.wallet.check_address_for_corruption(addr)
        except InternalAddressCorruption as e:
            self.show_error(str(e))
            raise
        privkeys = get_pk()

        def on_success(result):
            coins, keypairs, asset_outpoints_to_locking_scripts = result
            total_held = sum([coin.value_sats() for coin in coins], EvrmoreValue())

            coins_evr = [coin for coin in coins if coin.value_sats().evr_value.value != 0]
            coins_assets = [coin for coin in coins if coin.value_sats().assets]

            self.warn_if_watching_only()

            # If there is not EVR in the privkeys, use our own
                        
            outputs = []
            if total_held.assets:
                outputs = [PartialTxOutput.from_address_and_value(addr, value='!', asset=asset) for asset in total_held.assets.keys()]
            if total_held.evr_value.value != 0:
                outputs += [PartialTxOutput.from_address_and_value(addr, value='!')]

            self.send_tab.pay_onchain_dialog(self.get_coins(), outputs, mandatory_inputs=coins_evr + coins_assets, external_keypairs=keypairs, mixed=True,
                                        locking_script_overrides=asset_outpoints_to_locking_scripts)

        def on_failure(exc_info):
            self.on_error(exc_info)

        msg = _('Preparing sweep transaction...')
        task = lambda: self.network.run_from_another_thread(
            sweep_preparations(privkeys, self.network))
        WaitingDialog(self, msg, task, on_success, on_failure)

    def _do_import(self, title, header_layout, func):
        text = text_dialog(
            parent=self,
            title=title,
            header_layout=header_layout,
            ok_label=_('Import'),
            allow_multi=True,
            config=self.config,
        )
        if not text:
            return
        keys = str(text).split()
        good_inputs, bad_inputs = func(keys)
        if good_inputs:
            msg = '\n'.join(good_inputs[:10])
            if len(good_inputs) > 10: msg += '\n...'
            self.show_message(_("The following addresses were added")
                              + f' ({len(good_inputs)}):\n' + msg)
        if bad_inputs:
            msg = "\n".join(f"{key[:10]}... ({msg})" for key, msg in bad_inputs[:10])
            if len(bad_inputs) > 10: msg += '\n...'
            self.show_error(_("The following inputs could not be imported")
                            + f' ({len(bad_inputs)}):\n' + msg)
        self.address_list.update()
        self.history_list.update()
        self.asset_view.update()

    def import_addresses(self):
        if not self.wallet.can_import_address():
            return
        title, msg = _('Import addresses'), _("Enter addresses") + ':'
        self._do_import(title, msg, self.wallet.import_addresses)

    @protected
    def do_import_privkey(self, password):
        if not self.wallet.can_import_privkey():
            return
        title = _('Import private keys')
        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel(_("Enter private keys") + ':'))
        header_layout.addWidget(InfoButton(WIF_HELP_TEXT), alignment=Qt.AlignRight)
        self._do_import(title, header_layout, lambda x: self.wallet.import_private_keys(x, password))

    def refresh_amount_edits(self):
        edits = self.send_tab.amount_e, self.receive_tab.receive_amount_e
        amounts = [edit.get_amount() for edit in edits]
        for edit, amount in zip(edits, amounts):
            edit.setAmount(amount)

    def update_fiat(self):
        b = self.fx and self.fx.is_enabled()
        self.send_tab.fiat_send_e.setVisible(b)
        self.receive_tab.fiat_receive_e.setVisible(b)
        self.history_model.refresh('update_fiat')
        self.history_list.update()
        self.address_list.refresh_headers()
        self.address_list.update()
        self.update_status()

    def settings_dialog(self):
        from .settings_dialog import SettingsDialog
        d = SettingsDialog(self, self.config)
        d.exec_()
        if self.fx:
            self.fx.trigger_update()
        run_hook('close_settings_dialog')
        if d.save_blacklist:
            self.config.set_key('asset_blacklist', self.asset_blacklist, True)
        if d.save_whitelist:
            self.config.set_key('asset_whitelist', self.asset_whitelist, True)
        if d.save_whitelist or d.save_blacklist:
            self.asset_view.update()
            self.history_model.refresh('Changed asset white or black list', True)
        if d.need_restart:
            self.show_warning(_('Please restart Electrum to activate the new GUI settings'), title=_('Success'))
        vis = self.config.get('enable_op_return_messages', False)
        self.send_tab.op_return_label.setVisible(vis)
        self.send_tab.op_return_e.setVisible(vis)
        #self.asset_memo_label.setVisible(vis and self.to_send_combo.currentIndex() != 0)
        #self.asset_memo_e.setVisible(vis and self.to_send_combo.currentIndex() != 0)

    def closeEvent(self, event):
        # note that closeEvent is NOT called if the user quits with Ctrl-C
        self.clean_up()
        event.accept()

    def clean_up(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.thread:
            self.thread.stop()
            self.thread = None
        for fut in self._coroutines_scheduled.keys():
            fut.cancel()
        self.unregister_callbacks()
        self.config.set_key("is_maximized", self.isMaximized())
        if not self.isMaximized():
            g = self.geometry()
            self.wallet.db.put("winpos-qt", [g.left(), g.top(),
                                             g.width(), g.height()])
        self.wallet.db.put("qt-console-history", self.console.history[-50:])
        if self.qr_window:
            self.qr_window.close()
        self.close_wallet()

        if self._update_check_thread:
            self._update_check_thread.exit()
            self._update_check_thread.wait()
        if self.tray:
            self.tray = None
        self.gui_object.timer.timeout.disconnect(self.timer_actions)
        self.gui_object.close_window(self)

    # TODO: On mac, this cannot be closed; disabled for now
    def logview_dialog(self):
        from electrum.logging import get_logfile_path, electrum_logger

        def watch_file(fn, logviewer):
            # poor man's tail
            if os.path.exists(fn):
                mtime = os.path.getmtime(fn)
                if mtime > self.logfile_mtime:
                    # file modified
                    self.logfile_mtime = mtime
                    logviewer.clear()
                    with open(fn, "r") as f:
                        for line in f:
                            logviewer.append(line.partition('Z |')[2].lstrip(' ').rstrip('\n'))

        d = WindowModalDialog(self, _('Log Viewer'))
        d.setMinimumSize(610, 290)
        layout = QGridLayout(d)
        self.logviewer = QTextEdit()
        self.logviewer.setAcceptRichText(False)
        self.logviewer.setReadOnly(True)
        self.logviewer.setPlainText(
            _("Enable 'Write logs to file' in Preferences -> General and restart Electrum-Evrmore to view logs here"))
        layout.addWidget(self.logviewer, 1, 1)
        logfile = get_logfile_path()
        self.logtimer = QTimer(self)
        if logfile is not None:
            load_logfile = partial(watch_file, logfile, self.logviewer)
            self.logfile_mtime = 0
            load_logfile()
            self.logtimer.timeout.connect(load_logfile)
            self.logtimer.start(2500)
        d.exec_()
        self.logtimer.stop()

    def plugins_dialog(self):
        self.pluginsdialog = d = WindowModalDialog(self, _('Electrum Plugins'))

        plugins = self.gui_object.plugins

        vbox = QVBoxLayout(d)

        # plugins
        scroll = QScrollArea()
        scroll.setEnabled(True)
        scroll.setWidgetResizable(True)
        scroll.setMinimumSize(400, 250)
        vbox.addWidget(scroll)

        w = QWidget()
        scroll.setWidget(w)
        w.setMinimumHeight(plugins.count() * 35)

        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        w.setLayout(grid)

        settings_widgets = {}

        def enable_settings_widget(p: Optional['BasePlugin'], name: str, i: int):
            widget = settings_widgets.get(name)  # type: Optional[QWidget]
            if widget and not p:
                # plugin got disabled, rm widget
                grid.removeWidget(widget)
                widget.setParent(None)
                settings_widgets.pop(name)
            elif widget is None and p and p.requires_settings() and p.is_enabled():
                # plugin got enabled, add widget
                widget = settings_widgets[name] = p.settings_widget(d)
                grid.addWidget(widget, i, 1)

        def do_toggle(cb, name, i):
            p = plugins.toggle(name)
            cb.setChecked(bool(p))
            enable_settings_widget(p, name, i)
            # note: all enabled plugins will receive this hook:
            run_hook('init_qt', self.gui_object)

        for i, descr in enumerate(plugins.descriptions.values()):
            full_name = descr['__name__']
            prefix, _separator, name = full_name.rpartition('.')
            p = plugins.get(name)
            if descr.get('registers_keystore'):
                continue
            try:
                cb = QCheckBox(descr['fullname'])
                plugin_is_loaded = p is not None
                cb_enabled = (not plugin_is_loaded and plugins.is_available(name, self.wallet)
                              or plugin_is_loaded and p.can_user_disable())
                cb.setEnabled(cb_enabled)
                cb.setChecked(plugin_is_loaded and p.is_enabled())
                grid.addWidget(cb, i, 0)
                enable_settings_widget(p, name, i)
                cb.clicked.connect(partial(do_toggle, cb, name, i))
                msg = descr['description']
                if descr.get('requires'):
                    msg += '\n\n' + _('Requires') + ':\n' + '\n'.join(map(lambda x: x[1], descr.get('requires')))
                grid.addWidget(HelpButton(msg), i, 2)
            except Exception:
                self.logger.exception(f"cannot display plugin {name}")
        grid.setRowStretch(len(plugins.descriptions.values()), 1)
        vbox.addLayout(Buttons(CloseButton(d)))
        d.exec_()

    def cpfp_dialog(self, parent_tx: Transaction) -> None:
        new_tx = self.wallet.cpfp(parent_tx, 0)
        total_size = parent_tx.estimated_size() + new_tx.estimated_size()
        parent_txid = parent_tx.txid()
        assert parent_txid
        parent_fee = self.wallet.adb.get_tx_fee(parent_txid)
        if parent_fee is None:
            self.show_error(_("Can't CPFP: unknown fee for parent transaction."))
            return
        d = WindowModalDialog(self, _('Child Pays for Parent'))
        vbox = QVBoxLayout(d)
        msg = _(
            "A CPFP is a transaction that sends an unconfirmed output back to "
            "yourself, with a high fee. The goal is to have miners confirm "
            "the parent transaction in order to get the fee attached to the "
            "child transaction.")
        vbox.addWidget(WWLabel(msg))
        msg2 = _("The proposed fee is computed using your "
            "fee/kB settings, applied to the total size of both child and "
            "parent transactions. After you broadcast a CPFP transaction, "
            "it is normal to see a new unconfirmed transaction in your history.")
        vbox.addWidget(WWLabel(msg2))
        grid = QGridLayout()
        grid.addWidget(QLabel(_('Total size') + ':'), 0, 0)
        grid.addWidget(QLabel('%d bytes' % total_size), 0, 1)
        max_fee = new_tx.output_value()
        grid.addWidget(QLabel(_('Input amount') + ':'), 1, 0)
        grid.addWidget(QLabel(self.format_amount(max_fee) + ' ' + self.base_unit()), 1, 1)
        output_amount = QLabel('')
        grid.addWidget(QLabel(_('Output amount') + ':'), 2, 0)
        grid.addWidget(output_amount, 2, 1)
        fee_e = EVRAmountEdit(self.get_decimal_point)
        combined_fee = QLabel('')
        combined_feerate = QLabel('')

        def on_fee_edit(x):
            fee_for_child = fee_e.get_amount()
            if fee_for_child is None:
                return
            out_amt = max_fee - fee_for_child
            out_amt_str = (self.format_amount(out_amt) + ' ' + self.base_unit()) if out_amt else ''
            output_amount.setText(out_amt_str)
            comb_fee = parent_fee + fee_for_child
            comb_fee_str = (self.format_amount(comb_fee) + ' ' + self.base_unit()) if comb_fee else ''
            combined_fee.setText(comb_fee_str)
            comb_feerate = comb_fee / total_size * 1000
            comb_feerate_str = self.format_fee_rate(comb_feerate) if comb_feerate else ''
            combined_feerate.setText(comb_feerate_str)

        fee_e.textChanged.connect(on_fee_edit)

        def get_child_fee_from_total_feerate(fee_per_kb: Optional[int]) -> Optional[int]:
            if fee_per_kb is None:
                return None
            fee = fee_per_kb * total_size / 1000 - parent_fee
            fee = round(fee)
            fee = min(max_fee, fee)
            fee = max(total_size, fee)  # pay at least 1 sat/byte for combined size
            return fee

        suggested_feerate = self.config.fee_per_kb()
        fee = get_child_fee_from_total_feerate(suggested_feerate)
        fee_e.setAmount(fee)
        grid.addWidget(QLabel(_('Fee for child') + ':'), 3, 0)
        grid.addWidget(fee_e, 3, 1)

        def on_rate(dyn, pos, fee_rate):
            fee = get_child_fee_from_total_feerate(fee_rate)
            fee_e.setAmount(fee)

        fee_slider = FeeSlider(self, self.config, on_rate)
        fee_combo = FeeComboBox(fee_slider)
        fee_slider.update()
        grid.addWidget(fee_slider, 4, 1)
        grid.addWidget(fee_combo, 4, 2)
        grid.addWidget(QLabel(_('Total fee') + ':'), 5, 0)
        grid.addWidget(combined_fee, 5, 1)
        grid.addWidget(QLabel(_('Total feerate') + ':'), 6, 0)
        grid.addWidget(combined_feerate, 6, 1)
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(CancelButton(d), OkButton(d)))
        if not d.exec_():
            return
        fee = fee_e.get_amount()
        if fee is None:
            return  # fee left empty, treat is as "cancel"
        if fee > max_fee:
            self.show_error(_('Max fee exceeded'))
            return
        try:
            new_tx = self.wallet.cpfp(parent_tx, fee)
        except CannotCPFP as e:
            self.show_error(str(e))
            return
        self.show_transaction(new_tx)

    def _add_info_to_tx_from_wallet_and_network(self, tx: PartialTransaction) -> bool:
        """Returns whether successful."""
        # note side-effect: tx is being mutated
        assert isinstance(tx, PartialTransaction)
        try:
            # note: this might download input utxos over network
            BlockingWaitingDialog(
                self,
                _("Adding info to tx, from wallet and network..."),
                lambda: tx.add_info_from_wallet(self.wallet, ignore_network_issues=False),
            )
        except NetworkException as e:
            self.show_error(repr(e))
            return False
        return True

    def bump_fee_dialog(self, tx: Transaction):
        txid = tx.txid()
        if not isinstance(tx, PartialTransaction):
            tx = PartialTransaction.from_tx(tx)
        if not self._add_info_to_tx_from_wallet_and_network(tx):
            return
        d = BumpFeeDialog(main_window=self, tx=tx, txid=txid)
        d.run()

    def dscancel_dialog(self, tx: Transaction):
        txid = tx.txid()
        if not isinstance(tx, PartialTransaction):
            tx = PartialTransaction.from_tx(tx)
        if not self._add_info_to_tx_from_wallet_and_network(tx):
            return
        d = DSCancelDialog(main_window=self, tx=tx, txid=txid)
        d.run()

    def save_transaction_into_wallet(self, tx: Transaction):
        win = self.top_level_window()
        try:
            if not self.wallet.adb.add_transaction(tx):
                win.show_error(_("Transaction could not be saved.") + "\n" +
                               _("It conflicts with current history."))
                return False
        except AddTransactionException as e:
            win.show_error(e)
            return False
        else:
            self.wallet.save_db()
            # need to update at least: history_list, utxo_list, address_list
            self.need_update.set()
            msg = (_("Transaction added to wallet history.") + '\n\n' +
                   _("Note: this is an offline transaction, if you want the network "
                     "to see it, you need to broadcast it."))
            win.msg_box(QPixmap(icon_path("offline_tx.png")), None, _('Success'), msg)
            return True

    def show_cert_mismatch_error(self):
        if self.showing_cert_mismatch_error:
            return
        self.showing_cert_mismatch_error = True
        self.show_critical(title=_("Certificate mismatch"),
                           msg=_(
                               "The SSL certificate provided by the main server did not match the fingerprint passed in with the --serverfingerprint option.") + "\n\n" +
                               _("Electrum will now exit."))
        self.showing_cert_mismatch_error = False
        self.close()

    def rebalance_dialog(self, chan1, chan2, amount_sat=None):
        from .rebalance_dialog import RebalanceDialog
        if chan1 is None or chan2 is None:
            return
        d = RebalanceDialog(self, chan1, chan2, amount_sat)
        d.run()
