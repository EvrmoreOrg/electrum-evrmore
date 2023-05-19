# Copyright (C) 2022 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

import asyncio
from decimal import Decimal
from typing import Optional, TYPE_CHECKING, Sequence, List, Dict
from urllib.parse import urlparse

from PyQt5.QtCore import pyqtSignal, QPoint, QRegExp
from PyQt5.QtGui import QRegExpValidator
from PyQt5.QtWidgets import (QLabel, QVBoxLayout, QGridLayout,
                             QHBoxLayout, QCompleter, QWidget, QToolTip,
                             QComboBox)

from electrum import util, paymentrequest
from electrum import lnutil
from electrum.plugin import run_hook
from electrum.i18n import _
from electrum.evrmore import COIN, make_op_return
from electrum.util import (AssetAmountModified, UserFacingException, get_asyncio_loop, bh2u,
                           InvalidBitcoinURI, maybe_extract_lightning_payment_identifier, NotEnoughFunds,
                           NoDynamicFeeEstimates, InvoiceError, parse_max_spend, EvrmoreValue)
from electrum.invoices import PR_PAID, Invoice
from electrum.transaction import Transaction, PartialTxInput, PartialTransaction, PartialTxOutput
from electrum.network import TxBroadcastError, BestEffortRequestFailed
from electrum.logging import Logger
from electrum.lnaddr import lndecode, LnInvoiceException
from electrum.lnurl import decode_lnurl, request_lnurl, callback_lnurl, LNURLError, LNURL6Data

from .amountedit import AmountEdit, PayToAmountEdit, SizedFreezableLineEdit
from .util import WaitingDialog, HelpLabel, MessageBoxMixin, EnterButton
from .confirm_tx_dialog import ConfirmTxDialog
from .transaction_dialog import PreviewTxDialog

if TYPE_CHECKING:
    from .main_window import ElectrumWindow


