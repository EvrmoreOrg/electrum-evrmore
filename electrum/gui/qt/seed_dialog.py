#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2013 ecdsa@github
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

from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (QVBoxLayout, QCheckBox, QHBoxLayout, QLineEdit,
                             QLabel, QCompleter, QDialog, QStyledItemDelegate,
                             QScrollArea, QWidget, QPushButton, QComboBox)

from electrum.i18n import _
from electrum.mnemonic import Mnemonic, seed_type
from electrum import old_mnemonic
from electrum import slip39

from .util import (Buttons, OkButton, WWLabel, ButtonsTextEdit, icon_path,
                   EnterButton, CloseButton, WindowModalDialog, ColorScheme,
                   ChoicesLayout, HelpButton)
from .qrtextedit import ShowQRTextEdit, ScanQRTextEdit
from .completion_text_edit import CompletionTextEdit

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig


def seed_warning_msg(seed):
    return ''.join([
        "<p>",
        _("Please save these {0} words on paper (order is important). "),
        _("This seed will allow you to recover your wallet in case "
          "of computer failure."),
        "</p>",
        "<b>" + _("WARNING") + ":</b>",
        "<ul>",
        "<li>" + _("Never disclose your seed.") + "</li>",
        "<li>" + _("Never type it on a website.") + "</li>",
        "<li>" + _("Do not store it electronically.") + "</li>",
        "</ul>"
    ]).format(len(seed.split()))


