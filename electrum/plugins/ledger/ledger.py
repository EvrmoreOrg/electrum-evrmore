import datetime
import logging
import threading
import time
from struct import pack, unpack
import hashlib
import sys
import traceback
from typing import Optional, Tuple

from electrum import ecc, constants
from electrum import bip32
from electrum.crypto import hash_160
from electrum.evrmore import int_to_hex, var_int, is_segwit_script_type, is_b58_address
from electrum.bip32 import BIP32Node, convert_bip32_intpath_to_strpath
from electrum.i18n import _
from electrum.keystore import Hardware_KeyStore
from electrum.transaction import Transaction, PartialTransaction, PartialTxInput, PartialTxOutput
from electrum.wallet import Standard_Wallet
from electrum.util import bfh, bh2u, versiontuple, UserFacingException
from electrum.base_wizard import ScriptTypeNotSupported
from electrum.logging import get_logger
from electrum.plugin import runs_in_hwd_thread, Device

from ..hw_wallet import HW_PluginBase, HardwareClientBase
from ..hw_wallet.plugin import is_any_tx_output_on_change_branch, validate_op_return_output, LibraryFoundButUnusable

_logger = get_logger(__name__)

try:
    import hid
    from btchip.btchipComm import HIDDongleHIDAPI, DongleWait
    from btchip.btchip import btchip
    from btchip.btchipUtils import compress_public_key, format_transaction, get_regular_input_script, \
        get_p2sh_input_script
    from btchip.bitcoinTransaction import bitcoinTransaction
    from btchip.btchipFirmwareWizard import checkFirmware, updateFirmware
    from btchip.btchipException import BTChipException
    from btchip.bitcoinVarint import writeVarint

    BTCHIP = True
    BTCHIP_DEBUG = False
except ImportError as e:
    if not (isinstance(e, ModuleNotFoundError) and e.name == 'btchip'):
        _logger.exception('error importing ledger plugin deps')
    BTCHIP = False

MSG_NEEDS_FW_UPDATE_GENERIC = _('Firmware version too old. Please update at') + \
                              ' https://www.ledgerwallet.com'
MSG_NEEDS_FW_UPDATE_SEGWIT = _('Firmware version (or "evrmore" app) too old for Segwit support. Please update at') + \
                             ' https://www.ledgerwallet.com'
MULTI_OUTPUT_SUPPORT = '1.1.4'
SEGWIT_SUPPORT = '1.1.10'
SEGWIT_SUPPORT_SPECIAL = '1.0.4'
SEGWIT_TRUSTEDINPUTS = '1.4.0'


