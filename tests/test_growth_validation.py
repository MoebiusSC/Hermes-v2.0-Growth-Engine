import dataclasses
import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist
from unittest.mock import patch

import numpy as np

from hermes_trading.growth import BAR_MS, HOUR_MS, GrowthConfig, Portfolio, replay
from hermes_trading.growth_lab import _settle, assess, validate_history
from hermes_trading.growth_optimizer import initialise, record, reserve_trials
from hermes_trading.growth_validation import (DAY_MS, block_stress, daily_returns,
    deflated_sharpe, effective_observations, probability_overfitting)
from hermes_trading.growth_run import _save, _restore
from hermes_trading.growth_web import snapshot


class ValidationTests(unittest.TestCase):
    def test_dsr_matches_daily_formula_and_penalizes_trials_and_dispersion(self):
        rng = np.random.default_rng(91)
        r = rng.normal(.002, .01, 360)
        trials = [-.03, .02, .05]
        out = deflated_sharpe(r, trials, 5)
        sr = r.mean() / r.std(ddof=1)
        z = (r-r.mean()) / r.std()
        n = effective_observations(r)
        dispersion = max(np.std(trials, ddof=1), 1/math.sqrt(n-1))
        normal = NormalDist()
        expected = dispersion*((1-.5772156649015329)*normal.inv_cdf(.8)
            + .5772156649015329*normal.inv_cdf(1-1/(5*math.e)))
        prob = normal.cdf((sr-expected)*math.sqrt(n-1)/math.sqrt(
            1-np.mean(z**3)*sr+(np.mean(z**4)-1)*sr**2/4))
        self.assertAlmostEqual(out['probability'], prob, places=12)
        self.assertAlmostEqual(out['sharpe_annual'], sr*math.sqrt(365.25))
        self.assertLess(deflated_sharpe(r, trials, 100)['probability'], prob)
        self.assertLess(deflated_sharpe(r, [-.3, 0, .3], 5)['probability'], prob)

    def test_zero_or_tiny_sample_never_passes_and_autocorrelation_reduces_sample(self):
        self.assertEqual(deflated_sharpe(np.zeros(120), [0,0,0], 5)['status'], 'INSUFFICIENT')
        self.assertEqual(deflated_sharpe([.01,.02], [0,.1], 5)['status'], 'INSUFFICIENT')
        persistent = np.repeat(np.random.default_rng(4).normal(0,.01,30), 7)
        self.assertLess(effective_observations(persistent), len(persistent)/3)
        json.dumps(deflated_sharpe(persistent, [0,.1,.2], 5), allow_nan=False)

    def test_cscv_separates_stable_winner_from_overfit_and_handles_ties(self):
        noise = np.random.default_rng(1).normal(0,.01,120)
        stable = np.column_stack([noise+.008, noise, noise-.008])
        self.assertEqual(probability_overfitting(stable)['probability'], 0)
        alternating = np.column_stack([noise+np.repeat([.02,-.02],60),
                                       noise-np.repeat([.02,-.02],60), noise])
        self.assertGreater(probability_overfitting(alternating)['probability'], .5)
        self.assertEqual(probability_overfitting(np.zeros((120,3)))['probability'], 1)
        self.assertEqual(probability_overfitting(stable)['splits'], 70)

    def test_block_bootstrap_is_paired_deterministic_and_rejects_no_advantage(self):
        base = np.random.default_rng(4).normal(0,.002,120)
        result = block_stress(base+.001, base, .06)
        self.assertEqual(result, block_stress(base+.001, base, .06))
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(block_stress(base, base, .06)['status'], 'FAIL')

    def test_calendar_days_do_not_compress_missing_equity(self):
        curve = [{'ts':dt.datetime.fromtimestamp(i*86400,dt.timezone.utc).isoformat(),
                  'equity':50*(1.01**i)} for i in (1,2,3)]
        np.testing.assert_allclose(daily_returns(curve,0,3*DAY_MS,50), [.01]*3)
        with self.assertRaisesRegex(ValueError,'missing_daily_equity'):
            daily_returns(curve[:-1],0,3*DAY_MS,50)
        with self.assertRaises(ValueError):
            daily_returns(curve,BAR_MS,3*DAY_MS,50)

    def test_lifetime_trials_survive_history_truncation_and_restart(self):
        cfg = GrowthConfig()
        state = Portfolio(cfg).state
        meta = initialise(state,0)
        for i in range(40):
            reserve_trials(meta,cfg,i,0)
            record(meta, {'accepted':False, 'reason':'rejected'}, i)
        self.assertEqual(meta['trial_count'],200)
        self.assertEqual(len(meta['history']),30)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'account.json'
            _save(Portfolio(cfg,state),path,cfg)
            reloaded = initialise(_restore(cfg,path).state,0)
            self.assertEqual(reloaded['trial_count'],200)
            payload = snapshot(path)
            self.assertFalse(payload['validation']['live_approved'])
            self.assertEqual(payload['validation']['strategies'][0]['state'],'PAPER_LEGACY')
            self.assertEqual(json.loads(path.read_text())['state']['cash'],50)

    def test_next_open_sizes_do_not_use_another_assets_future_close(self):
        cfg = dataclasses.replace(GrowthConfig(), assets=('BTC/USDT','ETH/USDT'),
                                  daily_loss=.5, weekly_loss=.5, monthly_drawdown=.5)
        def data(final):
            return {'t':[0,BAR_MS,2*BAR_MS], 'open':[100.]*3,
                    'high':[100.,final,final], 'low':[100.,100.,100.], 'close':[100.,final,final]}
        def signals(asset, bars, hourly, cfg):
            if bars['t'][-1] != 0:
                return []
            return [{'asset':asset,'decision_ms':BAR_MS,'regime':'TEST','strength':1.,
                     'stop_fraction':.02,'strategy':'hermes_core'}]
        hours = {a:{'t':[], 'close':[]} for a in cfg.assets}
        with patch('hermes_trading.growth.signals_for_asset',side_effect=signals):
            normal = replay(cfg,{a:data(100) for a in cfg.assets},hours)
            future = replay(cfg,{a:data(200) for a in cfg.assets},hours)
        entries = lambda book:[(e['asset'],e['notional']) for e in book.state['events'] if e['event']=='entry']
        self.assertEqual(entries(normal),entries(future))
        self.assertEqual(len(entries(normal)),2)

    def test_future_paper_quote_is_rejected_and_execution_timestamps_persist(self):
        cfg = GrowthConfig()
        p = Portfolio(cfg)
        signal = {'asset':'BTC/USDT','decision_ms':BAR_MS,'regime':'TEST',
                  'strength':1.,'stop_fraction':.02}
        p.decide([signal],BAR_MS)
        p.paper_fill_pending('BTC/USDT',100,BAR_MS-1)
        self.assertFalse(p.state['positions'])
        p.decide([signal],BAR_MS)
        p.paper_fill_pending('BTC/USDT',100,BAR_MS+10)
        pos = p.state['positions']['BTC/USDT']
        p.state['marks']['BTC/USDT'] = 100.
        self.assertGreaterEqual(pos['fill_time'],pos['decision_time'])
        p.state['curve'] = [{'ts':dt.datetime.fromtimestamp(1800,dt.timezone.utc).isoformat(),
                             'equity':p.equity()}]
        before = p.equity()
        _settle(p,1800000)
        self.assertLess(p.equity(),before)
        self.assertEqual(p.state['trades'][0]['fill_time'],BAR_MS+10)
        self.assertFalse(p.state['positions'])

    def test_hourly_gaps_and_invalid_prices_are_rejected_before_replay(self):
        def data(step):
            ts = list(range(0,143*DAY_MS,step))
            return {'t':ts, **{k:[100.]*len(ts) for k in ('open','high','low','close')}}
        c,h={'BTC/USDT':data(BAR_MS)},{'BTC/USDT':data(HOUR_MS)}
        validate_history(('BTC/USDT',),c,h,142*DAY_MS,120)
        h['BTC/USDT']['t'][5]+=HOUR_MS
        with self.assertRaisesRegex(ValueError,'market_data_gap'):
            validate_history(('BTC/USDT',),c,h,142*DAY_MS,120)
        h={'BTC/USDT':data(HOUR_MS)}
        c['BTC/USDT']['close'][5]=float('nan')
        with self.assertRaisesRegex(ValueError,'invalid_ohlc'):
            validate_history(('BTC/USDT',),c,h,142*DAY_MS,120)

    def test_assessment_on_actual_replay_returns_evidence_and_rejects_flat_market(self):
        cfg = dataclasses.replace(GrowthConfig(),assets=('BTC/USDT',))
        def data(step):
            ts = list(range(0,83*DAY_MS,step))
            return {'t':ts, **{k:[100.]*len(ts) for k in ('open','high','low','close')}}
        c,h={'BTC/USDT':data(BAR_MS)},{'BTC/USDT':data(HOUR_MS)}
        result = assess(cfg,dataclasses.replace(cfg,range_rsi=28),c,h,windows=2,window_days=30)
        self.assertFalse(result['eligible_for_manual_review'])
        v=result['validation']
        self.assertEqual(v['dsr']['status'],'INSUFFICIENT')
        self.assertEqual(v['leakage']['status'],'PASS')
        self.assertEqual(v['walk_forward']['window_days'],30)
        self.assertEqual(v['costs']['double_return'],0)
        self.assertFalse(v['live_approved'])
        json.dumps(result,allow_nan=False)

if __name__=='__main__':
    unittest.main()