class SeedConfirmDisplay(QVBoxLayout):
    def __init__(
            self,
            title=None,
            icon=True,
            options=None,
            is_seed=None,
            parent=None,
            for_seed_words=True,
            full_check=True,
            *,
            config: 'SimpleConfig',
    ):
        QVBoxLayout.__init__(self)
        self.parent = parent
        self.options = options
        self.config = config
        self.seed_type = 'electrum'
        if title:
            self.addWidget(WWLabel(title))
        assert for_seed_words
        self.seed_e = CompletionTextEdit()
        self.seed_e.setTabChangesFocus(False)  # so that tab auto-completes
        self.is_seed = is_seed
        self.saved_is_seed = self.is_seed
        self.full_check = full_check
        self.seed_e.textChanged.connect(self.on_edit)
        self.initialize_completer()

        self.seed_e.setMaximumHeight(75)
        hbox = QHBoxLayout()
        if icon:
            logo = QLabel()
            logo.setPixmap(QPixmap(icon_path("seed.png"))
                           .scaledToWidth(64, mode=Qt.SmoothTransformation))
            logo.setMaximumWidth(60)
            hbox.addWidget(logo)
        hbox.addWidget(self.seed_e)
        self.addLayout(hbox)
        hbox = QHBoxLayout()
        hbox.addStretch(1)

        self.seed_type_label = QLabel('')
        if full_check:
            hbox.addWidget(self.seed_type_label)

        # options
        self.is_ext = False

        self.opt_button = None

        if options:
            self.opt_button = EnterButton(_('Options'), self.seed_options)

        seed_types = [
            (value, title) for value, title in (
                ('electrum', _('Electrum')),
                ('bip39', _('BIP39 seed')),
            )
        ]
        seed_type_values = [t[0] for t in seed_types]

        def f(choices_layout):
            self.seed_type = seed_type_values[choices_layout.selected_index()]
            self.is_seed = (lambda x: bool(x)) if self.seed_type != 'bip39' else self.saved_is_seed
            self.seed_status.setText('')
            self.on_edit(from_click=True)
            #self.initialize_completer()
            self.seed_warning.setText(None)

            #if self.seed_type == 'bip39':
            #    if self.opt_button:
            #        self.opt_button.setVisible(False)
            #else:
            #    if self.opt_button:
            #        self.opt_button.setVisible(True)

        if options and full_check:
            hbox.addWidget(self.opt_button)
        self.addLayout(hbox)

        checked_index = seed_type_values.index(self.seed_type)
        titles = [t[1] for t in seed_types]
        self.clayout = ChoicesLayout(_('Seed type'), titles, on_clicked=f, checked_index=checked_index)
        if full_check:
            hbox.addLayout(self.clayout.layout())

        self.addStretch(1)
        self.seed_status = WWLabel('')
        self.addWidget(self.seed_status)
        self.seed_warning = WWLabel('')
        self.addWidget(self.seed_warning)

        self.lang = 'en'

    def seed_options(self):
        dialog = QDialog()
        vbox = QVBoxLayout(dialog)

        if 'ext' in self.options:
            cb_ext = QCheckBox(_('Extend this seed with custom words (the "passphrase")'))
            cb_ext.setChecked(self.is_ext)
            vbox.addWidget(cb_ext)

        vbox.addLayout(Buttons(OkButton(dialog)))
        if not dialog.exec_():
            return None
        self.is_ext = cb_ext.isChecked() if 'ext' in self.options else False
        #self.seed_type = 'electrum'

    def initialize_completer(self):
        if self.seed_type != 'slip39':
            bip39_english_list = Mnemonic('en').wordlist
            old_list = old_mnemonic.wordlist
            only_old_list = set(old_list) - set(bip39_english_list)
            self.wordlist = list(bip39_english_list) + list(only_old_list)  # concat both lists
            self.wordlist.sort()

            class CompleterDelegate(QStyledItemDelegate):
                def initStyleOption(self, option, index):
                    super().initStyleOption(option, index)
                    # Some people complained that due to merging the two word lists,
                    # it is difficult to restore from a metal backup, as they planned
                    # to rely on the "4 letter prefixes are unique in bip39 word list" property.
                    # So we color words that are only in old list.
                    if option.text in only_old_list:
                        # yellow bg looks ~ok on both light/dark theme, regardless if (un)selected
                        option.backgroundBrush = ColorScheme.YELLOW.as_color(background=True)

            delegate = CompleterDelegate(self.seed_e)
        else:
            self.wordlist = list(slip39.get_wordlist())
            delegate = None

        self.completer = QCompleter(self.wordlist)
        if delegate:
            self.completer.popup().setItemDelegate(delegate)
        self.seed_e.set_completer(self.completer)

    def get_seed_words(self):
        return self.seed_e.text().split()

    def get_seed(self):
        if self.seed_type != 'slip39':
            return ' '.join(self.get_seed_words())
        else:
            return self.slip39_seed

    def on_edit(self, *, from_click=False):
        s = ' '.join(self.get_seed_words())
        b = self.is_seed(s)

        if self.full_check:
            is_checksum = False
            is_wordlist = False

            from electrum.keystore import bip39_is_checksum_valid
            from electrum.mnemonic import Wordlist, filenames

            lang = ''
            for type, file in filenames.items():
                word_list = Wordlist.from_file(file)
                is_checksum, is_wordlist = bip39_is_checksum_valid(s, wordlist=word_list)
                if not is_wordlist and len(s.split()) > 1:
                    is_checksum, is_wordlist = bip39_is_checksum_valid(' '.join(s.split()[:-1]), wordlist=word_list)
                if is_wordlist:
                    lang = type
                    break

            if lang and lang != self.lang:
                if lang == 'en':
                    bip39_english_list = Mnemonic('en').wordlist
                    old_list = old_mnemonic.wordlist
                    only_old_list = set(old_list) - set(bip39_english_list)
                    self.wordlist = list(bip39_english_list) + list(only_old_list)  # concat both lists
                else:
                    self.wordlist = list(Mnemonic(lang).wordlist)

                self.wordlist.sort()
                self.completer.model().setStringList(self.wordlist)
                self.lang = lang

            if self.seed_type == 'bip39':
                status = ('checksum: ' + ('ok' if is_checksum else 'failed')) if is_wordlist else 'unknown wordlist'
                label = 'BIP39 - ' + lang + ' (%s)' % status
                b = is_checksum
            else:
                t = seed_type(s)
                label = _('Seed Type') + ': ' + t if t else ''

                if is_checksum and is_wordlist and not from_click:
                    # This is a valid bip39 and this method was called from typing
                    # Emulate selecting the bip39 option
                    self.clayout.group.buttons()[1].click()
                    return

            self.seed_type_label.setText(label)

        self.parent.next_button.setEnabled(b)

        # disable suggestions if user already typed an unknown word
        for word in self.get_seed_words()[:-1]:
            if word not in self.wordlist:
                self.seed_e.disable_suggestions()
                return
        self.seed_e.enable_suggestions()


