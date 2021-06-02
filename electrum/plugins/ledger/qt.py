from functools import partial

from PyQt5.QtCore import pyqtSignal, QTimer, Qt
from PyQt5.QtWidgets import QInputDialog, QLabel, QVBoxLayout, QLineEdit, QWidget, QPushButton

from electrum.i18n import _
from electrum.plugin import hook
from electrum.wallet import Standard_Wallet
from electrum.gui.qt.util import WindowModalDialog

from .ledger import LedgerPlugin, Ledger_Client, AtomicBoolean, AbstractTracker
from ..hw_wallet.qt import QtHandlerBase, QtPluginBase
from ..hw_wallet.plugin import only_hook_if_libraries_available


class Plugin(LedgerPlugin, QtPluginBase):
    icon_unpaired = "ledger_unpaired.png"
    icon_paired = "ledger.png"

    def create_handler(self, window):
        return Ledger_Handler(window)

    @only_hook_if_libraries_available
    @hook
    def receive_menu(self, menu, addrs, wallet):
        if type(wallet) is not Standard_Wallet:
            return
        keystore = wallet.get_keystore()
        if type(keystore) == self.keystore_class and len(addrs) == 1:
            def show_address():
                keystore.thread.add(partial(self.show_address, wallet, addrs[0]))

            menu.addAction(_("Show on Ledger"), show_address)


class Ledger_UI(WindowModalDialog):
    def __init__(self, parse_data: AbstractTracker, atomic_b: AtomicBoolean, parent=None, title='Ledger UI'):
        super().__init__(parent, title)
        # self.setWindowModality(Qt.NonModal)
        # Thread interrupter. If we cancel, set true
        self.parse_data = parse_data
        self.atomic_b = atomic_b
        self.label = QLabel('')
        self.label.setText(_("Generating Information..."))
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)

        self.cancel = QPushButton(_('Cancel'))

        def end():
            self.finished()
            self.close()
            self.atomic_b.set_true()

        self.cancel.clicked.connect(end)
        layout.addWidget(self.cancel)

        self.setLayout(layout)
        self.setWindowFlags(self.windowFlags() | Qt.CustomizeWindowHint)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowCloseButtonHint)

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_text)

    def begin(self):
        self.timer.start(500)

    def finished(self):
        self.timer.stop()

    def update_text(self):
        self.label.setText(self.parse_data.parsed_string())


class Ledger_Handler(QtHandlerBase):
    setup_signal = pyqtSignal()
    auth_signal = pyqtSignal(object, object)
    ui_start_signal = pyqtSignal(object, object, object)
    ui_stop_signal = pyqtSignal()

    def __init__(self, win):
        super(Ledger_Handler, self).__init__(win, 'Ledger')
        self.setup_signal.connect(self.setup_dialog)
        self.auth_signal.connect(self.auth_dialog)
        self.ui_start_signal.connect(self.ui_dialog)
        self.ui_stop_signal.connect(self.stop_ui_dialog)

    def word_dialog(self, msg):
        response = QInputDialog.getText(self.top_level_window(), "Ledger Wallet Authentication", msg,
                                        QLineEdit.Password)
        if not response[1]:
            self.word = None
        else:
            self.word = str(response[0])
        self.done.set()

    def message_dialog(self, msg):
        self.clear_dialog()
        self.dialog = dialog = WindowModalDialog(self.top_level_window(), _("Ledger Status"))
        l = QLabel(msg)
        vbox = QVBoxLayout(dialog)
        vbox.addWidget(l)
        dialog.show()

    def ui_dialog(self, title, stopped_boolean, parse_data):
        self.clear_dialog()
        self.dialog = Ledger_UI(parse_data, stopped_boolean, self.top_level_window(), title)
        self.dialog.show()
        self.dialog.begin()

    def stop_ui_dialog(self):
        if isinstance(self.dialog, Ledger_UI):
            self.dialog.finished()

    def auth_dialog(self, data, client: 'Ledger_Client'):
        try:
            from .auth2fa import LedgerAuthDialog
        except ImportError as e:
            self.message_dialog(repr(e))
            return
        dialog = LedgerAuthDialog(self, data, client=client)
        dialog.exec_()
        self.word = dialog.pin
        self.done.set()

    def get_auth(self, data, *, client: 'Ledger_Client'):
        self.done.clear()
        self.auth_signal.emit(data, client)
        self.done.wait()
        return self.word

    def get_setup(self):
        self.done.clear()
        self.setup_signal.emit()
        self.done.wait()
        return

    def get_ui(self, title, atomic_b, data):
        self.ui_start_signal.emit(title, atomic_b, data)

    def finished_ui(self):
        self.ui_stop_signal.emit()

    def setup_dialog(self):
        self.show_error(_('Initialization of Ledger HW devices is currently disabled.'))
