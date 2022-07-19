#!/usr/bin/env python

from electrum.i18n import _

from PyQt5.QtWidgets import QVBoxLayout, QLabel

from .util import WindowModalDialog, ButtonsLineEdit, Buttons, CloseButton
from .history_list import HistoryList, AssetHistoryModel
from .qrtextedit import ShowQRTextEdit

class AssetDialog(WindowModalDialog):

    def __init__(self, parent, asset):
        WindowModalDialog.__init__(self, parent, _("Asset"))
        self.asset = asset
        self.parent = parent
        self.config = parent.config
        self.wallet = parent.wallet
        self.app = parent.app
        self.saved = True

        self.setMinimumWidth(700)
        vbox = QVBoxLayout()
        self.setLayout(vbox)

        vbox.addWidget(QLabel(_("Asset:")))
        self.addr_e = ButtonsLineEdit(self.asset)
        self.addr_e.addCopyButton()
        self.addr_e.setReadOnly(True)
        vbox.addWidget(self.addr_e)

        vbox.addWidget(QLabel(_("History")))
        addr_hist_model = AssetHistoryModel(self.parent, self.asset)
        self.hw = HistoryList(self.parent, addr_hist_model)
        addr_hist_model.set_view(self.hw)
        vbox.addWidget(self.hw)

        vbox.addLayout(Buttons(CloseButton(self)))
        self.format_amount = self.parent.format_amount
        addr_hist_model.refresh('asset dialog constructor')
