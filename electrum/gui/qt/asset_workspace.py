import time
from abc import abstractmethod
from enum import IntEnum
from typing import Dict, List, Optional

from PyQt5.QtGui import QPixmap, QKeySequence, QIcon, QCursor, QFont, QRegExpValidator
from PyQt5.QtCore import Qt, QRect, QStringListModel, QSize, pyqtSignal, QPoint
from PyQt5.QtCore import QTimer, QRegExp
from PyQt5.QtWidgets import (QMessageBox, QComboBox, QSystemTrayIcon, QTabWidget,
                             QMenuBar, QFileDialog, QCheckBox, QLabel,
                             QVBoxLayout, QGridLayout, QLineEdit,
                             QHBoxLayout, QPushButton, QScrollArea, QTextEdit,
                             QShortcut, QMainWindow, QCompleter, QInputDialog,
                             QWidget, QSizePolicy, QStatusBar, QToolTip, QDialog,
                             QMenu, QAction, QStackedWidget, QToolButton)

from electrum import constants
from electrum.assets import is_main_asset_name_good, is_unique_asset_name_good, is_sub_asset_name_good, \
    create_transfer_asset_script, create_new_asset_script, create_owner_asset_script, create_reissue_asset_script
from electrum.gui.qt.amountedit import FreezableLineEdit
from electrum.gui.qt.util import ComplexLineEdit, HelpLabel, EnterButton, ColorScheme, ChoicesLayout, HelpButtonURL
from electrum.i18n import _
from electrum.logging import get_logger
from electrum.ravencoin import TOTAL_COIN_SUPPLY_LIMIT_IN_BTC, base_decode, address_to_script, COIN, is_address
from electrum.transaction import RavenValue, PartialTxOutput, AssetMeta
from electrum.util import Satoshis, bfh


_logger = get_logger(__name__)


class InterpretType(IntEnum):
    NO_DATA = 0
    IPFS = 1
    HEX = 2
    LATIN = 3
    