class SeedLayoutDisplay(QVBoxLayout):
    def __init__(
            self,
            seed=None,
            title=None,
            icon=True,
            msg=None,
            options=None,
            passphrase=None,
            parent=None,
            electrum_seed_type=None,
            only_display=False,
            *,
            config: 'SimpleConfig'):
        QVBoxLayout.__init__(self)
        self.parent = parent
        self.config = config
        self.options = options
        self.electrum_seed_type = electrum_seed_type
        self.seed_type = 'bip39'
        if title:
            self.addWidget(WWLabel(title))

        self.seed_e = ButtonsTextEdit()
        self.seed_e.setReadOnly(True)
        self.seed_e.setText(seed)
        self.seed_e.setMaximumHeight(75)

        self.cached_seed_phrases = {'bip39_en_128': seed}

        hbox = QHBoxLayout()
        if icon:
            logo = QLabel()
            logo.setPixmap(QPixmap(icon_path("seed.png"))
                           .scaledToWidth(64, mode=Qt.SmoothTransformation))
            logo.setMaximumWidth(60)
            hbox.addWidget(logo)
        hbox.addWidget(self.seed_e)
        self.addLayout(hbox)
        hbox = QHBoxLayout()
        hbox.addStretch(1)

        self.seed_type_label = QLabel('')
        hbox.addWidget(self.seed_type_label)

        self.is_ext = False
        self.opt_button = None
        if options:
            self.opt_button = EnterButton(_('Options'), self.seed_options)
            hbox.addWidget(self.opt_button)
            self.addLayout(hbox)
            #self.opt_button.setVisible(False)

        seed_types = [
            (value, title) for value, title in (
                ('bip39', _('BIP39 seed')),
                ('electrum', _('Electrum')),
            )
        ]
        seed_type_values = [t[0] for t in seed_types]

        from electrum import mnemonic

        self.languages = list(mnemonic.filenames.items())
        self.bits = (128, 160, 192, 224, 256)
        self.lang_cb = QComboBox()
        self.lang_cb.addItems([' '.join([s.capitalize() for s in x[1][:-4].split('_')]) for x in self.languages])
        self.lang_cb.setCurrentIndex(0)

        self.bits_label = QLabel('Entropy:')
        self.bits_cb = QComboBox()
        self.bits_cb.addItems([str(b) for b in self.bits])
        self.bits_cb.setCurrentIndex(0)

        def on_change():
            i = self.lang_cb.currentIndex()
            l = self.languages[i][0]
            k = 'bip39_' + l + str(self.bit)
            if k not in self.cached_seed_phrases:
                seed = mnemonic.Mnemonic(l).make_bip39_seed(num_bits=self.bit)
                self.cached_seed_phrases[k] = seed
            else:
                seed = self.cached_seed_phrases[k]
            self.seed_warning.setText(seed_warning_msg(seed))
            self.seed_e.setText(seed)
            self.lang = l

        self.lang_cb.currentIndexChanged.connect(on_change)

        def on_change_bits():
            i = self.bits_cb.currentIndex()
            b = self.bits[i]
            k = 'bip39_' + self.lang + str(b)
            if k not in self.cached_seed_phrases:
                seed = mnemonic.Mnemonic(self.lang).make_bip39_seed(num_bits=b)
                self.cached_seed_phrases[k] = seed
            else:
                seed = self.cached_seed_phrases[k]
            self.seed_warning.setText(seed_warning_msg(seed))
            self.seed_e.setText(seed)
            self.bit = b

        self.bits_cb.currentIndexChanged.connect(on_change_bits)

        def f(choices_layout):
            self.seed_type = seed_type_values[choices_layout.selected_index()]
            self.seed_status.setText('')
            if self.seed_type == 'bip39':
                #if self.opt_button:
                #    self.opt_button.setVisible(False)
                self.lang_cb.setVisible(True)
                self.bits_cb.setVisible(True)
                self.bits_label.setVisible(True)
                k = 'bip39_'+self.lang+str(self.bit)
                if k not in self.cached_seed_phrases:
                    seed = mnemonic.Mnemonic(self.lang).make_bip39_seed(num_bits=self.bit)
                    self.cached_seed_phrases[k] = seed
                else:
                    seed = self.cached_seed_phrases[k]
                self.seed_warning.setText(seed_warning_msg(seed))
                self.seed_e.setText(seed)
                self.seed_type = 'bip39'
            else:
                #if self.opt_button:
                #    self.opt_button.setVisible(True)
                self.lang_cb.setVisible(False)
                self.bits_cb.setVisible(False)
                self.bits_label.setVisible(False)
                k = 'electrum'
                if k not in self.cached_seed_phrases:
                    seed = mnemonic.Mnemonic('en').make_seed(seed_type=self.electrum_seed_type)
                    self.cached_seed_phrases[k] = seed
                else:
                    seed = self.cached_seed_phrases[k]
                self.seed_warning.setText(seed_warning_msg(seed))
                self.seed_e.setText(seed)
                self.seed_type = 'electrum'

        checked_index = seed_type_values.index(self.seed_type)
        titles = [t[1] for t in seed_types]
        self.clayout = ChoicesLayout(_('Seed type'), titles, on_clicked=f, checked_index=checked_index)

        if passphrase:
            hbox = QHBoxLayout()
            passphrase_e = QLineEdit()
            passphrase_e.setText(passphrase)
            passphrase_e.setReadOnly(True)
            hbox.addWidget(QLabel(_("Your seed extension is") + ':'))
            hbox.addWidget(passphrase_e)
            self.addLayout(hbox)

        sub_hbox = QHBoxLayout()
        sub_hbox.setStretch(0, 1)

        self.addStretch(1)
        self.seed_status = WWLabel('')
        self.addWidget(self.seed_status)
        self.seed_warning = WWLabel('')
        if msg:
            self.seed_warning.setText(seed_warning_msg(seed))

        self.addWidget(self.seed_warning)

        self.lang = 'en'
        self.bit = 128

    def seed_options(self):
        dialog = QDialog()
        vbox = QVBoxLayout(dialog)

        if 'ext' in self.options:
            cb_ext = QCheckBox(_('Extend this seed with custom words'))
            cb_ext.setChecked(self.is_ext)
            vbox.addWidget(cb_ext)

        vbox.addLayout(self.clayout.layout())
        h_b = QHBoxLayout()
        h_b.addWidget(self.lang_cb)
        help = HelpButton(_('The standard electrum seed phrase is not ' +
                            'BIP39 compliant and will not work with other wallets. ' +
                            'It does however, have some advantages over BIP39 as explained ' +
                            'here:') +
                          '\n\nhttps://electrum.readthedocs.io/en/latest/seedphrase.html\n\n' +
                          _('If you wish to use your seed phrase with other wallets, choose BIP39.'))
        h_b.addWidget(help)

        h_b2 = QHBoxLayout()
        h_b2.addWidget(self.bits_label)
        h_b2.addWidget(self.bits_cb)
        h_b2.setStretch(1, 1)

        vbox.addLayout(h_b)
        vbox.addLayout(h_b2)

        vbox.addLayout(Buttons(OkButton(dialog)))
        if not dialog.exec_():
            return None
        self.is_ext = cb_ext.isChecked() if 'ext' in self.options else False

    def get_seed_words(self):
        return self.seed_e.text().split()

    def get_seed(self):
        return ' '.join(self.get_seed_words())



