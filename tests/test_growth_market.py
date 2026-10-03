import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hermes_trading.growth import BAR_MS, GrowthConfig, Portfolio
from hermes_trading.growth_market import prepare_feeds, pause
from hermes_trading.growth_run import _cycle_delay, _recover_market_gap, _paper, _save
from hermes_trading.strategy import closed
from hermes_trading.adapters import price
from test_alpaca_paper_bridge import FakeAPI
from hermes_trading.alpaca_paper_bridge import BrokerError, PaperAuto


class MarketRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = GrowthConfig(assets=('BTC/USDT', 'ETH/USDT'))
        self.now = 400 * BAR_MS + 20_000
        self.latest = 399 * BAR_MS

    def feeds(self, now=None):
        now = self.now if now is None else now
        def candles(step):
            current = now // step * step
            times = list(range(current - 249 * step, current + step, step))
            return {'t': times, 'open': [100.] * 250, 'high': [101.] * 250,
                    'low': [99.] * 250, 'close': [100.] * 250, 'source': 'test'}
        raw, hourly = candles(BAR_MS), closed(candles(4 * BAR_MS), '1h', now)
        return {a: (closed(copy.deepcopy(raw), '15m', now), copy.deepcopy(hourly),
                    copy.deepcopy(raw)) for a in self.cfg.assets}

    def book(self, opened=False):
        book = Portfolio(self.cfg)
        if opened:
            book._open(390 * BAR_MS, {'open': 100}, {'asset': 'BTC/USDT',
                       'stop_fraction': .02, 'regime': 'RANGE', 'decision_ms': 390 * BAR_MS})
        book.state['last_bar'] = {a: self.latest for a in self.cfg.assets}
        return book

    def test_late_asset_does_not_advance_any_cursor_or_latch_gap(self):
        book, feeds = self.book(), self.feeds()
        bars, hours, raw = feeds['ETH/USDT']
        feeds['ETH/USDT'] = ({k: v[:-1] if isinstance(v, list) else v for k,v in bars.items()}, hours, raw)
        before = copy.deepcopy(book.state)
        with patch.object(price, 'backfill', new=AsyncMock()) as repair:
            self.assertFalse(asyncio.run(prepare_feeds(book, feeds, self.now)))
            repair.assert_not_called()
        self.assertEqual(book.state['last_bar'], before['last_bar'])
        self.assertEqual(book.state['cash'], before['cash'])
        self.assertIsNone(book.state['halted'])
        self.assertEqual(book.state['market_data']['status'], 'waiting')
        self.assertTrue(asyncio.run(prepare_feeds(book, self.feeds(), self.now)))

    def test_preboundary_missing_forming_quote_uses_fresh_one_minute_fallback(self):
        now = 400 * BAR_MS - 500
        latest = 398 * BAR_MS
        book, feeds = Portfolio(self.cfg), self.feeds(now)
        book.state['last_bar'] = {a: latest for a in self.cfg.assets}
        for asset, (bars, hours, raw) in list(feeds.items()):
            raw = {k: v[:-1] if isinstance(v, list) else v for k, v in raw.items()}
            feeds[asset] = (closed(copy.deepcopy(raw), '15m', now), hours, raw)
        fallback = {'price': 100., 'source': 'binance',
                    'source_ts': now // 60_000 * 60_000, 'observed_ms': now}
        with patch.object(price, 'quote', new=AsyncMock(return_value=fallback)) as fresh:
            self.assertTrue(asyncio.run(prepare_feeds(book, feeds, now)))
            self.assertEqual(fresh.await_count, len(self.cfg.assets))
        self.assertIsNone(book.state['halted'])
        self.assertEqual(book.state['market_data']['status'], 'ready')

    def test_preboundary_quote_outage_waits_without_latching_gap(self):
        now = 400 * BAR_MS - 500
        latest = 398 * BAR_MS
        book, feeds = Portfolio(self.cfg), self.feeds(now)
        book.state['last_bar'] = {a: latest for a in self.cfg.assets}
        for asset, (bars, hours, raw) in list(feeds.items()):
            raw = {k: v[:-1] if isinstance(v, list) else v for k, v in raw.items()}
            feeds[asset] = (closed(copy.deepcopy(raw), '15m', now), hours, raw)
        with patch.object(price, 'quote', new=AsyncMock(side_effect=RuntimeError('late quote'))):
            self.assertFalse(asyncio.run(prepare_feeds(book, feeds, now)))
        self.assertIsNone(book.state['halted'])
        self.assertEqual(book.state['market_data']['status'], 'waiting')
        self.assertTrue(all(p['reason'] == 'missing_current_quote'
                            for p in book.state['market_data']['problems']))

    def test_cycle_delay_aligns_five_seconds_after_minute(self):
        self.assertAlmostEqual(_cycle_delay(100 * 60 + 59.5), 5.5)
        self.assertAlmostEqual(_cycle_delay(100 * 60 + 5.0), 60.0)

    def test_real_gap_repairs_from_source_and_unrepairable_gap_stays_paused(self):
        book, good = self.book(), self.feeds()
        feeds = copy.deepcopy(good)
        bars, hours, raw = feeds['ETH/USDT']
        i = bars['t'].index(395 * BAR_MS)
        feeds['ETH/USDT'] = ({k: v[:i]+v[i+1:] if isinstance(v, list) else v for k,v in bars.items()}, hours, raw)
        with patch.object(price, 'backfill', new=AsyncMock(return_value=good['ETH/USDT'][2])) as repair:
            self.assertTrue(asyncio.run(prepare_feeds(book, copy.deepcopy(feeds), self.now)))
            self.assertEqual(repair.call_args.args[0], 'ETH/USDT')
        with patch.object(price, 'backfill', new=AsyncMock(side_effect=RuntimeError('offline'))):
            self.assertFalse(asyncio.run(prepare_feeds(book, feeds, self.now)))
        self.assertEqual(book.state['halted'], 'market_data_gap')
        self.assertEqual(book.state['market_data']['problems'][-1]['first_missing_bar'], 395 * BAR_MS)

    def test_recovery_preserves_open_position_cash_and_history(self):
        book = self.book(opened=True)
        book.state['halted'] = 'market_data_gap'
        original = copy.deepcopy(book.state)
        self.assertTrue(asyncio.run(_recover_market_gap(book, self.feeds(), self.now, None)))
        self.assertIsNone(book.state['halted'])
        for key in ('cash', 'positions', 'trades'):
            self.assertEqual(book.state[key], original[key])
        self.assertEqual(book.state['events'][-1]['open_positions'], 1)

    def test_gap_before_recent_window_still_prevents_open_position_recovery(self):
        book, feeds = self.book(opened=True), self.feeds()
        book.state['positions']['BTC/USDT']['opened_ms'] = 300 * BAR_MS
        book.state['halted'] = 'market_data_gap'
        bars, hours, raw = feeds['BTC/USDT']
        i = bars['t'].index(305 * BAR_MS)
        feeds['BTC/USDT'] = ({k: v[:i]+v[i+1:] if isinstance(v, list) else v for k,v in bars.items()}, hours, raw)
        self.assertFalse(asyncio.run(_recover_market_gap(book, feeds, self.now, None)))
        self.assertEqual(book.state['halted'], 'market_data_gap')
        self.assertEqual(len(book.state['positions']), 1)

    def test_missed_stop_exits_at_current_quote_and_time(self):
        book, feeds = self.book(opened=True), self.feeds()
        book.state['halted'] = 'market_data_gap'
        feeds['BTC/USDT'][0]['low'][-4] = 90.
        feeds['BTC/USDT'][2]['close'][-1] = 97.
        self.assertTrue(asyncio.run(_recover_market_gap(book, feeds, self.now, None)))
        trade = book.state['trades'][-1]
        self.assertEqual(trade['reason'], 'market_data_recovery_stop')
        self.assertEqual(trade['closed_ms'], self.now)
        self.assertAlmostEqual(trade['exit'], 97 * (1 - book.cfg.slippage - book.cfg.spread/2))

    def test_risk_halts_never_clear_or_get_replaced_by_data_errors(self):
        for halt in ('day_loss_limit', 'week_loss_limit', 'drawdown_limit', 'manual_review'):
            book = self.book()
            book.state['halted'] = halt
            pause(book, 'market_data_gap')
            pause(book, 'consecutive_data_errors')
            self.assertFalse(asyncio.run(_recover_market_gap(book, self.feeds(), self.now, None)))
            self.assertEqual(book.state['halted'], halt)

    def test_broker_recovery_checks_open_positions_and_manual_ownership_without_orders(self):
        api, book = FakeAPI('paper'), self.book()
        with tempfile.TemporaryDirectory() as folder:
            mirror = PaperAuto(Path(folder)/'shared.json', api)
            mirror.sync(book)
            now = int(time.time()*1000)
            book._open(now, {'open': 100}, {'asset': 'BTC/USDT', 'stop_fraction': .02,
                                          'regime': 'RANGE', 'decision_ms': now})
            mirror.sync(book)
            posts = api.posts
            self.assertTrue(mirror.reconcile_market_recovery(book))
            self.assertFalse(mirror.recover_transport_halt_if_flat(book))
            self.assertEqual(api.posts, posts)
            api.holdings['BTC/USD']['qty'] += .1
            with self.assertRaises(BrokerError):
                mirror.reconcile_market_recovery(book)
            self.assertEqual(api.posts, posts)

    def test_worker_catchup_never_fills_historical_signal_or_stale_exit(self):
        book, feeds = self.book(opened=True), self.feeds()
        book.state['last_bar'] = {a: 395 * BAR_MS for a in self.cfg.assets}
        feeds['BTC/USDT'][0]['low'][-2] = 90.
        raw = copy.deepcopy(feeds['BTC/USDT'][2])
        raw['low'][raw['t'].index(398 * BAR_MS)] = 90.
        async def fetch(asset, tf, limit, fresh=False):
            return raw if tf == '15m' else feeds[asset][1]
        signal = {'asset': 'ETH/USDT', 'stop_fraction': .02, 'regime': 'RANGE',
                  'strength': .5, 'decision_ms': 397 * BAR_MS}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'account.json'
            _save(book, path)
            with patch.object(price, 'ohlcv', side_effect=fetch), patch.object(price, 'close', new=AsyncMock()), patch(
                    'hermes_trading.growth_run.time.time', return_value=self.now/1000), patch(
                    'hermes_trading.growth_run.signals_for_asset', return_value=[signal]):
                asyncio.run(_paper(self.cfg, path, True))
            import json
            state = json.loads(path.read_text())['state']
            self.assertFalse(state['positions'])
            self.assertFalse(state['pending'])
            self.assertEqual(state['trades'][-1]['closed_ms'], self.now)
            self.assertEqual(state['trades'][-1]['reason'], 'market_data_recovery_stop')

    def test_recovery_exit_must_reconcile_before_entries_resume(self):
        api, book, feeds = FakeAPI('paper'), self.book(), self.feeds()
        with tempfile.TemporaryDirectory() as folder, patch(
                'hermes_trading.alpaca_paper_bridge.time.time', return_value=self.now/1000):
            mirror = PaperAuto(Path(folder)/'shared.json', api)
            mirror.sync(book)
            book._open(390 * BAR_MS, {'open': 100}, {'asset': 'BTC/USDT', 'stop_fraction': .02,
                       'regime': 'RANGE', 'decision_ms': 390 * BAR_MS})
            # This fixture stands for a previously mirrored fill; no old order is submitted.
            state = mirror.load()
            state['auto'] = {'BTC/USDT': .1}
            state['cursor'] = len(book.state['events'])
            mirror.save(state)
            api.holdings = {'BTC/USD': {'qty': .1}}
            book.state['halted'] = 'market_data_gap'
            feeds['BTC/USDT'][0]['low'][-4] = 90.
            self.assertFalse(asyncio.run(_recover_market_gap(book, feeds, self.now, mirror)))
            self.assertEqual(book.state['halted'], 'market_data_gap')
            self.assertEqual(api.posts, 0)
            _save(book, Path(folder)/'account.json')
            mirror.sync(book)
            self.assertEqual(api.posts, 1)
            self.assertFalse(api.holdings)
            self.assertTrue(asyncio.run(_recover_market_gap(book, feeds, self.now, mirror)))
            self.assertIsNone(book.state['halted'])
            mirror.sync(book)
            self.assertEqual(api.posts, 1)

    def test_closed_candles_preserve_source_and_remove_all_future_rows(self):
        raw = self.feeds()['BTC/USDT'][2]
        actual = closed(raw, '15m', self.now - 2*BAR_MS)
        self.assertEqual(actual['source'], 'test')
        self.assertTrue(all(t+BAR_MS <= self.now-2*BAR_MS for t in actual['t']))

    def test_backfill_pages_on_same_source_without_fabricating_missing_rows(self):
        raw = {'source': 'test', 't': [5*BAR_MS], **{k: [100.] for k in ('open','high','low','close')}}
        client = type('Client', (), {})()
        client.fetch_ohlcv = AsyncMock(side_effect=[
            [[0,100,101,99,100], [BAR_MS,100,101,99,100]],
            [[3*BAR_MS,100,101,99,100], [4*BAR_MS,100,101,99,100]]])
        with patch.object(price, '_client', return_value=client) as provider:
            data = asyncio.run(price.backfill('BTC/USDT', raw, 0, 4*BAR_MS))
        self.assertEqual(provider.call_args.args[0], 'test')
        self.assertNotIn(2*BAR_MS, data['t'])
        self.assertEqual(client.fetch_ohlcv.call_args_list[1].kwargs['since'], 2*BAR_MS)


if __name__ == '__main__':
    unittest.main()