class SendTab(QWidget, MessageBoxMixin, Logger):

    payment_request_ok_signal = pyqtSignal()
    payment_request_error_signal = pyqtSignal()
    lnurl6_round1_signal = pyqtSignal(object, object)
    lnurl6_round2_signal = pyqtSignal(object)
    clear_send_tab_signal = pyqtSignal()
    show_error_signal = pyqtSignal(str)

    payment_request: Optional[paymentrequest.PaymentRequest]
    _lnurl_data: Optional[LNURL6Data] = None

    def __init__(self, window: 'ElectrumWindow'):
        QWidget.__init__(self, window)
        Logger.__init__(self)

        self.window = window
        self.wallet = window.wallet
        self.fx = window.fx
        self.config = window.config
        self.network = window.network

        self.format_amount_and_units = window.format_amount_and_units
        self.format_amount = window.format_amount
        self.base_unit = window.base_unit

        self.payto_URI = None
        self.payment_request = None  # type: Optional[paymentrequest.PaymentRequest]
        self.pending_invoice = None
        self.notify_asset_amounts_have_changed = False

        # A 4-column grid layout.  All the stretch is in the last column.
        # The exchange rate plugin adds a fiat widget in column 2
        self.send_grid = grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(3, 1)

        # Let user choose to send EVR or Asset
        self.to_send_combo = QComboBox()
        self.amount_e = PayToAmountEdit(lambda: 8 if self.to_send_combo.currentIndex() > 0 else self.window.get_decimal_point(),
                                        lambda: self.window.send_options[self.to_send_combo.currentIndex()][:4])

        from .paytoedit import PayToEdit
        self.payto_e = PayToEdit(self)
        msg = (_("Recipient of the funds.") + "\n\n"
               + _("You may enter a Evrmore address, a label from your list of contacts "
                   "(a list of completions will be proposed), "
                   "or an alias (email-like address that forwards to a Evrmore address)") + ". "
               + _("Lightning invoices are also supported.") + "\n\n"
               + _("You can also pay to many outputs in a single transaction, "
                   "specifying one output per line.") + "\n" + _("Format: address, amount") + "\n"
               + _("To set the amount to 'max', use the '!' special character.") + "\n"
               + _("Integers weights can also be used in conjunction with '!', "
                   "e.g. set one amount to '2!' and another to '3!' to split your coins 40-60."))
        payto_label = HelpLabel(_('Pay to'), msg)
        grid.addWidget(payto_label, 1, 0)
        grid.addWidget(self.payto_e, 1, 1, 1, -1)

        completer = QCompleter()
        completer.setCaseSensitivity(False)
        self.payto_e.set_completer(completer)
        completer.setModel(self.window.completions)

        msg = _('Description of the transaction (not mandatory).') + '\n\n' \
              + _(
            'The description is not sent to the recipient of the funds. It is stored in your wallet file, and displayed in the \'History\' tab.')
        description_label = HelpLabel(_('Description'), msg)
        grid.addWidget(description_label, 2, 0)
        self.message_e = SizedFreezableLineEdit(width=700)
        grid.addWidget(self.message_e, 2, 1, 1, -1)

        op_return_vis = self.config.get('enable_op_return_messages', False)
        self.op_return_e = SizedFreezableLineEdit(width=700)
        
        msg = _('OP_RETURN message.') + '\n\n' \
              + _('A 40 byte maximum message of arbitrary data encoded in utf8 or read from hex.') + ' ' \
              + _('This message is associated with the transaction as a whole') + '\n\n' \
              + _('This will increase your fee slightly.')
        self.op_return_label = HelpLabel(_('OP_RETURN (utf8)'), msg)
        self.op_return_label.setVisible(op_return_vis)
        self.op_return_e.setVisible(op_return_vis)

        def update_text_with_proper_length():
            raw_text = self.op_return_e.text()
            try:
                if len(raw_text) <= 1:
                    raise ValueError()
                text_to_test = raw_text
                # Ignore last character of odd (user typing)
                if len(raw_text) % 2 == 1:
                    text_to_test = raw_text[:-1]
                bytes.fromhex(text_to_test)
                self.op_return_e.setText(raw_text[:80])
                self.op_return_label.setText('OP_RETURN (hex)')
            except ValueError:
                utf8_bytes = raw_text.encode('utf8')
                while (len(utf8_bytes) > 40):
                    utf8_bytes = utf8_bytes[:-1]
                    try:
                        raw_text = utf8_bytes.decode('utf8')
                    except ValueError:
                        continue
                self.op_return_e.setText(raw_text)
                self.op_return_label.setText('OP_RETURN (utf8)')
        self.op_return_e.textChanged.connect(update_text_with_proper_length)


        grid.addWidget(self.op_return_label, 3, 0)
        grid.addWidget(self.op_return_e, 3, 1, 1, -1)


        def on_to_send():
            vis = self.config.get('enable_op_return_messages', False)

            i = self.to_send_combo.currentIndex()
            self.fiat_send_e.setVisible(i == 0)
            #self.asset_memo_e.setVisible(vis and i != 0)
            #self.asset_memo_label.setVisible(vis and i != 0)
            if i == 0:
                reg = QRegExp('^[0-9]{0,11}\\.([0-9]{1,8})$')
            else:
                meta = self.window.wallet.adb.get_asset_meta(self.window.send_options[i])
                divs = meta.divisions if meta else 0
                if divs == 0:
                    reg = QRegExp('^[1-9][0-9]{0,10}$')
                else:
                    reg = QRegExp(f'^[0-9]{{0,11}}\\.([0-9]{{1,{divs}}})$')
                amt_sats = self.amount_e.get_amount()
                if amt_sats:
                    minimum_sats = COIN * pow(10, -divs)
                    new_sats = amt_sats - (amt_sats % minimum_sats)
                    self.amount_e.setAmount(new_sats)
            validator = QRegExpValidator(reg)
            self.amount_e.setValidator(validator)
            self.amount_e.update()

            # In case of max, update amount
            self.read_outputs()

        self.to_send_combo.currentIndexChanged.connect(on_to_send)

        msg = (_('The amount to be received by the recipient.') + ' '
               + _('Fees are paid by the sender.') + '\n\n'
               + _('The amount will be displayed in red if you do not have enough funds in your wallet.') + ' '
               + _('Note that if you have frozen some of your addresses, the available funds will be lower than your total balance.') + '\n\n'
               + _('Keyboard shortcut: type "!" to send all your coins.'))
        amount_label = HelpLabel(_('Amount'), msg)
        grid.addWidget(amount_label, 4, 0)
        grid.addWidget(self.amount_e, 4, 1)
        grid.addWidget(self.to_send_combo, 4, 3)

        self.fiat_send_e = AmountEdit(self.fx.get_currency if self.fx else '')
        if not self.fx or not self.fx.is_enabled():
            self.fiat_send_e.setVisible(False)
        grid.addWidget(self.fiat_send_e, 4, 2)
        self.amount_e.frozen.connect(
            lambda: self.fiat_send_e.setFrozen(self.amount_e.isReadOnly()))

        self.window.connect_fields(self.amount_e, self.fiat_send_e)

        self.max_button = EnterButton(_("Max"), self.spend_max)
        self.max_button.setFixedWidth(100)
        self.max_button.setCheckable(True)
        grid.addWidget(self.max_button, 4, 4)

        self.save_button = EnterButton(_("Save"), self.do_save_invoice)
        self.send_button = EnterButton(_("Pay") + "...", self.do_pay_or_get_invoice)
        self.clear_button = EnterButton(_("Clear"), self.do_clear)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.send_button)
        grid.addLayout(buttons, 6, 1, 1, 4)

        self.amount_e.shortcut.connect(self.spend_max)

        def reset_max(text):
            self.max_button.setChecked(False)
            enable = not bool(text) and not self.amount_e.isReadOnly()
            # self.max_button.setEnabled(enable)

        self.amount_e.textEdited.connect(reset_max)
        self.fiat_send_e.textEdited.connect(reset_max)

        self.set_onchain(False)

        self.invoices_label = QLabel(_('Send queue'))
        from .invoice_list import InvoiceList
        self.invoice_list = InvoiceList(self)

        vbox0 = QVBoxLayout()
        vbox0.addLayout(grid)
        hbox = QHBoxLayout()
        hbox.addLayout(vbox0)
        hbox.addStretch(1)

        vbox = QVBoxLayout(self)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        vbox.addWidget(self.invoices_label)
        vbox.addWidget(self.invoice_list)
        vbox.setStretchFactor(self.invoice_list, 1000)
        self.searchable_list = self.invoice_list
        self.invoice_list.update()  # after parented and put into a layout, can update without flickering
        run_hook('create_send_tab', grid)

        self.payment_request_ok_signal.connect(self.payment_request_ok)
        self.payment_request_error_signal.connect(self.payment_request_error)
        self.lnurl6_round1_signal.connect(self.on_lnurl6_round1)
        self.lnurl6_round2_signal.connect(self.on_lnurl6_round2)
        self.clear_send_tab_signal.connect(self.do_clear)
        self.show_error_signal.connect(self.show_error)

    def spend_max(self):
        if run_hook('abort_send', self):
            return
        outputs = self.payto_e.get_outputs(True)
        if not outputs:
            return

        asset: Optional[str] = None
        for output in outputs:
            if output.asset is not None:
                if asset is None:
                    asset = output.asset
                elif asset != output.asset:
                    raise UserFacingException(_('Only one asset at a time is currently supported.'))
        
        make_tx = lambda fee_est, raise_on_asset_amount_modified: self.wallet.make_unsigned_transaction(
            coins=self.window.get_coins(asset=asset),
            outputs=outputs,
            fee=fee_est,
            is_sweep=False,
            raise_on_asset_changes=raise_on_asset_amount_modified)

        raise_on_asset_amount_modified = True
        while True:
            try:
                try:
                    try:
                        tx = make_tx(None, raise_on_asset_amount_modified)
                        break
                    except (NotEnoughFunds, NoDynamicFeeEstimates) as e:
                        # Check if we had enough funds excluding fees,
                        # if so, still provide opportunity to set lower fees.
                        tx = make_tx(0, raise_on_asset_amount_modified)
                        break
                except AssetAmountModified:
                    self.notify_asset_amounts_have_changed = True
                    raise_on_asset_amount_modified = False
                    continue
            except NotEnoughFunds as e:
                self.max_button.setChecked(False)
                text = self.get_text_not_enough_funds_mentioning_frozen()
                self.show_error(text)
                return

        self.max_button.setChecked(True)
        amount = tx.output_value()
            
        __, x_fee_amount = run_hook('get_tx_extra_fee', self.wallet, tx) or (None, EvrmoreValue())
        amount_after_all_fees = amount - x_fee_amount

        if amount_after_all_fees.assets:
            if len(amount_after_all_fees.assets) > 1:
                raise UserFacingException(_('Only one asset operation at a time is supported'))
            amount_after_all_fees = list(amount_after_all_fees.assets.values())[0].value
        else:
            amount_after_all_fees = amount_after_all_fees.evr_value.value

        self.amount_e.setAmount(amount_after_all_fees)
        # show tooltip explaining max amount
        mining_fee = tx.get_fee()
        # Mining fees will only be evr
        mining_fee_str = self.format_amount_and_units(mining_fee.evr_value.value)
        msg = _("Mining fee: {} (can be adjusted on next screen)").format(mining_fee_str)
        if x_fee_amount != EvrmoreValue():
            twofactor_fee_str = self.format_amount_and_units(x_fee_amount)
            msg += "\n" + _("2fa fee: {} (for the next batch of transactions)").format(twofactor_fee_str)
        frozen_bal = self.get_frozen_balance_str()
        if frozen_bal:
            msg += "\n" + _("Some coins are frozen: {} (can be unfrozen in the Addresses or in the Coins tab)").format(frozen_bal)
        QToolTip.showText(self.max_button.mapToGlobal(QPoint(0, 0)), msg)

    def pay_onchain_dialog(
            self, inputs: Sequence[PartialTxInput],
            outputs: List[PartialTxOutput], *,
            external_keypairs=None,
            mixed=False,
            coinbase_outputs: List[PartialTxOutput]=None,
            mandatory_inputs: List[PartialTxInput]=list(),
            locking_script_overrides: Dict[str, str]=None
        ) -> None:
        # trustedcoin requires this
        if run_hook('abort_send', self):
            return
        is_sweep = bool(external_keypairs)
        
        op_return_raw: str = self.op_return_e.text()
        if len(op_return_raw) > 0:
            try:
                if len(op_return_raw) <= 1:
                    raise ValueError()
                if len(op_return_raw) % 2 == 0:
                    text_to_encode = op_return_raw
                else:
                    self.show_warning(_('OP_RETURN predicted hex value has an odd number of characters; truncating by 1 to maintain a proper hex value.'))
                    text_to_encode = op_return_raw[:-1]
                op_return_encoded = bytes.fromhex(text_to_encode)
            except ValueError:
                op_return_encoded = op_return_raw.encode('utf8')
            outputs.append(
                PartialTxOutput(
                    value=0,
                    scriptpubkey=make_op_return(op_return_encoded)
                ))

        make_tx = lambda fee_est: self.wallet.make_unsigned_transaction(
            coins=inputs,
            outputs=outputs,
            fee=fee_est,
            is_sweep=is_sweep,
            coinbase_outputs=coinbase_outputs,
            inputs=mandatory_inputs,
            locking_script_overrides=locking_script_overrides,
            raise_on_asset_changes=False)
        output_values = [x.evrmore_value for x in outputs]
        if any(parse_max_spend(outval) for outval in output_values):
            output_value = '!'
        else:
            output_value = sum(output_values, EvrmoreValue())
        conf_dlg = ConfirmTxDialog(window=self.window, make_tx=make_tx, output_value=output_value, is_sweep=is_sweep)
        if conf_dlg.not_enough_funds:
            # Check if we had enough funds excluding fees,
            # if so, still provide opportunity to set lower fees.
            if not conf_dlg.have_enough_funds_assuming_zero_fees():
                text = self.get_text_not_enough_funds_mentioning_frozen()
                self.show_message(text)
                return

        # shortcut to advanced preview (after "enough funds" check!)
        if self.config.get('advanced_preview'):
            preview_dlg = PreviewTxDialog(
                window=self.window,
                make_tx=make_tx,
                external_keypairs=external_keypairs,
                mixed=mixed,
                output_value=output_value)
            preview_dlg.show()
            return

        cancelled, is_send, password, tx = conf_dlg.run()
        if cancelled:
            return
        if is_send:
            self.save_pending_invoice()
            def sign_done(success):
                if success:
                    self.window.broadcast_or_show(tx)
            self.window.sign_tx_with_password(
                tx,
                callback=sign_done,
                password=password,
                mixed=mixed,
                external_keypairs=external_keypairs,
            )
        else:
            preview_dlg = PreviewTxDialog(
                window=self.window,
                make_tx=make_tx,
                mixed=mixed,
                external_keypairs=external_keypairs,
                output_value=output_value)
            preview_dlg.show()

    def get_text_not_enough_funds_mentioning_frozen(self) -> str:
        text = _("Not enough funds")
        frozen_str = self.get_frozen_balance_str()
        if frozen_str:
            text += " ({} {})".format(
                frozen_str, _("are frozen")
            )
        return text

    def get_frozen_balance_str(self) -> Optional[str]:
        frozen_bal = sum(self.wallet.get_frozen_balance(), EvrmoreValue())
        if frozen_bal == EvrmoreValue():
            return None
        return self.format_amount_and_units(frozen_bal)

    def do_clear(self):
        self._lnurl_data = None
        self.send_button.restore_original_text()
        self.max_button.setChecked(False)
        self.payment_request = None
        self.payto_URI = None
        self.payto_e.do_clear()
        self.set_onchain(False)
        for e in [self.message_e, self.amount_e]:
            e.setText('')
            e.setFrozen(False)
        for e in [self.send_button, self.save_button, self.clear_button, self.amount_e, self.fiat_send_e]:
            e.setEnabled(True)
        self.window.update_status()
        run_hook('do_clear', self)

    def set_onchain(self, b):
        self._is_onchain = b
        self.max_button.setEnabled(b)

    def lock_amount(self, b: bool) -> None:
        self.amount_e.setFrozen(b)
        self.max_button.setEnabled(not b)

    def prepare_for_send_tab_network_lookup(self):
        self.window.show_send_tab()
        self.payto_e.disable_checks = True
        for e in [self.payto_e, self.message_e]:
            e.setFrozen(True)
        self.lock_amount(True)
        for btn in [self.save_button, self.send_button, self.clear_button]:
            btn.setEnabled(False)
        self.payto_e.setTextNoCheck(_("please wait..."))

    def payment_request_ok(self):
        pr = self.payment_request
        if not pr:
            return
        invoice = Invoice.from_bip70_payreq(pr, height=0)
        if self.wallet.get_invoice_status(invoice) == PR_PAID:
            self.show_message("invoice already paid")
            self.do_clear()
            self.payment_request = None
            return
        self.payto_e.disable_checks = True
        if not pr.has_expired():
            self.payto_e.setGreen()
        else:
            self.payto_e.setExpired()
        self.payto_e.setTextNoCheck(pr.get_requestor())
        self.amount_e.setAmount(pr.get_amount())
        self.message_e.setText(pr.get_memo())
        self.set_onchain(True)
        self.max_button.setEnabled(False)
        for btn in [self.send_button, self.clear_button]:
            btn.setEnabled(True)
        # signal to set fee
        self.amount_e.textEdited.emit("")

    def payment_request_error(self):
        pr = self.payment_request
        if not pr:
            return
        self.show_message(pr.error)
        self.payment_request = None
        self.do_clear()

    def on_pr(self, request: 'paymentrequest.PaymentRequest'):
        self.payment_request = request
        if self.payment_request.verify(self.window.contacts):
            self.payment_request_ok_signal.emit()
        else:
            self.payment_request_error_signal.emit()

    def set_lnurl6(self, lnurl: str, *, can_use_network: bool = True):
        try:
            url = decode_lnurl(lnurl)
        except LnInvoiceException as e:
            self.show_error(_("Error parsing Lightning invoice") + f":\n{e}")
            return
        if not can_use_network:
            return

        async def f():
            try:
                lnurl_data = await request_lnurl(url)
            except LNURLError as e:
                self.show_error_signal.emit(f"LNURL request encountered error: {e}")
                self.clear_send_tab_signal.emit()
                return
            self.lnurl6_round1_signal.emit(lnurl_data, url)

        asyncio.run_coroutine_threadsafe(f(), get_asyncio_loop())  # TODO should be cancellable
        self.prepare_for_send_tab_network_lookup()

    def on_lnurl6_round1(self, lnurl_data: LNURL6Data, url: str):
        self._lnurl_data = lnurl_data
        domain = urlparse(url).netloc
        self.payto_e.setTextNoCheck(f"invoice from lnurl")
        self.message_e.setText(f"lnurl: {domain}: {lnurl_data.metadata_plaintext}")
        self.amount_e.setAmount(lnurl_data.min_sendable_sat)
        self.amount_e.setFrozen(False)
        self.send_button.setText(_('Get Invoice'))
        for btn in [self.send_button, self.clear_button]:
            btn.setEnabled(True)
        self.set_onchain(False)

    def set_bolt11(self, invoice: str):
        """Parse ln invoice, and prepare the send tab for it."""
        try:
            lnaddr = lndecode(invoice)
        except LnInvoiceException as e:
            self.show_error(_("Error parsing Lightning invoice") + f":\n{e}")
            return
        except lnutil.IncompatibleOrInsaneFeatures as e:
            self.show_error(_("Invoice requires unknown or incompatible Lightning feature") + f":\n{e!r}")
            return

        pubkey = bh2u(lnaddr.pubkey.serialize())
        for k,v in lnaddr.tags:
            if k == 'd':
                description = v
                break
        else:
             description = ''
        self.payto_e.setFrozen(True)
        self.payto_e.setTextNoCheck(pubkey)
        self.payto_e.lightning_invoice = invoice
        if not self.message_e.text():
            self.message_e.setText(description)
        if lnaddr.get_amount_sat() is not None:
            self.amount_e.setAmount(lnaddr.get_amount_sat())
        self.set_onchain(False)

    def set_bip21(self, text: str, *, can_use_network: bool = True):
        on_bip70_pr = self.on_pr if can_use_network else None
        try:
            out = util.parse_URI(text, on_bip70_pr)
        except InvalidBitcoinURI as e:
            self.show_error(_("Error parsing URI") + f":\n{e}")
            return
        if out.get('asset', None):
            asset = out['asset']
            try:
                index = self.window.send_options.index(asset)
                self.to_send_combo.setCurrentIndex(index)
            except ValueError:
                self.payto_e.do_clear()
                self.show_error(_("Asset not available") + f":\n{asset}")
                return
        self.payto_URI = out
        r = out.get('r')
        sig = out.get('sig')
        name = out.get('name')
        if (r or (name and sig)) and can_use_network:
            self.prepare_for_send_tab_network_lookup()
            return
        address = out.get('address')
        amount = out.get('amount')
        label = out.get('label')
        message = out.get('message')
        lightning = out.get('lightning')
        if lightning:
            self.handle_payment_identifier(lightning, can_use_network=can_use_network)
            return
        # use label as description (not BIP21 compliant)
        if label and not message:
            message = label
        if address:
            self.payto_e.setText(address)
        if message:
            self.message_e.setText(message)
        if amount:
            self.amount_e.setAmount(amount)            

    def handle_payment_identifier(self, text: str, *, can_use_network: bool = True):
        """Takes
        Lightning identifiers:
        * lightning-URI (containing bolt11 or lnurl)
        * bolt11 invoice
        * lnurl
        Bitcoin identifiers:
        * bitcoin-URI
        and sets the sending screen.
        """
        text = text.strip()
        if not text:
            return
        if invoice_or_lnurl := maybe_extract_lightning_payment_identifier(text):
            if invoice_or_lnurl.startswith('lnurl'):
                self.set_lnurl6(invoice_or_lnurl, can_use_network=can_use_network)
            else:
                self.set_bolt11(invoice_or_lnurl)
        elif text.lower().startswith(util.BITCOIN_BIP21_URI_SCHEME + ':'):
            self.set_bip21(text, can_use_network=can_use_network)
        else:
            raise ValueError("Could not handle payment identifier.")
        # update fiat amount
        self.amount_e.textEdited.emit("")
        self.window.show_send_tab()

    def read_invoice(self):
        if self.check_send_tab_payto_line_and_show_errors():
            return
        try:
            if not self._is_onchain:
                raise UserFacingException(_('Only on-chain invoices are currently supported'))
                invoice_str = self.payto_e.lightning_invoice
                if not invoice_str:
                    return
                if not self.wallet.has_lightning():
                    self.show_error(_('Lightning is disabled'))
                    return
                invoice = Invoice.from_bech32(invoice_str)
                if invoice.amount_msat is None:
                    amount_sat = self.amount_e.get_amount()
                    if amount_sat:
                        invoice.amount_msat = int(amount_sat * 1000)
                    else:
                        self.show_error(_('No amount'))
                        return
                return invoice
            else:
                outputs = self.read_outputs()
                if self.check_send_tab_onchain_outputs_and_show_errors(outputs):
                    return
                message = self.message_e.text()
                return self.wallet.create_invoice(
                    outputs=outputs,
                    message=message,
                    pr=self.payment_request,
                    URI=self.payto_URI)
        except InvoiceError as e:
            self.show_error(_('Error creating payment') + ':\n' + str(e))

    def do_save_invoice(self):
        self.pending_invoice = self.read_invoice()
        if not self.pending_invoice:
            return
        self.save_pending_invoice()

    def save_pending_invoice(self):
        if not self.pending_invoice:
            return
        self.do_clear()
        self.wallet.save_invoice(self.pending_invoice)
        self.invoice_list.update()
        self.pending_invoice = None

    def _lnurl_get_invoice(self) -> None:
        assert self._lnurl_data
        amount = self.amount_e.get_amount()
        if not (self._lnurl_data.min_sendable_sat <= amount <= self._lnurl_data.max_sendable_sat):
            self.show_error(f'Amount must be between {self._lnurl_data.min_sendable_sat} and {self._lnurl_data.max_sendable_sat} sat.')
            return

        async def f():
            try:
                invoice_data = await callback_lnurl(
                    self._lnurl_data.callback_url,
                    params={'amount': self.amount_e.get_amount() * 1000},
                )
            except LNURLError as e:
                self.show_error_signal.emit(f"LNURL request encountered error: {e}")
                self.clear_send_tab_signal.emit()
                return
            invoice = invoice_data.get('pr')
            self.lnurl6_round2_signal.emit(invoice)

        asyncio.run_coroutine_threadsafe(f(), get_asyncio_loop())  # TODO should be cancellable
        self.prepare_for_send_tab_network_lookup()

    def on_lnurl6_round2(self, bolt11_invoice: str):
        self.set_bolt11(bolt11_invoice)
        self.payto_e.setFrozen(True)
        self.amount_e.setEnabled(False)
        self.fiat_send_e.setEnabled(False)
        for btn in [self.send_button, self.clear_button, self.save_button]:
            btn.setEnabled(True)
        self.send_button.restore_original_text()
        self._lnurl_data = None

    def do_pay_or_get_invoice(self):
        if self._lnurl_data:
            self._lnurl_get_invoice()
            return
        self.pending_invoice = self.read_invoice()
        if not self.pending_invoice:
            return
        self.do_pay_invoice(self.pending_invoice)

    def pay_multiple_invoices(self, invoices):
        outputs = []
        asset: Optional[str] = None
        for invoice in invoices:
            invoice_outputs: Optional[List[PartialTxOutput]] = invoice.outputs
            for output in invoice_outputs:
                if output.asset is not None:
                    if asset is None:
                        asset = output.asset
                    elif asset != output.asset:
                        raise UserFacingException(_('Only one asset at a time is currently supported.'))
            outputs += invoice_outputs
        self.pay_onchain_dialog(self.window.get_coins(asset=asset), outputs)

    def do_pay_invoice(self, invoice: 'Invoice'):
        if invoice.is_lightning():
            raise UserFacingException(_('Lightning is currently not supported'))
            self.pay_lightning_invoice(invoice)
        else:
            if self.notify_asset_amounts_have_changed:
                self.notify_asset_amounts_have_changed = False
                text = _('Transaction amounts could not be properly distributed evenly due to ' +
                        'divisibility. Amounts may not be what you ' +
                        'expect. Click "Advanced" on the payment window for more information.')
                self.show_warning(text)
            asset: Optional[str] = None
            invoice_outputs: Optional[List[PartialTxOutput]] = invoice.outputs
            for output in invoice_outputs:
                if output.asset is not None:
                    if asset is None:
                        asset = output.asset
                    elif asset != output.asset:
                        raise UserFacingException(_('Only one asset at a time is currently supported.'))
            self.pay_onchain_dialog(self.window.get_coins(asset=asset), invoice.outputs)

    def read_outputs(self) -> List[PartialTxOutput]:
        if self.payment_request:
            outputs = self.payment_request.get_outputs()
        else:
            # Double check in case asset has changed
            self.payto_e._check_text(full_check=True, force_check=True)
            outputs = self.payto_e.get_outputs(self.max_button.isChecked())
        return outputs

    def check_send_tab_onchain_outputs_and_show_errors(self, outputs: List[PartialTxOutput]) -> bool:
        """Returns whether there are errors with outputs.
        Also shows error dialog to user if so.
        """
        if not outputs:
            self.show_error(_('No outputs'))
            return True

        for o in outputs:
            if o.scriptpubkey is None:
                self.show_error(_('Bitcoin Address is None'))
                return True
            if o.value is None:
                self.show_error(_('Invalid Amount'))
                return True

        return False  # no errors

    def check_send_tab_payto_line_and_show_errors(self) -> bool:
        """Returns whether there are errors.
        Also shows error dialog to user if so.
        """
        pr = self.payment_request
        if pr:
            if pr.has_expired():
                self.show_error(_('Payment request has expired'))
                return True

        if not pr:
            errors = self.payto_e.get_errors()
            if errors:
                if len(errors) == 1 and not errors[0].is_multiline:
                    err = errors[0]
                    self.show_warning(_("Failed to parse 'Pay to' line") + ":\n" +
                                      f"{err.line_content[:40]}...\n\n"
                                      f"{err.exc!r}")
                else:
                    self.show_warning(_("Invalid Lines found:") + "\n\n" +
                                      '\n'.join([_("Line #") +
                                                 f"{err.idx+1}: {err.line_content[:40]}... ({err.exc!r})"
                                                 for err in errors]))
                return True

            if self.payto_e.is_alias and self.payto_e.validated is False:
                alias = self.payto_e.toPlainText()
                msg = _('WARNING: the alias "{}" could not be validated via an additional '
                        'security check, DNSSEC, and thus may not be correct.').format(alias) + '\n'
                msg += _('Do you wish to continue?')
                if not self.question(msg):
                    return True

        return False  # no errors

    def pay_lightning_invoice(self, invoice: Invoice):
        amount_sat = invoice.get_amount_sat()
        key = self.wallet.get_key_for_outgoing_invoice(invoice)
        if amount_sat is None:
            raise Exception("missing amount for LN invoice")
        if not self.wallet.lnworker.can_pay_invoice(invoice):
            num_sats_can_send = int(self.wallet.lnworker.num_sats_can_send())
            lightning_needed = amount_sat - num_sats_can_send
            lightning_needed += (lightning_needed // 20) # operational safety margin
            coins = self.window.get_coins(nonlocal_only=True)
            can_pay_onchain = invoice.get_address() and self.wallet.can_pay_onchain(invoice.get_outputs(), coins=coins)
            can_pay_with_new_channel = self.wallet.lnworker.suggest_funding_amount(amount_sat, coins=coins)
            can_pay_with_swap = self.wallet.lnworker.suggest_swap_to_send(amount_sat, coins=coins)
            rebalance_suggestion = self.wallet.lnworker.suggest_rebalance_to_send(amount_sat)
            can_rebalance = bool(rebalance_suggestion) and self.window.num_tasks() == 0
            choices = {}
            if can_rebalance:
                msg = ''.join([
                    _('Rebalance existing channels'), '\n',
                    _('Move funds between your channels in order to increase your sending capacity.')
                ])
                choices[0] = msg
            if can_pay_with_new_channel:
                msg = ''.join([
                    _('Open a new channel'), '\n',
                    _('You will be able to pay once the channel is open.')
                ])
                choices[1] = msg
            if can_pay_with_swap:
                msg = ''.join([
                    _('Swap onchain funds for lightning funds'), '\n',
                    _('You will be able to pay once the swap is confirmed.')
                ])
                choices[2] = msg
            if can_pay_onchain:
                msg = ''.join([
                    _('Pay onchain'), '\n',
                    _('Funds will be sent to the invoice fallback address.')
                ])
                choices[3] = msg
            if not choices:
                raise NotEnoughFunds()
            msg = _('You cannot pay that invoice using Lightning.')
            if self.wallet.lnworker.channels:
                msg += '\n' + _('Your channels can send {}.').format(self.format_amount(num_sats_can_send) + self.base_unit())
            r = self.window.query_choice(msg, choices)
            if r is not None:
                self.save_pending_invoice()
                if r == 0:
                    chan1, chan2, delta = rebalance_suggestion
                    self.window.rebalance_dialog(chan1, chan2, amount_sat=delta)
                elif r == 1:
                    amount_sat, min_amount_sat = can_pay_with_new_channel
                    self.window.channels_list.new_channel_dialog(amount_sat=amount_sat, min_amount_sat=min_amount_sat)
                elif r == 2:
                    chan, swap_recv_amount_sat = can_pay_with_swap
                    self.window.run_swap_dialog(is_reverse=False, recv_amount_sat=swap_recv_amount_sat, channels=[chan])
                elif r == 3:
                    self.pay_onchain_dialog(coins, invoice.get_outputs())
            return

        # FIXME this is currently lying to user as we truncate to satoshis
        amount_msat = invoice.get_amount_msat()
        msg = _("Pay lightning invoice?") + '\n\n' + _("This will send {}?").format(self.format_amount_and_units(Decimal(amount_msat)/1000))
        if not self.question(msg):
            return
        self.save_pending_invoice()
        coro = self.wallet.lnworker.pay_invoice(invoice.lightning_invoice, amount_msat=amount_msat)
        self.window.run_coroutine_from_thread(coro, _('Sending payment'))

    def broadcast_transaction(self, tx: Transaction):

        def broadcast_thread():
            # non-GUI thread
            pr = self.payment_request
            if pr and pr.has_expired():
                self.payment_request = None
                return False, _("Invoice has expired")
            try:
                self.network.run_from_another_thread(self.network.broadcast_transaction(tx))
            except TxBroadcastError as e:
                return False, e.get_message_for_gui()
            except BestEffortRequestFailed as e:
                return False, repr(e)
            # success
            txid = tx.txid()
            if pr:
                self.payment_request = None
                refund_address = self.wallet.get_receiving_address()
                coro = pr.send_payment_and_receive_paymentack(tx.serialize(), refund_address)
                fut = asyncio.run_coroutine_threadsafe(coro, self.network.asyncio_loop)
                ack_status, ack_msg = fut.result(timeout=20)
                self.logger.info(f"Payment ACK: {ack_status}. Ack message: {ack_msg}")
            return True, txid

        # Capture current TL window; override might be removed on return
        parent = self.window.top_level_window(lambda win: isinstance(win, MessageBoxMixin))

        def broadcast_done(result):
            # GUI thread
            if result:
                success, msg = result
                if success:
                    parent.show_message(_('Payment sent.') + '\n' + msg)
                    self.invoice_list.update()
                else:
                    msg = msg or ''
                    parent.show_error(msg)

        WaitingDialog(self, _('Broadcasting transaction...'),
                      broadcast_thread, broadcast_done, self.window.on_error)

    def paytomany(self):
        self.window.show_send_tab()
        self.payto_e.paytomany()
        msg = '\n'.join([
            _('Enter a list of outputs in the \'Pay to\' field.'),
            _('One output per line.'),
            _('Format: address, amount'),
            _('You may load a CSV file using the file icon.')
        ])
        self.show_message(msg, title=_('Pay to many'))

    def payto_contacts(self, labels):
        paytos = [self.window.get_contact_payto(label) for label in labels]
        self.window.show_send_tab()
        if len(paytos) == 1:
            self.payto_e.setText(paytos[0])
            self.amount_e.setFocus()
        else:
            text = "\n".join([payto + ", 0" for payto in paytos])
            self.payto_e.setText(text)
            self.payto_e.setFocus()