class SeedLayout(QVBoxLayout):
    def __init__(
            self,
            seed=None,
            title=None,
            icon=True,
            msg=None,
            options=None,
            is_seed=None,
            passphrase=None,
            parent=None,
            for_seed_words=True,
            *,
            config: 'SimpleConfig',
    ):
        QVBoxLayout.__init__(self)
        self.parent = parent
        self.options = options
        self.config = config
        self.seed_type = 'bip39'
        if title:
            self.addWidget(WWLabel(title))
        if seed:  # "read only", we already have the text
            if for_seed_words:
                self.seed_e = ButtonsTextEdit()
            else:  # e.g. xpub
                self.seed_e = ShowQRTextEdit(config=self.config)
            self.seed_e.setReadOnly(True)
            self.seed_e.setText(seed)
        else:  # we expect user to enter text
            assert for_seed_words
            self.seed_e = CompletionTextEdit()
            self.seed_e.setTabChangesFocus(False)  # so that tab auto-completes
            self.is_seed = is_seed
            self.saved_is_seed = self.is_seed
            self.seed_e.textChanged.connect(self.on_edit)
            self.initialize_completer()

        self.seed_e.setMaximumHeight(75)
        hbox = QHBoxLayout()
        if icon:
            logo = QLabel()
            logo.setPixmap(QPixmap(icon_path("seed.png"))
                           .scaledToWidth(64, mode=Qt.SmoothTransformation))
            logo.setMaximumWidth(60)
            hbox.addWidget(logo)
        hbox.addWidget(self.seed_e)
        self.addLayout(hbox)
        hbox = QHBoxLayout()
        hbox.addStretch(1)

        self.seed_type_label = QLabel('')
        hbox.addWidget(self.seed_type_label)

        seed_types = [
            (value, title) for value, title in (
                ('bip39', _('BIP39 seed')),
                ('electrum', _('Electrum')),
                # ('slip39', _('SLIP39 seed')),
            )
            #if value in self.options or value == 'electrum'
        ]
        seed_type_values = [t[0] for t in seed_types]

        if len(seed_types) >= 2:
            def f(choices_layout):
                self.seed_type = seed_type_values[choices_layout.selected_index()]
                self.is_seed = (lambda x: bool(x)) if self.seed_type != 'bip39' else self.saved_is_seed
                self.slip39_current_mnemonic_invalid = None
                self.seed_status.setText('')
                #self.on_edit()
                self.update_share_buttons()
                #self.initialize_completer()
                self.seed_warning.setText(msg)

            checked_index = seed_type_values.index(self.seed_type)
            titles = [t[1] for t in seed_types]
            self.clayout = ChoicesLayout(_('Seed type'), titles, on_clicked=f, checked_index=checked_index)
            hbox.addLayout(self.clayout.layout())

        # options
        self.is_ext = False
        if options:
            opt_button = EnterButton(_('Options'), self.seed_options)
            hbox.addWidget(opt_button)
            self.addLayout(hbox)
        if passphrase:
            hbox = QHBoxLayout()
            passphrase_e = QLineEdit()
            passphrase_e.setText(passphrase)
            passphrase_e.setReadOnly(True)
            hbox.addWidget(QLabel(_("Your seed extension is") + ':'))
            hbox.addWidget(passphrase_e)
            self.addLayout(hbox)

        # slip39 shares
        self.slip39_mnemonic_index = 0
        self.slip39_mnemonics = [""]
        self.slip39_seed = None
        self.slip39_current_mnemonic_invalid = None
        hbox = QHBoxLayout()
        hbox.addStretch(1)
        self.prev_share_btn = QPushButton(_("Previous share"))
        self.prev_share_btn.clicked.connect(self.on_prev_share)
        hbox.addWidget(self.prev_share_btn)
        self.next_share_btn = QPushButton(_("Next share"))
        self.next_share_btn.clicked.connect(self.on_next_share)
        hbox.addWidget(self.next_share_btn)
        self.update_share_buttons()
        self.addLayout(hbox)

        self.addStretch(1)
        self.seed_status = WWLabel('')
        self.addWidget(self.seed_status)
        self.seed_warning = WWLabel('')
        #if msg:
        #    self.seed_warning.setText(seed_warning_msg(seed))
        self.addWidget(self.seed_warning)

        self.lang = 'en'

    def initialize_completer(self):
        if self.seed_type != 'slip39':
            bip39_english_list = Mnemonic('en').wordlist
            old_list = old_mnemonic.wordlist
            only_old_list = set(old_list) - set(bip39_english_list)
            self.wordlist = list(bip39_english_list) + list(only_old_list)  # concat both lists
            self.wordlist.sort()

            class CompleterDelegate(QStyledItemDelegate):
                def initStyleOption(self, option, index):
                    super().initStyleOption(option, index)
                    # Some people complained that due to merging the two word lists,
                    # it is difficult to restore from a metal backup, as they planned
                    # to rely on the "4 letter prefixes are unique in bip39 word list" property.
                    # So we color words that are only in old list.
                    if option.text in only_old_list:
                        # yellow bg looks ~ok on both light/dark theme, regardless if (un)selected
                        option.backgroundBrush = ColorScheme.YELLOW.as_color(background=True)

            delegate = CompleterDelegate(self.seed_e)
        else:
            self.wordlist = list(slip39.get_wordlist())
            delegate = None

        self.completer = QCompleter(self.wordlist)
        if delegate:
            self.completer.popup().setItemDelegate(delegate)
        self.seed_e.set_completer(self.completer)

    def get_seed_words(self):
        return self.seed_e.text().split()

    def get_seed(self):
        if self.seed_type != 'slip39':
            return ' '.join(self.get_seed_words())
        else:
            return self.slip39_seed

    def on_edit(self):
        s = ' '.join(self.get_seed_words())
        b = self.is_seed(s)

        from electrum.keystore import bip39_is_checksum_valid
        from electrum.mnemonic import Wordlist, filenames

        lang = ''
        is_checksum = is_wordlist = False
        for type, file in filenames.items():
            word_list = Wordlist.from_file(file)
            is_checksum, is_wordlist = bip39_is_checksum_valid(s, wordlist=word_list)
            if not is_wordlist and len(s.split()) > 1:
                is_checksum, is_wordlist = bip39_is_checksum_valid(' '.join(s.split()[:-1]), wordlist=word_list)
            if is_wordlist:
                lang = type
                break

        if lang and lang != self.lang:
            if lang == 'en':
                bip39_english_list = Mnemonic('en').wordlist
                old_list = old_mnemonic.wordlist
                only_old_list = set(old_list) - set(bip39_english_list)
                self.wordlist = list(bip39_english_list) + list(only_old_list)  # concat both lists
            else:
                self.wordlist = list(Mnemonic(lang).wordlist)
            self.wordlist.sort()
            self.completer.model().setStringList(self.wordlist)
            self.lang = lang

        if self.seed_type == 'bip39':
            status = ('checksum: ' + ('ok' if is_checksum else 'failed')) if is_wordlist else 'unknown wordlist'
            label = 'BIP39 - ' + lang + ' (%s)'%status

        elif self.seed_type == 'slip39':
            self.slip39_mnemonics[self.slip39_mnemonic_index] = s
            try:
                slip39.decode_mnemonic(s)
            except slip39.Slip39Error as e:
                share_status = str(e)
                current_mnemonic_invalid = True
            else:
                share_status = _('Valid.')
                current_mnemonic_invalid = False

            label = _('SLIP39 share') + ' #%d: %s' % (self.slip39_mnemonic_index + 1, share_status)

            # No need to process mnemonics if the current mnemonic remains invalid after editing.
            if not (self.slip39_current_mnemonic_invalid and current_mnemonic_invalid):
                self.slip39_seed, seed_status = slip39.process_mnemonics(self.slip39_mnemonics)
                self.seed_status.setText(seed_status)
            self.slip39_current_mnemonic_invalid = current_mnemonic_invalid

            b = self.slip39_seed is not None
            self.update_share_buttons()
        else:
            t = seed_type(s)
            label = _('Seed Type') + ': ' + t if t else ''

        self.seed_type_label.setText(label)
        self.parent.next_button.setEnabled(b)

        # disable suggestions if user already typed an unknown word
        for word in self.get_seed_words()[:-1]:
            if word not in self.wordlist:
                self.seed_e.disable_suggestions()
                return
        self.seed_e.enable_suggestions()

    def update_share_buttons(self):
        if self.seed_type != 'slip39':
            self.prev_share_btn.hide()
            self.next_share_btn.hide()
            return

        finished = self.slip39_seed is not None
        self.prev_share_btn.show()
        self.next_share_btn.show()
        self.prev_share_btn.setEnabled(self.slip39_mnemonic_index != 0)
        self.next_share_btn.setEnabled(
            # already pressed "prev" and undoing that:
            self.slip39_mnemonic_index < len(self.slip39_mnemonics) - 1
            # finished entering latest share and starting new one:
            or (bool(self.seed_e.text().strip()) and not self.slip39_current_mnemonic_invalid and not finished)
        )

    def on_prev_share(self):
        if not self.slip39_mnemonics[self.slip39_mnemonic_index]:
            del self.slip39_mnemonics[self.slip39_mnemonic_index]

        self.slip39_mnemonic_index -= 1
        self.seed_e.setText(self.slip39_mnemonics[self.slip39_mnemonic_index])
        self.slip39_current_mnemonic_invalid = None

    def on_next_share(self):
        if not self.slip39_mnemonics[self.slip39_mnemonic_index]:
            del self.slip39_mnemonics[self.slip39_mnemonic_index]
        else:
            self.slip39_mnemonic_index += 1

        if len(self.slip39_mnemonics) <= self.slip39_mnemonic_index:
            self.slip39_mnemonics.append("")
            self.seed_e.setFocus()
        self.seed_e.setText(self.slip39_mnemonics[self.slip39_mnemonic_index])
        self.slip39_current_mnemonic_invalid = None


