#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 kyuupichan@gmail
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
from collections import defaultdict
from math import floor, log10
from typing import NamedTuple, List, Callable, Sequence, Union, Dict, Tuple, Optional
from decimal import Decimal

from .ravencoin import sha256, COIN, is_address
from .transaction import Transaction, TxOutput, PartialTransaction, PartialTxInput, PartialTxOutput, RavenValue
from .util import NotEnoughFunds, Satoshis
from .logging import Logger


# A simple deterministic PRNG.  Used to deterministically shuffle a
# set of coins - the same set of coins should produce the same output.
# Although choosing UTXOs "randomly" we want it to be deterministic,
# so if sending twice from the same UTXO set we choose the same UTXOs
# to spend.  This prevents attacks on users by malicious or stale
# servers.
class PRNG:
    def __init__(self, seed):
        self.sha = sha256(seed)
        self.pool = bytearray()

    def get_bytes(self, n: int) -> bytes:
        while len(self.pool) < n:
            self.pool.extend(self.sha)
            self.sha = sha256(self.sha)
        result, self.pool = self.pool[:n], self.pool[n:]
        return bytes(result)

    def randint(self, start, end):
        # Returns random integer in [start, end)
        n = end - start
        r = 0
        p = 1
        while p < n:
            r = self.get_bytes(1)[0] + (r << 8)
            p = p << 8
        return start + (r % n)

    def choice(self, seq):
        return seq[self.randint(0, len(seq))]

    def shuffle(self, x):
        for i in reversed(range(1, len(x))):
            # pick an element in x[:i+1] with which to exchange x[i]
            j = self.randint(0, i+1)
            x[i], x[j] = x[j], x[i]


class Bucket(NamedTuple):
    desc: str
    weight: int                   # as in BIP-141
    value: RavenValue             # in satoshis
    effective_value: RavenValue   # estimate of value left after subtracting fees. in satoshis
    coins: List[PartialTxInput]   # UTXOs
    min_height: int               # min block height where a coin was confirmed
    witness: bool                 # whether any coin uses segwit


class ScoredCandidate(NamedTuple):
    penalty: float
    tx: PartialTransaction
    buckets: List[Bucket]

def strip_unneeded(bkts: List[Bucket], needs_more) -> List[Bucket]:
    '''Remove buckets that are unnecessary in achieving the spend amount'''
    if not needs_more([], bucket_value_sum=RavenValue()):
        # none of the buckets are needed
        return []

    a = sum([b.value for b in bkts], RavenValue()).assets.keys()

    bkts_rvn = sorted([b for b in bkts if b.value.rvn_value.value != 0],
                      key=lambda bkt: bkt.value.rvn_value.value, reverse=True)

    bkts_asset = {asset: sorted([b for b in bkts if asset in b.value.assets.keys()],
                                key=lambda bkt: bkt.value.assets[asset].value, reverse=True) for asset in a}

    rvn_ptr = 0

    asset_ptr = {asset: -1 for asset in a}

    def get_rvn_bucket_value() -> RavenValue:
        return bkts_rvn[rvn_ptr].value

    def get_asset_bucket_value(asset) -> RavenValue:
        return bkts_asset[asset][asset_ptr[asset]].value

    def is_asset_done(asset):
        return asset_ptr[asset]+1 == len(bkts_asset[asset])

    def increment_asset_ptr(asset):
        ptr = asset_ptr[asset]
        asset_ptr[asset] = ptr + 1

    def get_buckets() -> List[Bucket]:
        return bkts_rvn[:rvn_ptr+1] + \
               [bkt for asset, asset_bkts in bkts_asset.items() for bkt in asset_bkts[:asset_ptr[asset]+1]]

    bucket_value_sum = RavenValue()

    needed = {None}
    while needed:
        while needed == {None}:
            bucket_value_sum += get_rvn_bucket_value()
            needed = needs_more(get_buckets(), bucket_value_sum=bucket_value_sum)
            if needed == {None} and rvn_ptr+1 == len(bkts_rvn):
                raise Exception("keeping all RVN buckets is still not enough: {}".format(bucket_value_sum))
            rvn_ptr += 1
        while needed and needed != {None}:
            asset = next(iter(needed))
            increment_asset_ptr(asset)
            bucket_value_sum += get_asset_bucket_value(asset)
            needed = needs_more(get_buckets(), bucket_value_sum=bucket_value_sum)
            if needed and (needed & {asset}) and is_asset_done(asset):
                raise Exception("keeping all {} buckets is still not enough: {}".format(asset, bucket_value_sum))

    return get_buckets()