def test_pin_unlocked(func):
    """Function decorator to test the Ledger for being unlocked, and if not,
    raise a human-readable exception.
    """

    def catch_exception(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except BTChipException as e:
            if e.sw == 0x6982:
                raise UserFacingException(_('Your Ledger is locked. Please unlock it.'))
            else:
                raise

    return catch_exception

class ModifiedBTChip(btchip):
    def __init__(self, dongle):
        self.dongle = dongle
        self.needKeyCache = False
        try:
            firmware = self.getFirmwareVersion()['version']
            self.multiOutputSupported = tuple(map(int, (firmware.split(".")))) >= (1, 1, 4)
            if self.multiOutputSupported:
                self.scriptBlockLength = 50
            else:
                self.scriptBlockLength = 255
        except Exception:
            pass

class Ledger_Client(HardwareClientBase):
    def __init__(self, hidDevice, *, product_key: Tuple[int, int],
                 plugin: HW_PluginBase):
        HardwareClientBase.__init__(self, plugin=plugin)
        self.dongleObject = ModifiedBTChip(hidDevice)
        self.preflightDone = False
        self._product_key = product_key
        self._soft_device_id = None

    def is_pairable(self):
        return True

    @runs_in_hwd_thread
    def close(self):
        self.dongleObject.dongle.close()

    def is_initialized(self):
        return True

    @runs_in_hwd_thread
    def get_soft_device_id(self):
        if self._soft_device_id is None:
            # modern ledger can provide xpub without user interaction
            # (hw1 would prompt for PIN)
            if not self.is_hw1():
                self._soft_device_id = self.request_root_fingerprint_from_device()
        return self._soft_device_id

    def is_hw1(self) -> bool:
        return self._product_key[0] == 0x2581

    def device_model_name(self):
        return LedgerPlugin.device_name_from_product_key(self._product_key)

    @runs_in_hwd_thread
    def has_usable_connection_with_device(self):
        try:
            self.dongleObject.getFirmwareVersion()
        except BaseException:
            return False
        return True

    @runs_in_hwd_thread
    @test_pin_unlocked
    def get_xpub(self, bip32_path, xtype):
        self.checkDevice()
        # bip32_path is of the form 44'/0'/1'
        # S-L-O-W - we don't handle the fingerprint directly, so compute
        # it manually from the previous node
        # This only happens once so it's bearable
        # self.get_client() # prompt for the PIN before displaying the dialog if necessary
        # self.handler.show_message("Computing master public key")
        if xtype in ['p2wpkh', 'p2wsh'] and not self.supports_native_segwit():
            raise UserFacingException(MSG_NEEDS_FW_UPDATE_SEGWIT)
        if xtype in ['p2wpkh-p2sh', 'p2wsh-p2sh'] and not self.supports_segwit():
            raise UserFacingException(MSG_NEEDS_FW_UPDATE_SEGWIT)
        bip32_path = bip32.normalize_bip32_derivation(bip32_path)
        bip32_intpath = bip32.convert_bip32_path_to_list_of_uint32(bip32_path)
        bip32_path = bip32_path[2:]  # cut off "m/"
        if len(bip32_intpath) >= 1:
            prevPath = bip32.convert_bip32_intpath_to_strpath(bip32_intpath[:-1])[2:]
            nodeData = self.dongleObject.getWalletPublicKey(prevPath)
            publicKey = compress_public_key(nodeData['publicKey'])
            fingerprint_bytes = hash_160(publicKey)[0:4]
            childnum_bytes = bip32_intpath[-1].to_bytes(length=4, byteorder="big")
        else:
            fingerprint_bytes = bytes(4)
            childnum_bytes = bytes(4)
        nodeData = self.dongleObject.getWalletPublicKey(bip32_path)
        publicKey = compress_public_key(nodeData['publicKey'])
        depth = len(bip32_intpath)
        return BIP32Node(xtype=xtype,
                         eckey=ecc.ECPubkey(bytes(publicKey)),
                         chaincode=nodeData['chainCode'],
                         depth=depth,
                         fingerprint=fingerprint_bytes,
                         child_number=childnum_bytes).to_xpub()

    def has_detached_pin_support(self, client: 'btchip'):
        try:
            client.getVerifyPinRemainingAttempts()
            return True
        except BTChipException as e:
            if e.sw == 0x6d00:
                return False
            raise e

    def is_pin_validated(self, client: 'btchip'):
        try:
            # Invalid SET OPERATION MODE to verify the PIN status
            client.dongle.exchange(bytearray([0xe0, 0x26, 0x00, 0x00, 0x01, 0xAB]))
        except BTChipException as e:
            if (e.sw == 0x6982):
                return False
            if (e.sw == 0x6A80):
                return True
            raise e

    def supports_multi_output(self):
        return self.multiOutputSupported

    def supports_segwit(self):
        return self.segwitSupported

    def supports_native_segwit(self):
        return self.nativeSegwitSupported

    def supports_segwit_trustedInputs(self):
        return self.segwitTrustedInputs

    @runs_in_hwd_thread
    def perform_hw1_preflight(self):
        try:
            firmwareInfo = self.dongleObject.getFirmwareVersion()
            firmware = firmwareInfo['version']
            self.multiOutputSupported = versiontuple(firmware) >= versiontuple(MULTI_OUTPUT_SUPPORT)
            self.nativeSegwitSupported = versiontuple(firmware) >= versiontuple(SEGWIT_SUPPORT)
            self.segwitSupported = self.nativeSegwitSupported or (
                    firmwareInfo['specialVersion'] == 0x20 and versiontuple(firmware) >= versiontuple(
                SEGWIT_SUPPORT_SPECIAL))
            self.segwitTrustedInputs = versiontuple(firmware) >= versiontuple(SEGWIT_TRUSTEDINPUTS)

            if not checkFirmware(firmwareInfo):
                self.close()
                raise UserFacingException(MSG_NEEDS_FW_UPDATE_GENERIC)
            try:
                self.dongleObject.getOperationMode()
            except BTChipException as e:
                if (e.sw == 0x6985):
                    self.close()
                    self.handler.get_setup()
                    # Acquire the new client on the next run
                else:
                    raise e
            if self.has_detached_pin_support(self.dongleObject) and not self.is_pin_validated(self.dongleObject):
                assert self.handler, "no handler for client"
                remaining_attempts = self.dongleObject.getVerifyPinRemainingAttempts()
                if remaining_attempts != 1:
                    msg = _("Enter your Ledger PIN - remaining attempts : {}").format(str(remaining_attempts))
                else:
                    msg = _("Enter your Ledger PIN - WARNING : LAST ATTEMPT. If the PIN is not correct, the dongle "
                            "will be wiped.")
                confirmed, p, pin = self.password_dialog(msg)
                if not confirmed:
                    raise UserFacingException(
                        _('Aborted by user - please unplug the dongle and plug it again before retrying'))
                pin = pin.encode()
                self.dongleObject.verifyPin(pin)
                self.dongleObject.setAlternateCoinVersions(constants.net.ADDRTYPE_P2PKH, constants.net.ADDRTYPE_P2SH)
        except BTChipException as e:
            if (e.sw == 0x6faa):
                raise UserFacingException(_("Dongle is temporarily locked - please unplug it and replug it again"))
            if ((e.sw & 0xFFF0) == 0x63c0):
                raise UserFacingException(_("Invalid PIN - please unplug the dongle and plug it again before retrying"))
            if e.sw == 0x6f00 and e.message == 'Invalid channel':
                # based on docs 0x6f00 might be a more general error, hence we also compare message to be sure
                raise UserFacingException(_("Invalid channel.\n"
                                            "Please make sure that 'Browser support' is disabled on your device."))
            raise e

    @runs_in_hwd_thread
    def checkDevice(self):
        if not self.preflightDone:
            try:
                self.perform_hw1_preflight()
            except BTChipException as e:
                if (e.sw == 0x6d00 or e.sw == 0x6700 or e.sw == 0x6702):
                    raise UserFacingException(_("Device not in evrmore mode")) from e
                raise e
            self.preflightDone = True

    def password_dialog(self, msg=None):
        response = self.handler.get_word(msg)
        if response is None:
            return False, None, None
        return True, response, response


class AbstractTracker:
    def parsed_string(self):
        raise NotImplementedError()


class SigningTracker(AbstractTracker):
    def __init__(self, total_inputs):
        self.total_inputs = total_inputs
        self.ticker = 0
        self.start = time.time()
        self.last_ETA = _('Calculating...')

    def tick(self):
        self.ticker += 1

    def parsed_string(self):
        line = _('Signing transaction data...\n\n')

        line += _('Input: {}/{}\n\n').format(
            self.ticker,
            self.total_inputs)

        tx_delta = self.ticker
        sec_delta = time.time() - self.start
        tx_left = self.total_inputs - self.ticker

        eta = self.last_ETA
        if tx_delta != 0 and sec_delta != 0:
            secs = sec_delta / tx_delta * tx_left
            eta = str(datetime.timedelta(seconds=round(secs)))

        line += _('ETA: {}').format(eta)
        self.last_ETA = eta
        return line


class ParsingTracker(AbstractTracker):
    def __init__(self, total_tx):
        self.total_transactions = total_tx
        self.send_warning = False
        if self.total_transactions > 2000:  # Arbitrary number
            self.send_warning = True

        self.ticker = 0

        self.tx_count = 0
        self.tot_tx = 0
        self.in_count = 0
        self.tot_in = 0
        self.out_count = 0
        self.tot_out = 0

        self.start_time = time.time()
        self.last_ETA = _('Computing...')

    def set_tx_amt(self, amt):
        self.tot_tx = amt

    def tick_tx(self):
        self.tx_count += 1
        self.in_count = 0
        self.tot_in = 0
        self.out_count = 0
        self.tot_out = 0

    def tick_in(self):
        self.log_info()
        self.in_count += 1

    def tick_out(self):
        self.log_info()
        self.out_count += 1

    def set_io_amt(self, ins, outs):
        self.tot_in = ins
        self.tot_out = outs

    def log_info(self):
        self.ticker += 1
        if self.ticker % 500 == 0:
            _logger.info(self.parsed_string())

    def parsed_string(self):
        line = _('Parsing transaction data...\n\n')
        if self.send_warning:
            line += _('It looks there is a lot of data to parse.\n'
                      'This occurs if you receive EVR from transactions\n'
                      'with a lot of other outputs such as mining directly\n'
                      'to your ledger.\n'
                      'These long wait times are due to hardware limitations.\n'
                      'You may want to turn off your ledger\'s auto-lock to prevent\n'
                      'This transaction from failing due to your ledger locking.\n'
                      'These settings can be found in:\n'
                      'Settings > Security > Screen Saver > Off\n'
                      'Settings > Security > Pin Lock > Off\n'
                      'Once this transaction is complete and sent, your EVR will\n'
                      'be consolidated and transaction parsing times will be\n'
                      'unnoticeable in the future.\n\n')
        line += _('Tx: {}/{}\nInputs: {}/{}\nOutputs: {}/{}\n').format(
            self.tx_count,
            self.tot_tx,
            self.in_count,
            self.tot_in,
            self.out_count,
            self.tot_out)
        line += _('Total Completion: {}/{}\n').format(self.ticker, self.total_transactions)

        tx_delta = self.ticker
        sec_delta = time.time() - self.start_time
        tx_left = self.total_transactions - self.ticker

        eta = self.last_ETA

        if tx_delta != 0 and sec_delta != 0:
            secs = sec_delta / tx_delta * tx_left
            eta = str(datetime.timedelta(seconds=round(secs)))

        line += _('ETA: {}').format(eta)
        self.last_ETA = eta
        return line

# I am not sure if python booleans are atomic
class AtomicBoolean:
    def __init__(self):
        self.lock = threading.Lock()
        self.boolean = False

    def set_true(self):
        with self.lock:
            self.boolean = True

    def set_false(self):
        with self.lock:
            self.boolean = False

    def get_value(self):
        with self.lock:
            return self.boolean


class BTChipWrapper:

    def __init__(self, original):
        self.dongle = original.dongle

    # Same code as normal with counters stuck in
    def getTrustedInput(self, atomic_boolean: AtomicBoolean, ui_tracker: ParsingTracker, transaction, index):
        result = {}
        # Header
        apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x00, 0x00]
        params = bytearray.fromhex("%.8x" % index)
        params.extend(transaction.version)
        writeVarint(len(transaction.inputs), params)
        apdu.append(len(params))
        apdu.extend(params)
        self.dongle.exchange(bytearray(apdu))
        # Each input

        ui_tracker.set_io_amt(len(transaction.inputs), len(transaction.outputs))

        for trinput in transaction.inputs:
            if atomic_boolean.get_value():
                raise UserWarning()
            apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x80, 0x00]
            params = bytearray(trinput.prevOut)
            writeVarint(len(trinput.script), params)
            apdu.append(len(params))
            apdu.extend(params)
            self.dongle.exchange(bytearray(apdu))
            offset = 0
            while True:
                blockLength = 251
                if ((offset + blockLength) < len(trinput.script)):
                    dataLength = blockLength
                else:
                    dataLength = len(trinput.script) - offset
                params = bytearray(trinput.script[offset: offset + dataLength])
                if ((offset + dataLength) == len(trinput.script)):
                    params.extend(trinput.sequence)
                apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x80, 0x00, len(params)]
                apdu.extend(params)
                self.dongle.exchange(bytearray(apdu))
                offset += dataLength
                if (offset >= len(trinput.script)):
                    break

            ui_tracker.tick_in()

        # Number of outputs
        apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x80, 0x00]
        params = []
        writeVarint(len(transaction.outputs), params)
        apdu.append(len(params))
        apdu.extend(params)
        self.dongle.exchange(bytearray(apdu))
        # Each output
        indexOutput = 0
        for troutput in transaction.outputs:
            if atomic_boolean.get_value():
                raise UserWarning()
            apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x80, 0x00]
            params = bytearray(troutput.amount)
            writeVarint(len(troutput.script), params)
            apdu.append(len(params))
            apdu.extend(params)
            self.dongle.exchange(bytearray(apdu))
            offset = 0
            while (offset < len(troutput.script)):
                blockLength = 255
                if ((offset + blockLength) < len(troutput.script)):
                    dataLength = blockLength
                else:
                    dataLength = len(troutput.script) - offset
                apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x80, 0x00, dataLength]
                apdu.extend(troutput.script[offset: offset + dataLength])
                self.dongle.exchange(bytearray(apdu))
                offset += dataLength

            ui_tracker.tick_out()

        # Locktime
        apdu = [btchip.BTCHIP_CLA, btchip.BTCHIP_INS_GET_TRUSTED_INPUT, 0x80, 0x00, len(transaction.lockTime)]
        apdu.extend(transaction.lockTime)
        response = self.dongle.exchange(bytearray(apdu))
        result['trustedInput'] = True
        result['value'] = response
        return result