# TODO: Clean up these classes
class AssetCreateWorkspace(QWidget):
    def __init__(self, parent, create_asset_callable):
        super().__init__()

        self.parent = parent

        self.aval_owner_combo = QComboBox()
        self.aval_owner_combo.setCurrentIndex(0)
        self.aval_owner_combo.setVisible(False)

        c_grid = QGridLayout()
        c_grid.setSpacing(4)

        self.asset_name = ComplexLineEdit()
        self.asset_name.lineEdit.setMaxLength(30)
        self.asset_name.setPrefixStyle(ColorScheme.GRAY.as_stylesheet())
        self.asset_availability_text = QLabel()
        self.asset_availability_text.setAlignment(Qt.AlignCenter)

        self.divisions = FreezableLineEdit()
        self.asset_amount = FreezableLineEdit()
        self.reissuable = QCheckBox()

        self.cost_label = QLabel('Cost: {} RVN'.format(constants.net.BURN_AMOUNTS.IssueAssetBurnAmount))

        msg = _('Reissuability') + '\n\n' \
              + _('This lets the asset be edited in the future.')
        self.reissue_label = HelpLabel(_('Reissuable'), msg)

        def on_type_click(clayout_obj):
            self.asset_availability_text.setText('')
            self.divisions.setFrozen(False)
            self.asset_amount.setFrozen(False)
            i = clayout_obj.selected_index()
            i2 = self.aval_owner_combo.currentIndex()
            self.aval_owner_combo.setVisible(i != 0)

            if i == 0:
                self.cost_label.setText('Cost: {} RVN'.format(constants.net.BURN_AMOUNTS.IssueAssetBurnAmount))
            elif i == 1:
                self.cost_label.setText('Cost: {} RVN'.format(constants.net.BURN_AMOUNTS.IssueSubAssetBurnAmount))
            elif i == 2:
                self.cost_label.setText('Cost: {} RVN'.format(constants.net.BURN_AMOUNTS.IssueUniqueAssetBurnAmount))

            if i == 2:
                self.divisions.setFrozen(True)
                self.divisions.setText('0')
                self.asset_amount.setFrozen(True)
                self.asset_amount.setText('1')
                self.reissuable.setCheckState(False)
                self.reissuable.setEnabled(False)
                self.reissue_label.setStyleSheet(ColorScheme.GRAY.as_stylesheet())
            else:
                self.reissuable.setCheckState(True)
                self.reissuable.setEnabled(True)
                self.reissuable.setTristate(False)
                self.reissue_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
            if i == 0 or i2 == 0:
                self.asset_name.lineEdit.setMaxLength(30)
                self.asset_name.set_prefix('')
                return
            text = self.aval_owner_options[i2]
            self.asset_name.lineEdit.setMaxLength(30 - len(text) - 1)
            if i == 1:
                self.asset_name.set_prefix(text + '/')
            else:
                self.asset_name.set_prefix(text + '#')

        create_asset_options = ['Main', 'Sub', 'Unique']
        self.create_options_layout = ChoicesLayout('Select an asset type', create_asset_options, on_type_click,
                                                   horizontal=True)

        def on_combo_change():
            self.asset_availability_text.setText('')
            i = self.create_options_layout.selected_index()
            i2 = self.aval_owner_combo.currentIndex()
            self.aval_owner_combo.setVisible(i != 0)
            if i == 0 or i2 == 0:
                self.asset_name.set_prefix('')
                self.asset_name.lineEdit.setMaxLength(30)
                return
            text = self.aval_owner_options[i2]
            self.asset_name.lineEdit.setMaxLength(30 - len(text) - 1)
            if i == 1:
                self.asset_name.set_prefix(text + '/')
            else:
                self.asset_name.set_prefix(text + '#')

        self.aval_owner_combo.currentIndexChanged.connect(on_combo_change)

        msg = _('The asset name.') + '\n\n' \
              + _(
            'This name must be unique.')
        name_label = HelpLabel(_('Asset Name'), msg)
        c_grid.addWidget(name_label, 2, 0)
        c_grid.addWidget(self.aval_owner_combo, 2, 1)
        c_grid.addWidget(self.asset_name, 2, 2)

        self.asset_name_error_message = QLabel()
        self.asset_name_error_message.setStyleSheet(ColorScheme.RED.as_stylesheet())
        self.asset_name_error_message.setAlignment(Qt.AlignCenter)

        self.check_button = EnterButton(_("Check Availability"), self._check_availability)
        c_grid.addWidget(self.check_button, 2, 3)

        self.asset_name.lineEdit.textChanged.connect(self._check_asset_name)

        c_grid.addWidget(self.asset_name_error_message, 3, 2)
        c_grid.addWidget(self.asset_availability_text, 3, 3)

        c_grid_b = QGridLayout()
        c_grid_b.setColumnStretch(2, 1)
        c_grid_b.setHorizontalSpacing(10)

        def update_amount_line_edit():
            t = self.divisions.text()
            if not t:
                return
            split_amt = self.asset_amount.text().split('.')
            divs = int(t)
            # Update amount
            if len(split_amt) == 2:
                pre, post = split_amt
                post = post[:divs]
                if post:
                    self.asset_amount.setText(pre + '.' + post)
                else:
                    self.asset_amount.setText(pre)
            else:
                self.asset_amount.setText(split_amt[0])

            # Update regex
            if divs == 0:
                reg = QRegExp('^[1-9][0-9]{1,10}$')
            else:
                reg = QRegExp('^[0-9]{1,11}\\.([0-9]{1,' + str(divs) + '})$')
            validator = QRegExpValidator(reg)
            self.asset_amount.setValidator(validator)

        msg = _('Asset Divisions') + '\n\n' \
              + _('Asset divisions are a number from 0 to 8. They dictate how much an asset can be divided. '
                  'The minimum asset amount is 10^-d where d is the division amount. Once an asset is issued, you cannot decrease this number.')
        divisions_label = HelpLabel(_('Divisions'), msg)
        reg = QRegExp('^[012345678]{1}$')
        validator = QRegExpValidator(reg)
        self.divisions.setValidator(validator)
        self.divisions.setFixedWidth(25)
        self.divisions.setText('0')
        self.divisions.textChanged.connect(update_amount_line_edit)
        divisions_grid = QHBoxLayout()
        divisions_grid.setSpacing(0)
        divisions_grid.setContentsMargins(0, 0, 0, 0)
        divisions_grid.addWidget(divisions_label)
        divisions_grid.addWidget(self.divisions)
        divisions_w = QWidget()
        divisions_w.setLayout(divisions_grid)
        c_grid_b.addWidget(divisions_w, 0, 0)

        self.reissuable.setCheckState(True)
        self.reissuable.setTristate(False)
        reissue_grid = QHBoxLayout()
        reissue_grid.setSpacing(0)
        reissue_grid.setContentsMargins(0, 0, 0, 0)
        reissue_grid.addWidget(self.reissue_label)
        reissue_grid.addWidget(self.reissuable)
        reissue_w = QWidget()
        reissue_w.setLayout(reissue_grid)
        c_grid_b.addWidget(reissue_w, 0, 1)

        self.associated_data_info = QLabel()
        self.associated_data_info.setAlignment(Qt.AlignCenter)

        self.associated_data_interpret = InterpretType.NO_DATA

        msg = _('Associated Data') + '\n\n' \
              + _('Data to associate with this asset.')
        data_label = HelpLabel(_('Associated Data'), msg)
        self.associated_data = QLineEdit()

        self.associated_data.textChanged.connect(self._check_associated_data)

        self.associated_data_interpret_override = QComboBox()
        self.associated_data_interpret_override.addItems(['AUTO', 'IPFS', 'HEX', 'LATIN-1'])

        self.associated_data_interpret_override.currentIndexChanged.connect(self._check_associated_data)
        self.associated_data_interpret_override.setVisible(self.parent.config.get('advanced_asset_functions', False))

        data_grid = QHBoxLayout()
        data_grid.setSpacing(0)
        data_grid.setContentsMargins(0, 0, 0, 0)
        data_grid.addWidget(data_label)
        data_grid.addWidget(self.associated_data)
        data_w = QWidget()
        data_w.setLayout(data_grid)
        c_grid_b.addWidget(data_w, 0, 2)
        c_grid_b.addWidget(self.associated_data_info, 1, 2)
        c_grid_b.addWidget(self.associated_data_interpret_override, 0, 3)

        c_grid_c = QGridLayout()
        c_grid_c.setColumnStretch(4, 1)
        c_grid_c.setHorizontalSpacing(10)

        msg = _('Asset Amount') + '\n\n' \
              + _('The amount of an asset to create')
        amount_label = HelpLabel(_('Amount'), msg)
        reg = QRegExp('^[1-9][0-9]{0,10}$')
        validator = QRegExpValidator(reg)
        self.asset_amount.setValidator(validator)
        amount_grid = QHBoxLayout()
        amount_grid.setSpacing(0)
        amount_grid.setContentsMargins(0, 0, 0, 0)
        amount_grid.addWidget(amount_label)
        amount_grid.addWidget(self.asset_amount)
        amount_w = QWidget()
        amount_w.setLayout(amount_grid)
        c_grid_c.addWidget(amount_w, 0, 0)

        self.asset_amount_warning = QLabel()
        self.asset_amount_warning.setStyleSheet(ColorScheme.RED.as_stylesheet())

        self.asset_amount.textChanged.connect(self._check_amount)
        c_grid_c.addWidget(self.asset_amount_warning, 1, 0)

        self.change_addrs = None  # type: Optional[List[str]]
        self.refresh_change_addrs()

        self.send_asset_address_error = QLabel()
        self.send_asset_address_error.setStyleSheet(ColorScheme.RED.as_stylesheet())

        self.send_asset_address = QLineEdit()
        self.send_asset_address.textChanged.connect(self._check_asset_addr)

        if len(self.change_addrs) > 1:
            self.send_asset_address.setText(self.change_addrs[1])
        else:
            async def delayed_address_update():
                time.sleep(5)
                self.refresh_change_addrs()
                self.send_asset_address.setText(self.change_addrs[1])

            self.parent.run_coroutine_from_thread(delayed_address_update())

        asset_h = QHBoxLayout()
        asset_h.addWidget(QLabel(_('New asset address:')))
        asset_h.addWidget(self.send_asset_address)
        asset_h.addWidget(self.send_asset_address_error)

        self.asset_addr_w = QWidget()
        self.asset_addr_w.setLayout(asset_h)
        self.asset_addr_w.setVisible(self.parent.config.get('advanced_asset_functions', False))

        bottom_buttons = QGridLayout()
        bottom_buttons.setColumnStretch(1, 2)

        self.exec_asset_b = EnterButton(_("Create Asset"), create_asset_callable)
        bottom_buttons.addWidget(self.exec_asset_b, 1, 0)
        bottom_buttons.addWidget(self.cost_label, 1, 1)
        self.reset_create_b = EnterButton(_("Reset"), self.reset_workspace)
        bottom_buttons.addWidget(self.reset_create_b, 1, 3)

        top_layout = QHBoxLayout()
        top_layout.addLayout(self.create_options_layout.layout())
        top_layout.addWidget(HelpButtonURL("https://ravencoin.org/assets/"))

        widgetA = QWidget()
        widgetA.setLayout(top_layout)
        widgetB = QWidget()
        widgetB.setLayout(c_grid)
        widgetC = QWidget()
        widgetC.setLayout(c_grid_b)
        widgetD = QWidget()
        widgetD.setLayout(c_grid_c)
        widgetF = QWidget()
        widgetF.setLayout(bottom_buttons)

        create_l = QVBoxLayout()
        create_l.addWidget(widgetA)
        create_l.addWidget(widgetB)
        create_l.addWidget(widgetC)
        create_l.addWidget(widgetD)
        create_l.addWidget(self.asset_addr_w)
        create_l.addWidget(widgetF)
        self.setLayout(create_l)

        self.aval_owner_options = []  # type: List[str]
        self.last_checked = None  # type: Optional[str]

    def _check_asset_addr(self):
        addr = self.send_asset_address.text()
        if not is_address(addr):
            self.send_asset_address_error.setText(_('Invalid Ravencoin Address'))
            return False
        else:
            self.send_asset_address_error.setText('')
            return True

    def _check_asset_name(self):
        self.asset_availability_text.setText('')
        name = self.asset_name.text()
        if not name:
            self.asset_name_error_message.setText('')
            return
        pre = self.asset_name.get_prefix()
        i = self.create_options_layout.selected_index()
        if i == 0:
            error = is_main_asset_name_good(name)
            if error == 'SIZE':
                if len(name) < 3:
                    error = None
                else:
                    error = "Main assets may only use capital letters, numbers, '_', and '.'"
        elif i == 1:
            error = is_sub_asset_name_good(name)
        else:
            error = is_unique_asset_name_good(name)
        if len(pre + name) > 30:
            error = 'Asset name must be less than 31 characters (Including the parent).'
        if error:
            self.asset_name_error_message.setText(error)
            return False
        else:
            self.asset_name_error_message.setText('')
            return True

    def _check_availability(self):
        asset = self.asset_name.get_prefix() + self.asset_name.text()
        if self.create_options_layout.selected_index() == 0:
            if len(asset) < 3:
                self.asset_name_error_message.setText('Main assets must be more than 3 characters.')
                return
        elif self.aval_owner_combo.currentIndex() == 0:
            self.asset_name_error_message.setText('Please select a parent asset!')
            return
        if not self._check_asset_name():
            return
        self.check_asset_availability(asset)

    def _check_associated_data(self) -> bool:
        text = self.associated_data.text()
        i = self.associated_data_interpret_override.currentIndex()
        if len(text) == 0:
            self.associated_data_info.setText('')
            self.associated_data_interpret = InterpretType.NO_DATA
            return True
        if i != 0:
            if i == 1:
                self.associated_data_interpret = InterpretType.IPFS
                try:
                    if len(base_decode(text, base=58)) > 34:
                        self.associated_data_info.setText('Too much data in IPFS hash!')
                        self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                        return False
                    else:
                        self.associated_data_info.setText('Reading as IPFS')
                        self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                        return True
                except:
                    self.associated_data_info.setText('Invalid base 58 encoding!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
            if i == 2:
                self.associated_data_interpret = InterpretType.HEX
                try:
                    bfh(text)
                except:
                    self.associated_data_info.setText('Not a valid hex string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                if len(text) > 34 * 2:
                    self.associated_data_info.setText('Too much data in hex string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                else:
                    self.associated_data_info.setText('Reading as hex string')
                    self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    return True

            else:
                self.associated_data_interpret = InterpretType.LATIN
                try:
                    text.encode('latin-1')
                except:
                    self.associated_data_info.setText('Text not compatible with latin-1!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                if len(text) > 34:
                    self.associated_data_info.setText('Too much data in latin-1 string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                else:
                    self.associated_data_info.setText('Reading as latin-1 string')
                    self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    return True
        if self.parent.config.get('advanced_asset_functions', False):
            if text[:2] == 'Qm':
                try:
                    if len(base_decode(text, base=58)) == 34:
                        self.associated_data_info.setText('Reading as IPFS')
                        self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                        self.associated_data_interpret = InterpretType.IPFS
                        return True
                except:
                    pass
            try:
                if len(text) == 1:
                    raise Exception()
                bytes.fromhex(text)
                self.associated_data_info.setText('Reading as hex string')
                self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                self.associated_data_interpret = InterpretType.HEX
                if len(text) > 34 * 2:
                    self.associated_data_info.setText('Too much data in hex string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                return True
            except:
                self.associated_data_info.setText('Reading as latin-1 string')
                self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                self.associated_data_interpret = InterpretType.LATIN
                try:
                    text.encode('latin-1')
                except:
                    self.associated_data_info.setText('Text not compatible with latin-1!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                if len(text) > 34:
                    self.associated_data_info.setText('Too much data in latin-1 string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                return True
        else:
            self.associated_data_interpret = InterpretType.IPFS
            try:
                raw = base_decode(text, base=58)
                if len(raw) > 34:
                    self.associated_data_info.setText('Too much data in IPFS hash!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                elif len(raw) < 34:
                    self.associated_data_info.setText('Too little data in IPFS hash!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                else:
                    self.associated_data_info.setText('Reading as IPFS')
                    self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    return True
            except:
                self.associated_data_info.setText('Invalid IPFS hash!')
                self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                return False

    def _check_amount(self) -> bool:
        t = self.asset_amount.text()
        if not t:
            self.asset_amount_warning.setText('')
            return False
        v = float(t)
        if v > TOTAL_COIN_SUPPLY_LIMIT_IN_BTC:
            self.asset_amount_warning.setText(
                _('More than the maximum amount ({})').format(TOTAL_COIN_SUPPLY_LIMIT_IN_BTC))
            return False
        elif v == 0:
            self.asset_amount_warning.setText(
                _('The amount cannot be 0.')
            )
            return False
        else:
            self.asset_amount_warning.setText('')
            return True

    def check_asset_availability(self, asset):
        def x(result):
            self.update_screen_based_on_asset_result(asset, result)

        self.parent.run_coroutine_from_thread(self.parent.network.get_meta_for_asset(asset),
                                              x)

    def update_screen_based_on_asset_result(self, asset, result):
        if result:
            self.last_checked = None
            self.asset_availability_text.setText('Asset Unavailable')
            self.asset_availability_text.setStyleSheet(ColorScheme.RED.as_stylesheet())
        else:
            self.last_checked = asset
            self.asset_availability_text.setText('Asset Available')
            self.asset_availability_text.setStyleSheet(ColorScheme.GREEN.as_stylesheet())

    def refresh_owners(self):
        confirmed, unconfirmed, _ = self.parent.wallet.get_balance()
        owned_assets = confirmed.assets
        in_mempool = unconfirmed.assets
        owners = [n for n in owned_assets.keys() if
                  n[-1] == '!' and owned_assets.get(n, 0) != 0]
        indexes_in_mempool = set()
        new_aval_owner_options = ['Select a parent'] + \
                                  sorted([n[:-1] for n in owners])
        for i in range(len(new_aval_owner_options)):
            if i == 0:
                continue
            a = new_aval_owner_options[i]
            if (a + '!') in in_mempool:
                indexes_in_mempool.add(i)
                new_aval_owner_options[i] = a + ' (Mempool)'

        diff = set(new_aval_owner_options) - set(self.aval_owner_options)

        if self.aval_owner_options and not diff:
            return

        self.aval_owner_options = new_aval_owner_options
        self.aval_owner_combo.clear()
        self.aval_owner_combo.addItems(self.aval_owner_options)
        for i in indexes_in_mempool:
            self.aval_owner_combo.model().item(i).setEnabled(False)

    def refresh_change_addrs(self):
        # We just want addresses to send the newly created assets to
        # Is there a way to improve this?
        addrs = self.parent.wallet.get_change_addresses_for_new_transaction(extra_addresses=3)
        if not addrs:
            addrs = self.parent.wallet.get_change_addresses_for_new_transaction(allow_reusing_used_change_addrs=True, extra_addresses=3)
        if not addrs:
            addrs = self.parent.wallet.get_change_addresses(slice_stop=4)
        if not addrs:
            addrs = self.parent.wallet.get_receiving_addresses(slice_stop=4)
        if len(addrs) < 4:
            assert len(addrs) > 0
            addr = addrs[0]
            for _ in range(4 - len(addrs)):
                addrs.append(addr)
        self.change_addrs = addrs

    def verify_valid(self) -> Optional[str]:
        asset = self.asset_name.get_prefix() + self.asset_name.text()
        if asset != self.last_checked:
            self.asset_availability_text.setText('Check if available')
            self.asset_availability_text.setStyleSheet(ColorScheme.RED.as_stylesheet())
            return 'Check if your asset is available first'
        if not self._check_amount():
            return 'Invalid amount'
        if not self._check_asset_addr():
            return 'Invalid address'
        if self.create_options_layout.selected_index() == 0:
            if len(asset) < 3:
                self.asset_name_error_message.setText('Main assets must be more than 3 characters.')
                return 'Check name'
        elif self.aval_owner_combo.currentIndex() == 0:
            self.asset_name_error_message.setText('Please select a parent asset!')
            return 'No parent asset'
        if not self._check_asset_name():
            return 'Check name'
        if not self._check_associated_data():
            return 'Invalid associated data'
        return None

    def should_warn_associated_data(self):
        text = self.associated_data.text()  # type: str
        i = self.associated_data_interpret
        if i == InterpretType.NO_DATA:
            return False
        elif i == InterpretType.IPFS:
            b = base_decode(text, base=58)
        elif i == InterpretType.HEX:
            b = bfh(text)
        else:
            b = text.encode('latin-1')
        if len(b) < 34 and self.parent.config.get('warn_asset_small_associated', True):
            return True
        return False

    def should_warn_on_non_reissuable(self):
        is_unique = self.create_options_layout.selected_index() == 2
        c = self.reissuable.isChecked()
        if not c and not is_unique and self.parent.config.get('warn_asset_non_reissuable', True):
            return True
        return False

    def reset_workspace(self):
        self.create_options_layout.group.buttons()[0].setChecked(True)
        self.asset_name.lineEdit.setText('')
        self.asset_name.lineEdit.setMaxLength(30)
        self.asset_name.set_prefix('')
        self.divisions.setFrozen(False)
        self.divisions.setText('0')
        self.reissuable.setCheckState(True)
        self.reissuable.setEnabled(True)
        self.reissuable.setTristate(False)
        self.aval_owner_combo.setVisible(False)
        self.asset_name_error_message.setText('')
        self.asset_availability_text.setText('')
        self.associated_data_info.setText('')
        self.asset_amount_warning.setText('')
        self.associated_data.setText('')
        self.asset_amount.setText('')
        reg = QRegExp('^[1-9][0-9]{0,10}$')
        validator = QRegExpValidator(reg)
        self.asset_amount.setValidator(validator)
        self.reissue_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
        self.last_checked = None
        self.associated_data_interpret = InterpretType.NO_DATA
        self.associated_data_interpret_override.setCurrentIndex(0)
        self.refresh_change_addrs()
        self.send_asset_address.setText(self.change_addrs[1])
        self.send_asset_address_error.setText('')
        self.cost_label.setText('Cost: {} RVN'.format(constants.net.BURN_AMOUNTS.IssueAssetBurnAmount))
        self.refresh_owners()

    def get_owner(self):
        i = self.aval_owner_combo.currentIndex()
        if i == 0:
            return None
        return self.aval_owner_options[i] + '!'

    def get_output(self):
        i = self.create_options_layout.selected_index()
        if i == 0:
            addr = constants.net.BURN_ADDRESSES.IssueAssetBurnAddress
            amt = constants.net.BURN_AMOUNTS.IssueAssetBurnAmount
        elif i == 1:
            addr = constants.net.BURN_ADDRESSES.IssueSubAssetBurnAddress
            amt = constants.net.BURN_AMOUNTS.IssueSubAssetBurnAmount
        elif i == 2:
            addr = constants.net.BURN_ADDRESSES.IssueUniqueAssetBurnAddress
            amt = constants.net.BURN_AMOUNTS.IssueUniqueAssetBurnAmount
        else:
            NotImplementedError()
        burn = PartialTxOutput(
            scriptpubkey=bfh(address_to_script(addr)),
            value=Satoshis(amt * COIN)
        )
        norm = [burn]
        o = self.get_owner()
        if o:
            script = bfh(address_to_script(self.change_addrs[0]))
            norm.append(PartialTxOutput(
                scriptpubkey=create_transfer_asset_script(script, o, COIN),
                value=Satoshis(COIN),
                asset=o
            ))

        asset = self.asset_name.get_prefix() + self.asset_name.text()
        is_unique = self.create_options_layout.selected_index() == 2
        amt = int(float(self.asset_amount.text()) * COIN)
        d = self.associated_data.text()  # type: str

        i = self.associated_data_interpret
        if i == InterpretType.NO_DATA:
            data = None
        else:
            if i == InterpretType.IPFS:
                data = base_decode(d, base=58)
            elif i == InterpretType.HEX:
                data = bfh(d)
            else:
                data = d.encode('latin-1')
            data = data.rjust(34, b'\0')

        new = [
            PartialTxOutput(
                scriptpubkey=create_new_asset_script(bfh(address_to_script(self.send_asset_address.text())),
                                                     asset,
                                                     amt,
                                                     int(self.divisions.text()),
                                                     self.reissuable.isChecked(),
                                                     data),
                value=Satoshis(amt),
                asset=asset)
        ]

        if not is_unique:
            new.append(
                PartialTxOutput(
                    scriptpubkey=create_owner_asset_script(bfh(address_to_script(self.send_asset_address.text())),
                                                           asset + '!'),
                    value=Satoshis(COIN),
                    asset=asset + '!'
                )
            )

        return norm, new, self.change_addrs[2]


class AssetReissueWorkspace(QWidget):
    def __init__(self, parent, reissue_asset_callable):
        super().__init__()

        self.parent = parent

        self.current_asset_meta = None

        self.aval_owner_combo = QComboBox()
        self.aval_owner_combo.setCurrentIndex(0)

        self.divisions = FreezableLineEdit()
        self.asset_amount = FreezableLineEdit()
        self.reissuable = QCheckBox()
        self.associated_data = FreezableLineEdit()
        self.current_sats = QLabel('')

        self.associated_data_info = QLabel()
        self.associated_data_info.setAlignment(Qt.AlignCenter)
        self.associated_data_interpret_override = QComboBox()
        self.associated_data_interpret = InterpretType.NO_DATA

        self.cost_label = QLabel('Cost: {} RVN'.format(constants.net.BURN_AMOUNTS.ReissueAssetBurnAmount))

        msg = _('Reissuability') + '\n\n' \
              + _('This lets the asset be edited in the future.')
        self.reissue_label = HelpLabel(_('Reissuable'), msg)

        def on_combo_change():
            i = self.aval_owner_combo.currentIndex()
            if i == 0:
                self.reset_gui()
            else:
                asset = self.aval_owner_options[i]
                m = self.current_asset_meta = self.parent.wallet.get_asset_meta(asset)  # type: AssetMeta

                if not m:
                    # Edge case where we have the ownership asset, but not the normal asset
                    async def async_data_get():
                        # We will trust what the server sends us, since this is just used for GUI and locking out
                        # invalid options which would be caught in a node broadcast
                        m = await self.parent.network.get_meta_for_asset(asset)
                        if not m:
                            # Dummy data
                            _logger.warning("Couldn't query asset meta!")
                            divs = 0
                            reis = True
                            data = None
                            circulation = 0
                        else:
                            divs = m['divisions']
                            reis = False if m['reissuable'] == 0 else True
                            data = m.get('ipfs', None)
                            circulation = m['sats_in_circulation']
                        self.current_asset_meta = AssetMeta(asset, circulation, False, reis, divs, bool(data), data, -1, '', None, None)

                        r = reis

                        d = divs
                        if d < 8:
                            reg_base = '012345678'
                            reg = QRegExp('^[' + reg_base[d:] + ']{1}$')
                            validator = QRegExpValidator(reg)
                            self.divisions.setValidator(validator)
                            self.divisions.setFrozen(not r)

                        self.divisions.setText(str(d))

                        self.reissuable.setCheckState(r)
                        if r:
                            self.reissuable.setEnabled(r)
                            self.reissuable.setTristate(False)

                        i = data
                        if i:
                            self.associated_data.setFrozen(not r)
                            self.associated_data.setText(i)
                            self.associated_data_interpret_override.setCurrentIndex(0)
                            self._check_associated_data()
                        else:
                            self.associated_data.setFrozen(not r)
                            self.associated_data.setText('')
                            self._check_associated_data()

                        self.asset_amount.setFrozen(not r)
                        self.asset_amount.setText('0')
                        self.current_sats.setText(
                            _("({} {} currently in circulation)").format(Satoshis(circulation), asset))

                        if d == 0:
                            reg = QRegExp('^[0-9]{1,11}$')
                        else:
                            reg = QRegExp('^[0-9]{1,11}\\.([0-9]{1,' + str(d) + '})$')
                        validator = QRegExpValidator(reg)
                        self.asset_amount.setValidator(validator)

                        if r:
                            self.reissue_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                            self.divisions_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                            self.data_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                            self.amount_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())

                        self.exec_asset_b.setEnabled(r)

                    self.parent.run_coroutine_from_thread(async_data_get())
                    return

                r = m.is_reissuable

                d = m.divisions
                if d < 8:
                    reg_base = '012345678'
                    reg = QRegExp('^[' + reg_base[d:] + ']{1}$')
                    validator = QRegExpValidator(reg)
                    self.divisions.setValidator(validator)
                    self.divisions.setFrozen(not r)
                else:
                    self.divisions.setFrozen(True)

                self.divisions.setText(str(d))

                self.reissuable.setCheckState(r)
                if r:
                    self.reissuable.setEnabled(r)
                    self.reissuable.setTristate(False)

                i = m.ipfs_str
                if i:
                    self.associated_data.setFrozen(not r)
                    self.associated_data.setText(m.ipfs_str)
                    self.associated_data_interpret_override.setCurrentIndex(0)
                    self._check_associated_data()
                else:
                    self.associated_data.setFrozen(not r)
                    self.associated_data.setText('')
                    self._check_associated_data()

                self.asset_amount.setFrozen(not r)
                self.asset_amount.setText('0')
                self.current_sats.setText(_("({} {} currently in circulation)").format(Satoshis(m.circulation), m.name))

                if d == 0:
                    reg = QRegExp('^[0-9]{1,11}$')
                else:
                    reg = QRegExp('^[0-9]{1,11}\\.([0-9]{1,' + str(d) + '})$')
                validator = QRegExpValidator(reg)
                self.asset_amount.setValidator(validator)

                if r:
                    self.reissue_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    self.divisions_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    self.data_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    self.amount_label.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())

        self.aval_owner_combo.currentIndexChanged.connect(on_combo_change)

        c_grid_b = QGridLayout()
        c_grid_b.setColumnStretch(2, 1)
        c_grid_b.setHorizontalSpacing(10)

        msg = _('Asset Divisions') + '\n\n' \
              + _('Asset divisions are a number from 0 to 8. They dictate how much an asset can be divided. '
                  'The minimum asset amount is 10^-d where d is the division amount. Once an asset is issued, you cannot decrease this number.')
        self.divisions_label = HelpLabel(_('Divisions'), msg)
        self.divisions.setText('')
        self.divisions.setFixedWidth(25)
        self.divisions.setFrozen(True)

        divisions_grid = QHBoxLayout()
        divisions_grid.setSpacing(0)
        divisions_grid.setContentsMargins(0, 0, 0, 0)
        divisions_grid.addWidget(self.divisions_label)
        divisions_grid.addWidget(self.divisions)

        divisions_w = QWidget()
        divisions_w.setLayout(divisions_grid)
        c_grid_b.addWidget(divisions_w, 0, 0)

        self.reissuable.setCheckState(True)
        self.reissuable.setEnabled(False)
        reissue_grid = QHBoxLayout()
        reissue_grid.setSpacing(0)
        reissue_grid.setContentsMargins(0, 0, 0, 0)
        reissue_grid.addWidget(self.reissue_label)
        reissue_grid.addWidget(self.reissuable)
        reissue_w = QWidget()
        reissue_w.setLayout(reissue_grid)
        c_grid_b.addWidget(reissue_w, 0, 1)

        msg = _('Associated Data') + '\n\n' \
              + _('Data to associate with this asset.')
        self.data_label = HelpLabel(_('Associated Data'), msg)
        self.associated_data.setFrozen(True)

        self.associated_data.textChanged.connect(self._check_associated_data)

        self.associated_data_interpret_override.addItems(['AUTO', 'IPFS', 'HEX', 'LATIN-1'])

        self.associated_data_interpret_override.currentIndexChanged.connect(self._check_associated_data)
        self.associated_data_interpret_override.setVisible(self.parent.config.get('advanced_asset_functions', False))

        data_grid = QHBoxLayout()
        data_grid.setSpacing(0)
        data_grid.setContentsMargins(0, 0, 0, 0)
        data_grid.addWidget(self.data_label)
        data_grid.addWidget(self.associated_data)
        data_w = QWidget()
        data_w.setLayout(data_grid)
        c_grid_b.addWidget(data_w, 0, 2)
        c_grid_b.addWidget(self.associated_data_info, 1, 2)
        c_grid_b.addWidget(self.associated_data_interpret_override, 0, 3)

        c_grid_c = QGridLayout()
        c_grid_c.setColumnStretch(4, 1)
        c_grid_c.setHorizontalSpacing(10)

        msg = _('Amount to Add') + '\n\n' \
              + _('The amount of an asset to add to circulation')
        self.amount_label = HelpLabel(_('Additional Amount'), msg)
        amount_grid = QHBoxLayout()
        amount_grid.setSpacing(0)
        amount_grid.setContentsMargins(0, 0, 0, 0)
        amount_grid.addWidget(self.amount_label)
        amount_grid.addWidget(self.asset_amount)
        amount_grid.addWidget(self.current_sats)
        amount_w = QWidget()
        amount_w.setLayout(amount_grid)
        c_grid_c.addWidget(amount_w, 0, 0)

        self.asset_amount_warning = QLabel()
        self.asset_amount_warning.setStyleSheet(ColorScheme.RED.as_stylesheet())

        self.asset_amount.textChanged.connect(self._check_amount)
        c_grid_c.addWidget(self.asset_amount_warning, 1, 0)

        self.change_addrs = None  # type: Optional[List[str]]
        self.refresh_change_addrs()

        self.send_asset_address_error = QLabel()
        self.send_asset_address_error.setStyleSheet(ColorScheme.RED.as_stylesheet())

        self.send_asset_address = QLineEdit()
        self.send_asset_address.textChanged.connect(self._check_asset_addr)
        #self.send_asset_address.setText(self.change_addrs[1])

        if len(self.change_addrs) > 1:
            self.send_asset_address.setText(self.change_addrs[1])
        else:
            async def delayed_address_update():
                time.sleep(5)
                self.refresh_change_addrs()
                self.send_asset_address.setText(self.change_addrs[1])

            self.parent.run_coroutine_from_thread(delayed_address_update())

        asset_h = QHBoxLayout()
        asset_h.addWidget(QLabel(_('New asset address:')))
        asset_h.addWidget(self.send_asset_address)
        asset_h.addWidget(self.send_asset_address_error)

        self.asset_addr_w = QWidget()
        self.asset_addr_w.setLayout(asset_h)
        self.asset_addr_w.setVisible(self.parent.config.get('advanced_asset_functions', False))

        bottom_buttons = QGridLayout()
        bottom_buttons.setColumnStretch(1, 2)

        self.exec_asset_b = EnterButton(_("Reissue Asset"), reissue_asset_callable)
        bottom_buttons.addWidget(self.exec_asset_b, 1, 0)
        bottom_buttons.addWidget(self.cost_label, 1, 1)

        def hard_reset():
            self.reset_workspace()
        self.reset_create_b = EnterButton(_("Reset"), hard_reset)
        bottom_buttons.addWidget(self.reset_create_b, 1, 3)

        top_layout = QHBoxLayout()
        top_layout.addWidget(self.aval_owner_combo)
        top_layout.addWidget(HelpButtonURL("https://ravencoin.org/assets/"))
        widgetA = QWidget()
        widgetA.setLayout(top_layout)
        widgetC = QWidget()
        widgetC.setLayout(c_grid_b)
        widgetD = QWidget()
        widgetD.setLayout(c_grid_c)
        widgetF = QWidget()
        widgetF.setLayout(bottom_buttons)
        create_l = QVBoxLayout()
        create_l.addWidget(widgetA)
        create_l.addWidget(widgetC)
        create_l.addWidget(widgetD)
        create_l.addWidget(self.asset_addr_w)
        create_l.addWidget(widgetF)
        self.setLayout(create_l)

        self.aval_owner_options = []  # type: List[str]

    def _check_asset_addr(self):
        addr = self.send_asset_address.text()
        if not is_address(addr):
            self.send_asset_address_error.setText(_('Invalid Ravencoin Address'))
            return False
        else:
            self.send_asset_address_error.setText('')
            return True

    def _check_associated_data(self) -> bool:
        text = self.associated_data.text()
        i = self.associated_data_interpret_override.currentIndex()
        if len(text) == 0:
            self.associated_data_info.setText('')
            self.associated_data_interpret = InterpretType.NO_DATA
            return True
        if i != 0:
            if i == 1:
                self.associated_data_interpret = InterpretType.IPFS
                try:
                    if len(base_decode(text, base=58)) > 34:
                        self.associated_data_info.setText('Too much data in IPFS hash!')
                        self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                        return False
                    else:
                        self.associated_data_info.setText('Reading as IPFS')
                        self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                        return True
                except:
                    self.associated_data_info.setText('Invalid base 58 encoding!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
            if i == 2:
                self.associated_data_interpret = InterpretType.HEX
                try:
                    bfh(text)
                except:
                    self.associated_data_info.setText('Not a valid hex string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                if len(text) > 34 * 2:
                    self.associated_data_info.setText('Too much data in hex string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                else:
                    self.associated_data_info.setText('Reading as hex string')
                    self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    return True

            else:
                self.associated_data_interpret = InterpretType.LATIN
                try:
                    text.encode('latin-1')
                except:
                    self.associated_data_info.setText('Text not compatible with latin-1!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                if len(text) > 34:
                    self.associated_data_info.setText('Too much data in latin-1 string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                else:
                    self.associated_data_info.setText('Reading as latin-1 string')
                    self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    return True
        if self.parent.config.get('advanced_asset_functions', False):
            if text[:2] == 'Qm':
                try:
                    if len(base_decode(text, base=58)) == 34:
                        self.associated_data_info.setText('Reading as IPFS')
                        self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                        self.associated_data_interpret = InterpretType.IPFS
                        return True
                except:
                    pass
            try:
                if len(text) == 1:
                    raise Exception()
                bytes.fromhex(text)
                self.associated_data_info.setText('Reading as hex string')
                self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                self.associated_data_interpret = InterpretType.HEX
                if len(text) > 34 * 2:
                    self.associated_data_info.setText('Too much data in hex string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                return True
            except:
                self.associated_data_info.setText('Reading as latin-1 string')
                self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                self.associated_data_interpret = InterpretType.LATIN
                try:
                    text.encode('latin-1')
                except:
                    self.associated_data_info.setText('Text not compatible with latin-1!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                if len(text) > 34:
                    self.associated_data_info.setText('Too much data in latin-1 string!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                return True
        else:
            self.associated_data_interpret = InterpretType.IPFS
            try:
                raw = base_decode(text, base=58)
                if len(raw) > 34:
                    self.associated_data_info.setText('Too much data in IPFS hash!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                elif len(raw) < 34:
                    self.associated_data_info.setText('Too little data in IPFS hash!')
                    self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                    return False
                else:
                    self.associated_data_info.setText('Reading as IPFS')
                    self.associated_data_info.setStyleSheet(ColorScheme.DEFAULT.as_stylesheet())
                    return True
            except:
                self.associated_data_info.setText('Invalid IPFS hash!')
                self.associated_data_info.setStyleSheet(ColorScheme.RED.as_stylesheet())
                return False

    def _check_amount(self) -> bool:
        t = self.asset_amount.text()
        if not t:
            self.asset_amount_warning.setText('')
            return False
        v = float(t) + self.current_asset_meta.circulation / 100_000_000
        if v > TOTAL_COIN_SUPPLY_LIMIT_IN_BTC:
            self.asset_amount_warning.setText(
                _('More than the maximum amount ({})').format(TOTAL_COIN_SUPPLY_LIMIT_IN_BTC))
            return False
        else:
            self.asset_amount_warning.setText('')
            return True

    def refresh_owners(self):
        confirmed, unconfirmed, _ = self.parent.wallet.get_balance()
        owned_assets = confirmed.assets
        in_mempool = unconfirmed.assets

        owners = [n for n in owned_assets.keys() if
                  n[-1] == '!' and owned_assets.get(n, 0) != 0]

        new_aval_owner_options = ['Select an asset'] + \
                                  sorted([n[:-1] for n in owners])
        disabled_indexes = set()
        for i in range(len(new_aval_owner_options)):
            if i == 0:
                continue
            asset = new_aval_owner_options[i]
            meta = self.parent.wallet.get_asset_meta(asset)  # type: AssetMeta
            if not meta:
                continue
            else:
                if not meta.is_reissuable:
                    disabled_indexes.add(i)
                    new_aval_owner_options[i] = asset + ' (Non-reissuable)'
                if (asset + '!') in in_mempool:
                    disabled_indexes.add(i)
                    new_aval_owner_options[i] = asset + ' (Mempool)'

        diff = set(new_aval_owner_options) - set(self.aval_owner_options)

        if self.aval_owner_options and not diff:
            return

        self.aval_owner_options = new_aval_owner_options
        self.aval_owner_combo.clear()

        self.aval_owner_combo.addItems(self.aval_owner_options)
        for i in disabled_indexes:
            self.aval_owner_combo.model().item(i).setEnabled(False)

    def refresh_change_addrs(self):
        # We just want addresses to send the newly created assets to
        # Is there a way to improve this?
        addrs = self.parent.wallet.get_change_addresses_for_new_transaction(extra_addresses=3)
        if not addrs:
            addrs = self.parent.wallet.get_change_addresses_for_new_transaction(allow_reusing_used_change_addrs=True, extra_addresses=3)
        if not addrs:
            addrs = self.parent.wallet.get_change_addresses(slice_stop=4)
        if not addrs:
            addrs = self.parent.wallet.get_receiving_addresses(slice_stop=4)
        if len(addrs) < 4:
            assert len(addrs) > 0
            addr = addrs[0]
            for _ in range(4 - len(addrs)):
                addrs.append(addr)
        self.change_addrs = addrs

    def verify_valid(self) -> Optional[str]:
        if not self._check_amount():
            return 'Invalid amount'
        if not self._check_asset_addr():
            return 'Invalid address'
        if not self._check_associated_data():
            return 'Invalid associated data'
        return None

    def should_warn_associated_data(self):
        text = self.associated_data.text()  # type: str
        i = self.associated_data_interpret
        if i == InterpretType.NO_DATA:
            return False
        elif i == InterpretType.IPFS:
            b = base_decode(text, base=58)
        elif i == InterpretType.HEX:
            b = bfh(text)
        else:
            b = text.encode('latin-1')
        if len(b) < 34 and self.parent.config.get('warn_asset_small_associated', True):
            return True
        return False

    def should_warn_on_non_reissuable(self):
        c = self.reissuable.isChecked()
        if not c and self.parent.config.get('warn_asset_non_reissuable', True):
            return True
        return False

    def reset_gui(self):
        self.aval_owner_combo.setCurrentIndex(0)

        self.current_asset_meta = None

        self.divisions.setFrozen(True)
        self.divisions.setText('')

        self.reissuable.setEnabled(False)
        self.reissuable.setCheckState(False)

        self.associated_data.setFrozen(True)
        self.associated_data.setText('')
        self.associated_data_info.setText('')
        self.associated_data_interpret = InterpretType.NO_DATA
        self.associated_data_interpret_override.setCurrentIndex(0)

        self.asset_amount.setFrozen(True)
        self.asset_amount.setText('')
        self.current_sats.setText('')
        self.asset_amount_warning.setText('')

        self.reissue_label.setStyleSheet(ColorScheme.GRAY.as_stylesheet())
        self.divisions_label.setStyleSheet(ColorScheme.GRAY.as_stylesheet())
        self.data_label.setStyleSheet(ColorScheme.GRAY.as_stylesheet())
        self.amount_label.setStyleSheet(ColorScheme.GRAY.as_stylesheet())

    def reset_workspace(self):
        self.reset_gui()
        self.refresh_change_addrs()
        self.refresh_owners()
        self.send_asset_address.setText(self.change_addrs[1])

    def get_owner(self):
        i = self.aval_owner_combo.currentIndex()
        if i == 0:
            return None
        return self.aval_owner_options[i] + '!'

    def get_output(self):
        burn = PartialTxOutput(
            scriptpubkey=bfh(address_to_script(
                constants.net.BURN_ADDRESSES.ReissueAssetBurnAddress
            )),
            value=Satoshis(constants.net.BURN_AMOUNTS.ReissueAssetBurnAmount * COIN)
        )
        o = self.get_owner()
        script = bfh(address_to_script(self.change_addrs[0]))
        ownr = PartialTxOutput(
            scriptpubkey=create_transfer_asset_script(script, o, COIN),
            value=Satoshis(COIN),
            asset=o
        )
        norm = [burn, ownr]

        asset = o[:-1]
        amt = int(float(self.asset_amount.text()) * COIN)
        d = self.associated_data.text()  # type: str

        i = self.associated_data_interpret
        if i == InterpretType.NO_DATA:
            data = None
        else:
            if i == InterpretType.IPFS:
                data = base_decode(d, base=58)
            elif i == InterpretType.HEX:
                data = bfh(d)
            else:
                data = d.encode('latin-1')
            data = data.rjust(34, b'\0')

        divs = int(self.divisions.text())
        new = [
            PartialTxOutput(
                scriptpubkey=create_reissue_asset_script(bfh(address_to_script(self.send_asset_address.text())),
                                                     asset,
                                                     amt,
                                                     bytes([divs]) if divs != self.current_asset_meta.divisions else b'\xff',
                                                     self.reissuable.isChecked(),
                                                     data),
                value=Satoshis(amt),
                asset=asset)
        ]
        return norm, new, self.change_addrs[2]