class CoinChooserBase(Logger):

    def __init__(self, *, enable_output_value_rounding: bool):
        Logger.__init__(self)
        self.enable_output_value_rounding = enable_output_value_rounding

    def keys(self, coins: Sequence[PartialTxInput]) -> Sequence[str]:
        raise NotImplementedError

    def bucketize_coins(self, coins: Sequence[PartialTxInput], *, fee_estimator_vb):
        keys = self.keys(coins)
        buckets = defaultdict(list)  # type: Dict[str, List[PartialTxInput]]
        for key, coin in zip(keys, coins):
            buckets[key].append(coin)

        # fee_estimator returns fee to be paid, for given vbytes.
        # guess whether it is just returning a constant as follows.
        constant_fee = fee_estimator_vb(2000) == fee_estimator_vb(200)

        def make_Bucket(desc: str, coins: List[PartialTxInput]):
            witness = any(coin.is_segwit(guess_for_address=True) for coin in coins)
            # note that we're guessing whether the tx uses segwit based
            # on this single bucket
            weight = sum(Transaction.estimated_input_weight(coin, witness)
                         for coin in coins)
            value = sum([coin.value_sats() for coin in coins], RavenValue())
            min_height = min(coin.block_height for coin in coins)
            assert min_height is not None
            # the fee estimator is typically either a constant or a linear function,
            # so the "function:" effective_value(bucket) will be homomorphic for addition
            # i.e. effective_value(b1) + effective_value(b2) = effective_value(b1 + b2)
            if constant_fee:
                effective_value = value
            else:
                # when converting from weight to vBytes, instead of rounding up,
                # keep fractional part, to avoid overestimating fee
                fee = fee_estimator_vb(Decimal(weight) / 4)
                effective_value = value - RavenValue(fee)
            return Bucket(desc=desc,
                          weight=weight,
                          value=value,
                          effective_value=effective_value,
                          coins=coins,
                          min_height=min_height,
                          witness=witness)

        return list(map(make_Bucket, buckets.keys(), buckets.values()))

    def penalty_func(self, base_tx, *,
                     tx_from_buckets: Callable[[List[Bucket]], Tuple[PartialTransaction, List[PartialTxOutput]]]) \
            -> Callable[[List[Bucket]], ScoredCandidate]:
        raise NotImplementedError

    def _change_amounts(self, tx: PartialTransaction, count: int, fee_estimator_numchange,
                        asset_divisions: Dict[str, int]) -> List[Tuple[Optional[str], int]]:
        # Break change up if bigger than max_change
        output_amounts = [(o.asset, o.value) for o in tx.outputs()]
        output_amounts_rvn = [t[1] for t in output_amounts if not t[0]]
        ret_amt = []

        # Each asset has a maximum of 1 vout amount
        for asset, divisions in asset_divisions.items():
            output_amounts_asset = [t[1] for t in output_amounts if t[0] == asset]
            minimum_division = 10 ** -divisions
            # Don't split change of less than min div or less than 0.02 BTC
            max_change_asset = max(max([o.value for o in output_amounts_asset]) * 1.25,
                                   max(minimum_division * COIN, 0.02 * COIN))

            change_amount_asset = tx.get_fee().assets.get(asset, Satoshis(0)).value

            # Get a handle on the precision of the output amounts; round our
            # change to look similar
            def trailing_zeroes(val):
                s = str(val)
                return len(s) - len(s.rstrip('0'))

            zeroes = [trailing_zeroes(i) for i in output_amounts_asset]
            min_zeroes = min(zeroes)

            zeroes = [min_zeroes]

            # Calculate change; randomize it a bit if using more than 1 output
            remaining = change_amount_asset
            amounts = []

            # Last change output.  Round down to maximum precision but lose
            # no more than 10**max_dp_to_round_for_privacy
            # e.g. a max of 2 decimal places means losing 100 satoshis to fees
            N = int(pow(10, min(0, zeroes[0])))
            amount = (remaining // N) * N
            amounts.append(amount)

            assert sum(amounts) == change_amount_asset

            amounts = [a for a in amounts if a > 0]

            ret_amt += [(asset, amount) for amount in amounts]

        if not output_amounts_rvn:
            # Append an amount for the vout after our transaction fee
            output_amounts_rvn = [Satoshis(max(0, tx.get_fee().rvn_value.value -
                                  fee_estimator_numchange(1, asset_divisions.keys())))]

        # Don't split change of less than 0.02 BTC
        max_change_rvn = max(max([o.value for o in output_amounts_rvn]) * 1.25, 0.02 * COIN)

        # Use N change outputs
        change_amount_rvn = 0
        n = 1
        for n1 in range(1, count + 1):
            # How much is left if we add this many change outputs?
            # tx.get_fee() returns our ins - outs
            # i.e. what should be going in our vouts
            n = n1
            change_amount_rvn = max(0, tx.get_fee().rvn_value.value -
                                    fee_estimator_numchange(n1, asset_divisions.keys()))
            if change_amount_rvn // n1 <= max_change_rvn:
                break

        # Get a handle on the precision of the output amounts; round our
        # change to look similar
        def trailing_zeroes(val):
            s = str(val)
            return len(s) - len(s.rstrip('0'))

        zeroes = [trailing_zeroes(i) for i in output_amounts_rvn]
        min_zeroes = min(zeroes)
        max_zeroes = max(zeroes)

        if n > 1:
            zeroes = range(max(0, min_zeroes - 1), (max_zeroes + 1) + 1)
        else:
            # if there is only one change output, this will ensure that we aim
            # to have one that is exactly as precise as the most precise output
            zeroes = [min_zeroes]

        # Calculate change; randomize it a bit if using more than 1 output
        remaining = change_amount_rvn
        amounts = []
        while n > 1:
            average = remaining / n
            amount = self.p.randint(int(average * 0.7), int(average * 1.3))
            precision = min(self.p.choice(zeroes), int(floor(log10(amount))))
            amount = int(round(amount, -precision))
            amounts.append(amount)
            remaining -= amount
            n -= 1

        # Last change output.  Round down to maximum precision but lose
        # no more than 10**max_dp_to_round_for_privacy
        # e.g. a max of 2 decimal places means losing 100 satoshis to fees
        max_dp_to_round_for_privacy = 2 if self.enable_output_value_rounding else 0
        N = int(pow(10, min(max_dp_to_round_for_privacy, zeroes[0])))
        amount = (remaining // N) * N
        amounts.append(amount)

        assert sum(amounts) <= change_amount_rvn

        ret_amt += [(None, amount) for amount in amounts]

        return ret_amt

    def _change_outputs(self, tx: PartialTransaction, change_addrs, fee_estimator_numchange,
                        dust_threshold, asset_divs: Dict[str, int], has_return: bool) -> List[PartialTxOutput]:
        amounts = self._change_amounts(tx, len(change_addrs) - len(asset_divs), fee_estimator_numchange, asset_divs)
        assert all([t[1] >= 0 for t in amounts])
        assert len(change_addrs) >= len(amounts) - (1 if has_return else 0)
        assert all([isinstance(amt, Tuple) for amt in amounts])
        # If change is above dust threshold after accounting for the
        # size of the change output, add it to the transaction.
        amounts = [amount for amount in amounts if amount[1] >= dust_threshold]
        change = [PartialTxOutput.from_address_and_value(addr, Satoshis(amount[1]), amount[0])
                  for addr, amount in zip(change_addrs, amounts)]
        return change

    def _construct_tx_from_selected_buckets(self, *, buckets: Sequence[Bucket],
                                            base_tx: PartialTransaction, change_addrs,
                                            fee_estimator_w, dust_threshold,
                                            base_weight,
                                            wallet,
                                            asset_divs: Dict[str, int],
                                            has_return: bool) -> Tuple[PartialTransaction, List[PartialTxOutput]]:
        # make a copy of base_tx so it won't get mutated
        tx = PartialTransaction.from_io(base_tx.inputs()[:], base_tx.outputs()[:], wallet=wallet)

        tx.add_inputs([coin for b in buckets for coin in b.coins])
        tx_weight = self._get_tx_weight(buckets, base_weight=base_weight)

        # change is sent back to sending address unless specified
        if not change_addrs:
            change_addrs = [tx.inputs()[0].address]
            # note: this is not necessarily the final "first input address"
            # because the inputs had not been sorted at this point
            assert is_address(change_addrs[0])

        # This takes a count of change outputs and returns a tx fee
        output_weight = 4 * Transaction.estimated_output_size_for_address(change_addrs[0])

        def fee_estimator_assets(assets: List[str]) -> int:
            weight = 0
            for asset in assets:
                weight += 4 * Transaction.estimated_output_size_for_address_with_asset(change_addrs[0], asset)
            return weight

        def fee_estimator_numchange(rvn_count: int, assets: List[str]) -> int:
            return fee_estimator_w(tx_weight + rvn_count * output_weight +
                                   fee_estimator_assets(assets))

        change = self._change_outputs(tx, change_addrs, fee_estimator_numchange, dust_threshold, asset_divs, has_return)
        tx.add_outputs(change)

        return tx, change

    def _get_tx_weight(self, buckets: Sequence[Bucket], *, base_weight: int) -> int:
        """Given a collection of buckets, return the total weight of the
        resulting transaction.
        base_weight is the weight of the tx that includes the fixed (non-change)
        outputs and potentially some fixed inputs. Note that the change outputs
        at this point are not yet known so they are NOT accounted for.
        """
        total_weight = base_weight + sum(bucket.weight for bucket in buckets)
        is_segwit_tx = any(bucket.witness for bucket in buckets)
        if is_segwit_tx:
            total_weight += 2  # marker and flag
            # non-segwit inputs were previously assumed to have
            # a witness of '' instead of '00' (hex)
            # note that mixed legacy/segwit buckets are already ok
            num_legacy_inputs = sum((not bucket.witness) * len(bucket.coins)
                                    for bucket in buckets)
            total_weight += num_legacy_inputs
        return total_weight

    def make_tx(self, *, coins: Sequence[PartialTxInput], inputs: List[PartialTxInput],
                outputs: List[PartialTxOutput], change_addrs: Sequence[str],
                fee_estimator_vb: Callable, dust_threshold: int,
                asset_divs: Dict[str, int],
                wallet,
                coinbase_outputs=None) -> PartialTransaction:
        """Select unspent coins to spend to pay outputs.  If the change is
        greater than dust_threshold (after adding the change output to
        the transaction) it is kept, otherwise none is sent and it is
        added to the transaction fee.

        `inputs` and `outputs` are guaranteed to be a subset of the
        inputs and outputs of the resulting transaction.
        `coins` are further UTXOs we can choose from.

        Note: fee_estimator_vb expects virtual bytes
        """
        assert outputs, 'tx outputs cannot be empty'

        # Deterministic randomness from coins
        utxos = [c.prevout.serialize_to_network() for c in coins]
        self.p = PRNG(b''.join(sorted(utxos)))

        # Copy the outputs so when adding change we don't modify "outputs"
        base_tx = PartialTransaction.from_io(inputs[:], outputs[:], wallet=wallet)
        input_value = base_tx.input_value()

        # Weight of the transaction with no inputs and no change
        # Note: this will use legacy tx serialization as the need for "segwit"
        # would be detected from inputs. The only side effect should be that the
        # marker and flag are excluded, which is compensated in get_tx_weight()
        # FIXME calculation will be off by this (2 wu) in case of RBF batching

        base_weight = base_tx.estimated_weight()

        if coinbase_outputs:
            base_weight = PartialTransaction.from_io(inputs[:], outputs[:] + coinbase_outputs, wallet=wallet).estimated_weight()

        spent_amount = base_tx.output_value()

        def fee_estimator_w(weight):
            i = Transaction.virtual_size_from_weight(weight)
            return fee_estimator_vb(i)

        def needs_more(buckets, *, bucket_value_sum: RavenValue):
            '''Given a list of buckets, return True if it has enough
            value to pay for the transaction'''
            # assert bucket_value_sum == sum(bucket.value for bucket in buckets)  # expensive!
            total_input = input_value + bucket_value_sum
            if total_input.rvn_value < spent_amount.rvn_value:  # shortcut for performance
                return {None}
            in_assets = total_input.assets
            out_assets = spent_amount.assets
            for asset in out_assets:
                if asset not in in_assets:
                    return {asset}
                if in_assets[asset] < out_assets[asset]:
                    return {asset}
            # any bitcoin tx must have at least 1 input by consensus
            # (check we add some new UTXOs now or already have some fixed inputs)
            if not buckets and not inputs:
                return {None}
            # note re performance: so far this was constant time
            # what follows is linear in len(buckets)
            total_weight = self._get_tx_weight(buckets, base_weight=base_weight)

            fee = fee_estimator_w(total_weight)
            total_out = spent_amount + RavenValue(fee)
            out_assets = spent_amount.assets
            ret_val = total_input.rvn_value >= total_out.rvn_value
            if not ret_val:
                return {None}
            for asset in out_assets:
                if asset not in in_assets:
                    return {asset}
                if in_assets[asset] < out_assets[asset]:
                    return {asset}
            return set()

        has_return = any([o.value == 0 and o.scriptpubkey[0] == 0x6a for o in outputs])

        def tx_from_buckets(buckets):
            return self._construct_tx_from_selected_buckets(buckets=buckets,
                                                            base_tx=base_tx,
                                                            change_addrs=change_addrs,
                                                            fee_estimator_w=fee_estimator_w,
                                                            dust_threshold=dust_threshold,
                                                            base_weight=base_weight,
                                                            wallet=wallet,
                                                            asset_divs=asset_divs,
                                                            has_return=has_return)

        # Collect the coins into buckets
        all_buckets = self.bucketize_coins(coins, fee_estimator_vb=fee_estimator_vb)
        # Filter some buckets out. Only keep those that have positive effective value.
        # Note that this filtering is intentionally done on the bucket level
        # instead of per-coin, as each bucket should be either fully spent or not at all.
        # (e.g. CoinChooserPrivacy ensures that same-address coins go into one bucket)
        all_buckets = list(filter(lambda b: b.effective_value.rvn_value.value > 0 or
                                            len(b.value.assets) > 0, all_buckets))

        # Choose a subset of the buckets
        scored_candidate = self.choose_buckets(all_buckets, needs_more,
                                               self.penalty_func(base_tx, tx_from_buckets=tx_from_buckets),
                                               coinbase_outputs=coinbase_outputs)
        tx = scored_candidate.tx

        self.logger.info(f"using {len(tx.inputs())} inputs")
        self.logger.info(f"using buckets: {[bucket.desc for bucket in scored_candidate.buckets]}")

        return tx

    def choose_buckets(self, buckets: List[Bucket],
                       needs_more: Callable,
                       penalty_func: Callable[[List[Bucket]], ScoredCandidate],
                       coinbase_outputs) -> ScoredCandidate:
        raise NotImplemented('To be subclassed')


class CoinChooserRandom(CoinChooserBase):
    def bucket_candidates_any(self, buckets: List[Bucket], needs_more) -> List[List[Bucket]]:
        '''Returns a list of bucket sets.'''
        if not buckets:
            if not needs_more([], bucket_value_sum=RavenValue()):
                return [[]]
            else:
                raise NotEnoughFunds()

        candidates = set()

        # Add all singletons
        for n, bucket in enumerate(buckets):
            if not needs_more([bucket], bucket_value_sum=bucket.value):
                candidates.add((n,))

        # And now some random ones
        attempts = min(100, (len(buckets) - 1) * 10 + 1)
        permutation = list(range(len(buckets)))
        for i in range(attempts):
            # Get a random permutation of the buckets, and
            # incrementally combine buckets until sufficient
            self.p.shuffle(permutation)
            bkts = []
            bucket_value_sum = RavenValue()
            for count, index in enumerate(permutation):
                bucket = buckets[index]
                bkts.append(bucket)
                bucket_value_sum += bucket.value
                if not needs_more(bkts, bucket_value_sum=bucket_value_sum):
                    candidates.add(tuple(sorted(permutation[:count + 1])))
                    break
            else:
                # note: this assumes that the effective value of any bkt is >= 0
                raise NotEnoughFunds()

        candidates = [[buckets[n] for n in c] for c in candidates]
        return [strip_unneeded(c, needs_more) for c in candidates]

    def bucket_candidates_prefer_confirmed(self, buckets: List[Bucket],
                                           needs_more) -> List[List[Bucket]]:
        """Returns a list of bucket sets preferring confirmed coins.

        Any bucket can be:
        1. "confirmed" if it only contains confirmed coins; else
        2. "unconfirmed" if it does not contain coins with unconfirmed parents
        3. other: e.g. "unconfirmed parent" or "local"

        This method tries to only use buckets of type 1, and if the coins there
        are not enough, tries to use the next type but while also selecting
        all buckets of all previous types.
        """
        conf_buckets = [bkt for bkt in buckets if bkt.min_height > 0]
        unconf_buckets = [bkt for bkt in buckets if bkt.min_height == 0]
        other_buckets = [bkt for bkt in buckets if bkt.min_height < 0]

        bucket_sets = [conf_buckets, unconf_buckets, other_buckets]
        already_selected_buckets = []
        already_selected_buckets_value_sum = RavenValue()

        for bkts_choose_from in bucket_sets:
            try:
                def sfunds(bkts, *, bucket_value_sum: RavenValue):
                    bucket_value_sum += already_selected_buckets_value_sum
                    return needs_more(already_selected_buckets + bkts,
                                            bucket_value_sum=bucket_value_sum)

                candidates = self.bucket_candidates_any(bkts_choose_from, sfunds)
                break
            except NotEnoughFunds:
                already_selected_buckets += bkts_choose_from
                already_selected_buckets_value_sum += sum([bucket.value for bucket in bkts_choose_from], RavenValue())
        else:
            raise NotEnoughFunds()

        candidates = [(already_selected_buckets + c) for c in candidates]
        return [strip_unneeded(c, needs_more) for c in candidates]

    def choose_buckets(self, buckets, needs_more, penalty_func, coinbase_outputs):
        candidates = self.bucket_candidates_prefer_confirmed(buckets, needs_more)
        scored_candidates = [penalty_func(cand) for cand in candidates]
        winner = min(scored_candidates, key=lambda x: x.penalty)
        self.logger.info(f"Total number of buckets: {len(buckets)}")
        self.logger.info(f"Num candidates considered: {len(candidates)}. "
                         f"Winning penalty: {winner.penalty}")
        if coinbase_outputs:
            winner.tx.add_outputs(coinbase_outputs)
        return winner


class CoinChooserPrivacy(CoinChooserRandom):
    """Attempts to better preserve user privacy.
    First, if any coin is spent from a user address, all coins are.
    Compared to spending from other addresses to make up an amount, this reduces
    information leakage about sender holdings.  It also helps to
    reduce blockchain UTXO bloat, and reduce future privacy loss that
    would come from reusing that address' remaining UTXOs.
    Second, it penalizes change that is quite different to the sent amount.
    Third, it penalizes change that is too big.
    """

    def keys(self, coins):
        return [coin.scriptpubkey.hex() for coin in coins]

    def penalty_func(self, base_tx, *, tx_from_buckets):
        # This is per bucket; we dont care about RavenValues; just values
        min_change = min(o.value.value for o in base_tx.outputs()) * 0.75
        max_change = max(o.value.value for o in base_tx.outputs()) * 1.33

        def penalty(buckets: List[Bucket]) -> ScoredCandidate:
            # Penalize using many buckets (~inputs)
            badness = len(buckets) - 1
            tx, change_outputs = tx_from_buckets(buckets)
            change = sum(o.value.value for o in change_outputs)
            # Penalize change not roughly in output range
            if change == 0:
                pass  # no change is great!
            elif change < min_change:
                badness += (min_change - change) / (min_change + 10000)
                # Penalize really small change; under 1 mBTC ~= using 1 more input
                if change < COIN / 1000:
                    badness += 1
            elif change > max_change:
                badness += (change - max_change) / (max_change + 10000)
                # Penalize large change; 5 BTC excess ~= using 1 more input
                badness += change / (COIN * 5)
            return ScoredCandidate(badness, tx, buckets)

        return penalty


COIN_CHOOSERS = {
    'Privacy': CoinChooserPrivacy,
}

def get_name(config):
    kind = config.get('coin_chooser')
    if not kind in COIN_CHOOSERS:
        kind = 'Privacy'
    return kind

def get_coin_chooser(config):
    klass = COIN_CHOOSERS[get_name(config)]
    # note: we enable enable_output_value_rounding by default as
    #       - for sacrificing a few satoshis
    #       + it gives better privacy for the user re change output
    #       + it also helps the network as a whole as fees will become noisier
    #         (trying to counter the heuristic that "whole integer sat/byte feerates" are common)
    coinchooser = klass(
        enable_output_value_rounding=config.get('coin_chooser_output_rounding', False),
    )
    return coinchooser