class Ledger_KeyStore(Hardware_KeyStore):
    hw_type = 'ledger'
    device = 'Ledger'

    plugin: 'LedgerPlugin'

    def __init__(self, d):
        Hardware_KeyStore.__init__(self, d)
        self.signing = False
        self.cfg = d.get('cfg', {'mode': 0})

    def dump(self):
        obj = Hardware_KeyStore.dump(self)
        obj['cfg'] = self.cfg
        return obj

    def get_client_dongle_object(self, *, client: Optional['Ledger_Client'] = None) -> 'btchip':
        if client is None:
            client = self.get_client()
        return client.dongleObject

    def give_error(self, message):
        _logger.info(message)
        if not self.signing:
            self.handler.show_error(message)
        else:
            self.signing = False
        raise UserFacingException(message)

    def set_and_unset_signing(func):
        """Function decorator to set and unset self.signing."""

        def wrapper(self, *args, **kwargs):
            try:
                self.signing = True
                return func(self, *args, **kwargs)
            finally:
                self.signing = False

        return wrapper

    def decrypt_message(self, pubkey, message, password):
        raise UserFacingException(_('Encryption and decryption are currently not supported for {}').format(self.device))

    @runs_in_hwd_thread
    @test_pin_unlocked
    @set_and_unset_signing
    def sign_message(self, sequence, message, password, script_type):
        raise UserFacingException(_('Signing messages is not currently supported on ledger devices'))
        message = message.encode('utf8')
        message_hash = hashlib.sha256(message).hexdigest().upper()
        # prompt for the PIN before displaying the dialog if necessary
        client_electrum = self.get_client()
        client_ledger = self.get_client_dongle_object(client=client_electrum)
        address_path = self.get_derivation_prefix()[2:] + "/%d/%d"%sequence
        self.handler.show_message("Signing message ...\r\nMessage hash: "+message_hash)
        try:
            info = client_ledger.signMessagePrepare(address_path, message)
            pin = ""
            if info['confirmationNeeded']:
                # do the authenticate dialog and get pin:
                pin = self.handler.get_auth(info, client=client_electrum)
                if not pin:
                    raise UserWarning(_('Cancelled by user'))
                pin = str(pin).encode()
            signature = client_ledger.signMessageSign(pin)
        except BTChipException as e:
            if e.sw == 0x6a80:
                self.give_error("Unfortunately, this message cannot be signed by the Ledger wallet. "
                                "Only alphanumerical messages shorter than 140 characters are supported. "
                                "Please remove any extra characters (tab, carriage return) and retry.")
            elif e.sw == 0x6985:  # cancelled by user
                return b''
            elif e.sw == 0x6982:
                raise  # pin lock. decorator will catch it
            else:
                self.give_error(e)
        except UserWarning:
            self.handler.show_error(_('Cancelled by user'))
            return b''
        except Exception as e:
            self.give_error(e)
        finally:
            self.handler.finished()
        # Parse the ASN.1 signature
        rLength = signature[3]
        r = signature[4: 4 + rLength]
        sLength = signature[4 + rLength + 1]
        s = signature[4 + rLength + 2:]
        if rLength == 33:
            r = r[1:]
        if sLength == 33:
            s = s[1:]
        # And convert it

        # Pad r and s points with 0x00 bytes when the point is small to get valid signature.
        r_padded = bytes([0x00]) * (32 - len(r)) + r
        s_padded = bytes([0x00]) * (32 - len(s)) + s

        return bytes([27 + 4 + (signature[0] & 0x01)]) + r_padded + s_padded

    @runs_in_hwd_thread
    @test_pin_unlocked
    @set_and_unset_signing
    def sign_transaction(self, tx, password):
        if tx.is_complete():
            return
        inputs = []
        inputsPaths = []
        chipInputs = []
        redeemScripts = []
        changePath = ""
        output = None
        p2shTransaction = False
        segwitTransaction = False
        pin = ""
        # prompt for the PIN before displaying the dialog if necessary
        client_electrum = self.get_client()
        assert client_electrum
        client_ledger = self.get_client_dongle_object(client=client_electrum)

        # Fetch inputs of the transaction to sign
        for txin in tx.inputs():
            if txin.is_coinbase_input():
                self.give_error(_("Coinbase not supported"))  # should never happen

            if txin.script_type in ['p2sh']:
                p2shTransaction = True

            if txin.script_type in ['p2wpkh-p2sh', 'p2wsh-p2sh']:
                if not client_electrum.supports_segwit():
                    self.give_error(MSG_NEEDS_FW_UPDATE_SEGWIT)
                segwitTransaction = True

            if txin.script_type in ['p2wpkh', 'p2wsh']:
                if not client_electrum.supports_native_segwit():
                    self.give_error(MSG_NEEDS_FW_UPDATE_SEGWIT)
                segwitTransaction = True

            my_pubkey, full_path = self.find_my_pubkey_in_txinout(txin)
            if not full_path:
                self.give_error(_("No matching pubkey for sign_transaction"))  # should never happen
            full_path = convert_bip32_intpath_to_strpath(full_path)[2:]

            redeemScript = Transaction.get_preimage_script(txin, client_electrum.handler.get_wallet())
            txin_prev_tx = txin.utxo
            if txin_prev_tx is None and not txin.is_segwit():
                raise UserFacingException(_('Missing previous tx for legacy input.'))
            txin_prev_tx_raw = txin_prev_tx.serialize() if txin_prev_tx else None
            inputs.append([txin_prev_tx_raw,
                           txin.prevout.out_idx,
                           redeemScript,
                           txin.prevout.txid.hex(),
                           my_pubkey,
                           txin.nsequence,
                           txin.value_sats()])
            inputsPaths.append(full_path)

        # Sanity check
        if p2shTransaction:
            for txin in tx.inputs():
                if txin.script_type != 'p2sh':
                    self.give_error(_(
                        "P2SH / regular input mixed in same transaction not supported"))  # should never happen

        txOutput = var_int(len(tx.outputs()))
        for o in tx.outputs():
            #if o.asset:
            #    self.give_error(_("Sending assets with ledger is currently not supported."))
            txOutput += int_to_hex(0 if o.asset else o.value, 8)
            script = o.scriptpubkey.hex()
            txOutput += var_int(len(script) // 2)
            txOutput += script
            if o.asset:
                raise UserFacingException(_('Ledger does not currently support asset transactions'))

        txOutput = bfh(txOutput)

        if not client_electrum.supports_multi_output():
            if len(tx.outputs()) > 2:
                self.give_error(_("Transaction with more than 2 outputs not supported"))
        for txout in tx.outputs():
            if client_electrum.is_hw1() and txout.address and not is_b58_address(txout.address):
                self.give_error(_("This {} device can only send to base58 addresses.").format(self.device))
            if not txout.address:
                if client_electrum.is_hw1():
                    self.give_error(_("Only address outputs are supported by {}").format(self.device))
                # note: max_size based on https://github.com/LedgerHQ/ledger-app-btc/commit/3a78dee9c0484821df58975803e40d58fbfc2c38#diff-c61ccd96a6d8b54d48f54a3bc4dfa7e2R26
                validate_op_return_output(txout, max_size=190)

        # Output "change" detection
        # - only one output and one change is authorized (for hw.1 and nano)
        # - at most one output can bypass confirmation (~change) (for all)
        if not p2shTransaction:
            has_change = False
            any_output_on_change_branch = is_any_tx_output_on_change_branch(tx)
            for txout in tx.outputs():
                if txout.is_mine and len(tx.outputs()) > 1 \
                        and not has_change:
                    # prioritise hiding outputs on the 'change' branch from user
                    # because no more than one change address allowed
                    if txout.is_change == any_output_on_change_branch:
                        my_pubkey, changePath = self.find_my_pubkey_in_txinout(txout)
                        assert changePath
                        changePath = convert_bip32_intpath_to_strpath(changePath)[2:]
                        has_change = True
                    else:
                        output = txout.address
                else:
                    output = txout.address

        # self.handler.show_message(_("Confirm Transaction on your Ledger device..."))

        def get_parse_count():
            c = 0
            for utxo in inputs:
                if not segwitTransaction and not p2shTransaction:
                    txtmp = bitcoinTransaction(bfh(utxo[0]))
                    c += len(txtmp.inputs) + len(txtmp.outputs)
            return c

        ui_tracker = ParsingTracker(get_parse_count())
        ui_tracker.set_tx_amt(len(inputs))
        atomic_b = AtomicBoolean()
        self.handler.finished()
        self.handler.get_ui(_('Parsing Transaction...'), atomic_b, ui_tracker)

        try:
            # Get trusted inputs from the original transactions
            for utxo in inputs:

                if atomic_b.get_value():
                    raise UserWarning()
                ui_tracker.tick_tx()

                sequence = int_to_hex(utxo[5], 4)
                if segwitTransaction and not client_electrum.supports_segwit_trustedInputs():
                    tmp = bfh(utxo[3])[::-1]
                    tmp += bfh(int_to_hex(utxo[1], 4))
                    tmp += bfh(int_to_hex(utxo[6], 8))  # txin['value']
                    chipInputs.append({'value': tmp, 'witness': True, 'sequence': sequence})
                    redeemScripts.append(bfh(utxo[2]))
                elif (not p2shTransaction) or client_electrum.supports_multi_output():
                    txtmp = bitcoinTransaction(bfh(utxo[0]))
                    trustedInput = BTChipWrapper(client_ledger).getTrustedInput(atomic_b, ui_tracker, txtmp, utxo[1])
                    trustedInput['sequence'] = sequence
                    if segwitTransaction:
                        trustedInput['witness'] = True
                    chipInputs.append(trustedInput)
                    if p2shTransaction or segwitTransaction:
                        redeemScripts.append(bfh(utxo[2]))
                    else:
                        redeemScripts.append(txtmp.outputs[utxo[1]].script)
                else:
                    tmp = bfh(utxo[3])[::-1]
                    tmp += bfh(int_to_hex(utxo[1], 4))
                    chipInputs.append({'value': tmp, 'sequence': sequence})
                    redeemScripts.append(bfh(utxo[2]))

            self.handler.finished_ui()
            self.handler.finished()
            self.handler.show_message(_("Confirm Transaction on your Ledger device..."))
            # Sign all inputs
            firstTransaction = True
            inputIndex = 0
            rawTx = tx.serialize_to_network()
            client_ledger.enableAlternate2fa(False)

            ui_tracker = SigningTracker(len(inputs))
            atomic_b = AtomicBoolean()

            if segwitTransaction:
                client_ledger.startUntrustedTransaction(True, inputIndex,
                                                        chipInputs, redeemScripts[inputIndex], version=tx.version)
                # we don't set meaningful outputAddress, amount and fees
                # as we only care about the alternateEncoding==True branch
                outputData = client_ledger.finalizeInput(b'', 0, 0, changePath, bfh(rawTx))
                outputData['outputData'] = txOutput
                if outputData['confirmationNeeded']:
                    outputData['address'] = output
                    self.handler.finished_ui()
                    self.handler.finished()
                    # do the authenticate dialog and get pin:
                    pin = self.handler.get_auth(outputData, client=client_electrum)
                    if not pin:
                        raise UserWarning()
                    self.handler.show_message(_("Confirmed. Signing Transaction..."))
                while inputIndex < len(inputs):
                    if inputIndex == 0:
                        self.handler.finished()
                        self.handler.get_ui(_('Signing Transaction...'), atomic_b, ui_tracker)
                    ui_tracker.tick()
                    if atomic_b.get_value():
                        raise UserWarning()

                    singleInput = [chipInputs[inputIndex]]
                    client_ledger.startUntrustedTransaction(False, 0,
                                                            singleInput, redeemScripts[inputIndex], version=tx.version)
                    inputSignature = client_ledger.untrustedHashSign(inputsPaths[inputIndex], pin, lockTime=tx.locktime)
                    inputSignature[0] = 0x30  # force for 1.4.9+
                    my_pubkey = inputs[inputIndex][4]
                    tx.add_signature_to_txin(txin_idx=inputIndex,
                                             signing_pubkey=my_pubkey.hex(),
                                             sig=inputSignature.hex())
                    inputIndex = inputIndex + 1
            else:
                while inputIndex < len(inputs):
                    client_ledger.startUntrustedTransaction(firstTransaction, inputIndex,
                                                            chipInputs, redeemScripts[inputIndex], version=tx.version)
                    # we don't set meaningful outputAddress, amount and fees
                    # as we only care about the alternateEncoding==True branch
                    outputData = client_ledger.finalizeInput(b'', 0, 0, changePath, bfh(rawTx))
                    outputData['outputData'] = txOutput
                    if outputData['confirmationNeeded']:
                        outputData['address'] = output
                        self.handler.finished_ui()
                        self.handler.finished()
                        # do the authenticate dialog and get pin:
                        pin = self.handler.get_auth(outputData, client=client_electrum)
                        if not pin:
                            raise UserWarning()
                        self.handler.show_message(_("Confirmed. Signing Transaction..."))
                    else:
                        if inputIndex == 0:
                            self.handler.finished()
                            self.handler.get_ui(_('Signing Transaction...'), atomic_b, ui_tracker)
                        ui_tracker.tick()
                        if atomic_b.get_value():
                            raise UserWarning()
                        # Sign input with the provided PIN
                        inputSignature = client_ledger.untrustedHashSign(inputsPaths[inputIndex], pin,
                                                                         lockTime=tx.locktime)
                        inputSignature[0] = 0x30  # force for 1.4.9+
                        my_pubkey = inputs[inputIndex][4]
                        tx.add_signature_to_txin(txin_idx=inputIndex,
                                                 signing_pubkey=my_pubkey.hex(),
                                                 sig=inputSignature.hex())
                        inputIndex = inputIndex + 1
                    firstTransaction = False
        except UserWarning:
            logging.exception('Ledger UserWarning')
            self.give_error(_('Cancelled by user'), True)
            return
        except BTChipException as e:
            logging.exception('Ledger BTChip')
            if e.sw in (0x6985, 0x6d00):  # cancelled by user
                return
            elif e.sw == 0x6982:
                raise  # pin lock. decorator will catch it
            else:
                self.logger.exception('')
                self.give_error(e)
        except BaseException as e:
            self.logger.exception('')
            self.give_error(e)
        finally:
            self.handler.finished()

    @runs_in_hwd_thread
    @test_pin_unlocked
    @set_and_unset_signing
    def show_address(self, sequence, txin_type):
        client_ledger = self.get_client_dongle_object()
        address_path = self.get_derivation_prefix()[2:] + "/%d/%d"%sequence
        self.handler.show_message(_("Showing address ..."))
        segwit = is_segwit_script_type(txin_type)
        segwitNative = txin_type == 'p2wpkh'
        try:
            client_ledger.getWalletPublicKey(address_path, showOnScreen=True, segwit=segwit, segwitNative=segwitNative)
        except BTChipException as e:
            if e.sw == 0x6985:  # cancelled by user
                pass
            elif e.sw == 0x6982:
                raise  # pin lock. decorator will catch it
            elif e.sw == 0x6b00:  # hw.1 raises this
                self.handler.show_error('{}\n{}\n{}'.format(
                    _('Error showing address') + ':',
                    e,
                    _('Your device might not have support for this functionality.')))
            else:
                self.logger.exception('')
                self.handler.show_error(e)
        except BaseException as e:
            self.logger.exception('')
            self.handler.show_error(e)
        finally:
            self.handler.finished()


class LedgerPlugin(HW_PluginBase):
    keystore_class = Ledger_KeyStore
    minimum_library = (0, 1, 32)
    DEVICE_IDS = [
                   (0x2581, 0x1807), # HW.1 legacy btchip
                   (0x2581, 0x2b7c), # HW.1 transitional production
                   (0x2581, 0x3b7c), # HW.1 ledger production
                   (0x2581, 0x4b7c), # HW.1 ledger test
                   (0x2c97, 0x0000), # Blue
                   (0x2c97, 0x0011), # Blue app-bitcoin >= 1.5.1
                   (0x2c97, 0x0015), # Blue app-bitcoin >= 1.5.1
                   (0x2c97, 0x0001), # Nano-S
                   (0x2c97, 0x1011), # Nano-S app-bitcoin >= 1.5.1
                   (0x2c97, 0x1015), # Nano-S app-bitcoin >= 1.5.1
                   (0x2c97, 0x0004), # Nano-X
                   (0x2c97, 0x4011), # Nano-X app-bitcoin >= 1.5.1
                   (0x2c97, 0x4015), # Nano-X app-bitcoin >= 1.5.1
                   (0x2c97, 0x0005), # Nano-S Plus
                   (0x2c97, 0x5011), # Nano-S Plus app-bitcoin >= 1.5.1
                   (0x2c97, 0x5015), # Nano-S Plus app-bitcoin >= 1.5.1
                   (0x2c97, 0x0006), # RFU
                   (0x2c97, 0x0007), # RFU
                   (0x2c97, 0x0008), # RFU
                   (0x2c97, 0x0009), # RFU
                   (0x2c97, 0x000a)  # RFU
                 ]
    VENDOR_IDS = (0x2c97,)
    LEDGER_MODEL_IDS = {
        0x10: "Ledger Nano S",
        0x40: "Ledger Nano X",
        0x50: "Ledger Nano S Plus",
    }
    SUPPORTED_XTYPES = ('standard', 'p2wpkh-p2sh', 'p2wpkh', 'p2wsh-p2sh', 'p2wsh')

    def __init__(self, parent, config, name):
        self.segwit = config.get("segwit")
        HW_PluginBase.__init__(self, parent, config, name)
        self.libraries_available = self.check_libraries_available()
        if not self.libraries_available:
            return
        # to support legacy devices and legacy firmwares
        self.device_manager().register_devices(self.DEVICE_IDS, plugin=self)
        # to support modern firmware
        self.device_manager().register_vendor_ids(self.VENDOR_IDS, plugin=self)

    def get_library_version(self):
        try:
            import btchip
            version = btchip.__version__
        except ImportError:
            raise
        except:
            version = "unknown"
        if BTCHIP:
            return version
        else:
            raise LibraryFoundButUnusable(library_version=version)

    @classmethod
    def _recognize_device(cls, product_key) -> Tuple[bool, Optional[str]]:
        """Returns (can_recognize, model_name) tuple."""
        # legacy product_keys
        if product_key in cls.DEVICE_IDS:
            if product_key[0] == 0x2581:
                return True, "Ledger HW.1"
            if product_key == (0x2c97, 0x0000):
                return True, "Ledger Blue"
            if product_key == (0x2c97, 0x0001):
                return True, "Ledger Nano S"
            if product_key == (0x2c97, 0x0004):
                return True, "Ledger Nano X"
            if product_key == (0x2c97, 0x0005):
                return True, "Ledger Nano S Plus"
            return True, None
        # modern product_keys
        if product_key[0] == 0x2c97:
            product_id = product_key[1]
            model_id = product_id >> 8
            if model_id in cls.LEDGER_MODEL_IDS:
                model_name = cls.LEDGER_MODEL_IDS[model_id]
                return True, model_name
        # give up
        return False, None

    def can_recognize_device(self, device: Device) -> bool:
        return self._recognize_device(device.product_key)[0]

    @classmethod
    def device_name_from_product_key(cls, product_key) -> Optional[str]:
        return cls._recognize_device(product_key)[1]

    def create_device_from_hid_enumeration(self, d, *, product_key):
        device = super().create_device_from_hid_enumeration(d, product_key=product_key)
        if not self.can_recognize_device(device):
            return None
        return device

    @runs_in_hwd_thread
    def get_btchip_device(self, device):
        ledger = False
        if device.product_key[0] == 0x2581 and device.product_key[1] == 0x3b7c:
            ledger = True
        if device.product_key[0] == 0x2581 and device.product_key[1] == 0x4b7c:
            ledger = True
        if device.product_key[0] == 0x2c97:
            if device.interface_number == 0 or device.usage_page == 0xffa0:
                ledger = True
            else:
                return None  # non-compatible interface of a Nano S or Blue
        dev = hid.device()
        dev.open_path(device.path)
        dev.set_nonblocking(True)
        return HIDDongleHIDAPI(dev, ledger, BTCHIP_DEBUG)

    @runs_in_hwd_thread
    def create_client(self, device, handler):
        client = self.get_btchip_device(device)
        if client is not None:
            client = Ledger_Client(client, product_key=device.product_key, plugin=self)
        return client

    def setup_device(self, device_info, wizard, purpose):
        device_id = device_info.device.id_
        client = self.scan_and_create_client_for_device(device_id=device_id, wizard=wizard)
        wizard.run_task_without_blocking_gui(
            task=lambda: client.get_xpub("m/175'", 'standard'))  # TODO replace by direct derivation once Nano S > 1.1
        return client

    def get_xpub(self, device_id, derivation, xtype, wizard):
        if xtype not in self.SUPPORTED_XTYPES:
            raise ScriptTypeNotSupported(_('This type of script is not supported with {}.').format(self.device))
        client = self.scan_and_create_client_for_device(device_id=device_id, wizard=wizard)
        client.checkDevice()
        xpub = client.get_xpub(derivation, xtype)
        return xpub

    @runs_in_hwd_thread
    def get_client(self, keystore, force_pair=True, *,
                   devices=None, allow_user_interaction=True):
        # All client interaction should not be in the main GUI thread
        client = super().get_client(keystore, force_pair,
                                    devices=devices,
                                    allow_user_interaction=allow_user_interaction)
        # returns the client for a given keystore. can use xpub
        # if client:
        #    client.used()
        if client is not None:
            client.checkDevice()
        return client

    @runs_in_hwd_thread
    def show_address(self, wallet, address, keystore=None):
        if keystore is None:
            keystore = wallet.get_keystore()
        if not self.show_address_helper(wallet, address, keystore):
            return
        if type(wallet) is not Standard_Wallet:
            keystore.handler.show_error(
                _('This function is only available for standard wallets when using {}.').format(self.device))
            return
        sequence = wallet.get_address_index(address)
        txin_type = wallet.get_txin_type(address)
        keystore.show_address(sequence, txin_type)