class KeysLayout(QVBoxLayout):
    def __init__(
            self,
            parent=None,
            header_layout=None,
            is_valid=None,
            allow_multi=False,
            *,
            config: 'SimpleConfig',
    ):
        QVBoxLayout.__init__(self)
        self.parent = parent
        self.is_valid = is_valid
        self.text_e = ScanQRTextEdit(allow_multi=allow_multi, config=config)
        self.text_e.textChanged.connect(self.on_edit)
        if isinstance(header_layout, str):
            self.addWidget(WWLabel(header_layout))
        else:
            self.addLayout(header_layout)
        self.addWidget(self.text_e)

    def get_text(self):
        return self.text_e.text()

    def on_edit(self):
        valid = False
        try:
            valid = self.is_valid(self.get_text())
        except Exception as e:
            self.parent.next_button.setToolTip(f'{_("Error")}: {str(e)}')
        else:
            self.parent.next_button.setToolTip('')
        self.parent.next_button.setEnabled(valid)


class SeedDialog(WindowModalDialog):

    def __init__(self, parent, seed, passphrase, *, config: 'SimpleConfig'):
        WindowModalDialog.__init__(self, parent, ('Electrum Ravencoin - ' + _('Seed')))
        self.setMinimumWidth(400)
        vbox = QVBoxLayout(self)
        title =  _("Your wallet generation seed is:")
        slayout = SeedLayoutDisplay(
            title=title,
            seed=seed,
            msg=True,
            passphrase=passphrase,
            config=config,
            only_display=True
        )
        vbox.addLayout(slayout)
        vbox.addLayout(Buttons(CloseButton(self)))
