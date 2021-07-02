# Copyright (C) 2019 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

import asyncio
import base64
from distutils.version import StrictVersion

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QLabel, QProgressBar,
                             QHBoxLayout, QPushButton, QDialog, QGridLayout, QTextEdit, QLineEdit)

from electrum import version
from electrum import constants
from electrum import ecc
from electrum.gui.qt import ColorScheme
from electrum.i18n import _
from electrum.util import make_aiohttp_session
from electrum.logging import Logger
from electrum.network import Network


VERSION_ANNOUNCEMENT_SIGNING_KEYS = (
        "RPuQNvDVBC5Q4fXKyfYLjrunbyqiEYckP5",  # kralverde since ravencoin fork
    )


class UpdateCheck(QDialog, Logger):
    url = "https://raw.githubusercontent.com/Electrum-RVN-SIG/electrum-ravencoin/master/check-version.json"
    download_url = "https://github.com/Electrum-RVN-SIG/electrum-ravencoin/releases"

    class VerifyUpdateHashes(QWidget):
        def __init__(self):
            super().__init__()

            layout = QGridLayout(self)

            title = QLabel(_('Verify Update Hashes for Binaries'))
            layout.addWidget(title, 0, 1)

            message_e = QTextEdit()
            message_e.setAcceptRichText(False)
            message_e.setPlaceholderText("===BEGIN CHECKSUMS===\nx\nx\nx\nx\nx\n===END CHECKSUMS===")
            layout.addWidget(QLabel(_('SHA256 checksums')), 1, 0)
            layout.addWidget(message_e, 1, 1)
            layout.setRowStretch(2, 3)

            signature_e = QTextEdit()
            signature_e.setAcceptRichText(False)
            layout.addWidget(QLabel(_('Signature')), 2, 0)
            layout.addWidget(signature_e, 2, 1)
            layout.setRowStretch(2, 1)

            hbox = QHBoxLayout()

            b = QPushButton(_("Verify"))
            b.clicked.connect(lambda: self.verify(message_e, signature_e))
            hbox.addWidget(b)

            layout.addLayout(hbox, 4, 1)

            self.message = QLabel()
            layout.addWidget(self.message, 4, 0)

        def verify(self, message, signature):
            message = message.toPlainText().strip().encode('utf-8')
            verified = False
            for address in VERSION_ANNOUNCEMENT_SIGNING_KEYS:
                try:
                    # This can throw on invalid base64
                    sig = base64.b64decode(str(signature.toPlainText()))
                    verified = ecc.verify_message_with_address(address, sig, message, net=constants.RavencoinMainnet)
                    break
                except:
                    pass
            if verified:
                self.message.setText(_("Signature verified"))
                self.message.setStyleSheet(ColorScheme.GREEN.as_stylesheet())
            else:
                self.message.setText(_("Wrong signature"))
                self.message.setStyleSheet(ColorScheme.RED.as_stylesheet())

    def __init__(self, *, latest_version=None):
        QDialog.__init__(self)
        self.setWindowTitle('Electrum - ' + _('Update Check'))
        self.content = QVBoxLayout()
        self.content.setContentsMargins(*[10]*4)

        self.heading_label = QLabel()
        self.content.addWidget(self.heading_label)

        self.detail_label = QLabel()
        self.detail_label.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.detail_label.setOpenExternalLinks(True)
        self.content.addWidget(self.detail_label)

        self.pb = QProgressBar()
        self.pb.setMaximum(0)
        self.pb.setMinimum(0)
        self.content.addWidget(self.pb)

        versions = QHBoxLayout()
        versions.addWidget(QLabel(_("Current version: {}".format(version.ELECTRUM_VERSION))))
        self.latest_version_label = QLabel(_("Latest version: {}".format(" ")))
        versions.addWidget(self.latest_version_label)
        self.content.addLayout(versions)

        self.verify_updates = self.VerifyUpdateHashes()
        self.content.addWidget(self.verify_updates)

        self.update_view(latest_version)

        self.update_check_thread = UpdateCheckThread()
        self.update_check_thread.checked.connect(self.on_version_retrieved)
        self.update_check_thread.failed.connect(self.on_retrieval_failed)
        self.update_check_thread.start()

        close_button = QPushButton(_("Close"))
        close_button.clicked.connect(self.close)
        self.content.addWidget(close_button)
        self.setLayout(self.content)
        self.show()

    def on_version_retrieved(self, version):
        self.update_view(version)

    def on_retrieval_failed(self):
        self.heading_label.setText('<h2>' + _("Update check failed") + '</h2>')
        self.detail_label.setText(_("Sorry, but we were unable to check for updates. Please try again later."))
        self.pb.hide()

    @staticmethod
    def is_newer(latest_version):
        return latest_version > StrictVersion(version.ELECTRUM_VERSION)

    def update_view(self, latest_version=None):
        if latest_version:
            self.pb.hide()
            self.latest_version_label.setText(_("Latest version: {}".format(latest_version)))
            if self.is_newer(latest_version):
                self.heading_label.setText('<h2>' + _("There is a new update available") + '</h2>')
                url = "<a href='{u}'>{u}</a>".format(u=UpdateCheck.download_url)
                self.detail_label.setText(_("You can download the new version from {}.").format(url))
            else:
                self.heading_label.setText('<h2>' + _("Already up to date") + '</h2>')
                self.detail_label.setText(_("You are already on the latest version of Electrum."))
        else:
            self.heading_label.setText('<h2>' + _("Checking for updates...") + '</h2>')
            self.detail_label.setText(_("Please wait while Electrum checks for available updates."))


class UpdateCheckThread(QThread, Logger):
    checked = pyqtSignal(object)
    failed = pyqtSignal()

    def __init__(self):
        QThread.__init__(self)
        Logger.__init__(self)
        self.network = Network.get_instance()

    async def get_update_info(self):
        # note: Use long timeout here as it is not critical that we get a response fast,
        #       and it's bad not to get an update notification just because we did not wait enough.
        async with make_aiohttp_session(proxy=self.network.proxy, timeout=120) as session:
            async with session.get(UpdateCheck.url) as result:
                signed_version_dict = await result.json(content_type=None)
                # example signed_version_dict:
                # {
                #     "version": "3.9.9",
                #     "signatures": {
                #         "1Lqm1HphuhxKZQEawzPse8gJtgjm9kUKT4": "IA+2QG3xPRn4HAIFdpu9eeaCYC7S5wS/sDxn54LJx6BdUTBpse3ibtfq8C43M7M1VfpGkD5tsdwl5C6IfpZD/gQ="
                #     }
                # }
                version_num = signed_version_dict['version']
                sigs = signed_version_dict['signatures']
                for address, sig in sigs.items():
                    if address not in VERSION_ANNOUNCEMENT_SIGNING_KEYS:
                        continue
                    sig = base64.b64decode(sig)
                    msg = version_num.encode('utf-8')
                    if ecc.verify_message_with_address(address=address, sig65=sig, message=msg,
                                                       net=constants.RavencoinMainnet):
                        self.logger.info(f"valid sig for version announcement '{version_num}' from address '{address}'")
                        break
                else:
                    raise Exception('no valid signature for version announcement')
                return StrictVersion(version_num.strip())

    def run(self):
        if not self.network:
            self.failed.emit()
            return
        try:
            update_info = asyncio.run_coroutine_threadsafe(self.get_update_info(), self.network.asyncio_loop).result()
        except Exception as e:
            self.logger.info(f"got exception: '{repr(e)}'")
            self.failed.emit()
        else:
            self.checked.emit(update_info)
