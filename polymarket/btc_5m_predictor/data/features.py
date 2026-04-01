"""Feature engineering for BTC 5-minute direction prediction.

Uses TA-Lib indicators + Qlib-inspired alpha factors on 1-minute klines.
All features are computed at t-1 to avoid lookahead bias for window [t, t+5min).

Feature categories:
  1. TA-Lib indicators          (~80 features)
  2. Momentum                   (~50 features)
  3. Volatility                 (~60 features)
  4. Volume                     (~40 features)
  5. Qlib Alpha factors         (~150 features)
  6. Time features              (~20 features)
  7. Microstructure features    (~30 features)
  8. Futures data features      (~50 features, conditional on data availability)
  9. CVD                        (Cumulative Volume Delta)
 10. Cross-asset                (ETH/BTC signals)
 11. Regime indicators          (ADX, trend strength, vol ratio, autocorrelation)
 12. Behavioral state           (30min participant behavior)
 13. Multi-timeframe            (30min/4h bar features)
 14. Coinbase premium           (US institutional flow signal)
"""

from __future__ import annotations

import os

import httpx
import numpy as np
import pandas as pd
import talib

from config import (
    RETURN_PERIODS,
    VOLATILITY_WINDOWS,
    VOLUME_WINDOWS,
    WINDOW_MINUTES,
)

# ---------------------------------------------------------------------------
# 1. TA-Lib features (~80)
# ---------------------------------------------------------------------------


def _talib_features(df: pd.DataFrame) -> pd.DataFrame:
    """TA-Lib technical indicators on 1m close/high/low/volume."""
    c = df["close"].values.astype(np.float64)
    h = df["high"].values.astype(np.float64)
    l = df["low"].values.astype(np.float64)  # noqa: E741
    o = df["open"].values.astype(np.float64)
    v = df["volume"].values.astype(np.float64)

    cols: dict = {}

    # --- RSI at multiple timeperiods ---
    for tp in [5, 7, 14, 21]:
        cols[f"rsi_{tp}"] = talib.RSI(c, timeperiod=tp)

    # --- MACD ---
    macd, signal, hist = talib.MACD(c, fastperiod=12, slowperiod=26, signalperiod=9)
    cols["macd"] = macd
    cols["macd_signal"] = signal
    cols["macd_hist"] = hist

    # --- Bollinger Bands ---
    upper, middle, lower = talib.BBANDS(c, timeperiod=20, nbdevup=2, nbdevdn=2)
    cols["bb_width"] = (upper - lower) / np.where(middle != 0, middle, 1)
    cols["bb_pos"] = (c - lower) / np.where((upper - lower) != 0, upper - lower, 1)

    # --- ATR at multiple timeperiods ---
    for tp in [5, 10, 14, 21, 30]:
        cols[f"atr_{tp}"] = talib.ATR(h, l, c, timeperiod=tp)

    # --- ADX at multiple timeperiods ---
    for tp in [7, 14, 21]:
        cols[f"adx_{tp}"] = talib.ADX(h, l, c, timeperiod=tp)

    # --- CCI at multiple timeperiods ---
    for tp in [7, 14, 20]:
        cols[f"cci_{tp}"] = talib.CCI(h, l, c, timeperiod=tp)

    # --- Williams %R ---
    for tp in [7, 14, 21]:
        cols[f"willr_{tp}"] = talib.WILLR(h, l, c, timeperiod=tp)

    # --- Momentum ---
    cols["mom_10"] = talib.MOM(c, timeperiod=10)

    # --- OBV (use rolling-window diff only, raw cumsum is start-point dependent) ---
    _obv = pd.Series(talib.OBV(c, v), index=df.index)
    cols["obv_change_5"] = _obv.diff(5)
    cols["obv_change_10"] = _obv.diff(10)
    cols["obv_change_20"] = _obv.diff(20)

    # --- Stochastic ---
    slowk, slowd = talib.STOCH(h, l, c, fastk_period=14, slowk_period=3, slowd_period=3)
    cols["stoch_k"] = slowk
    cols["stoch_d"] = slowd

    # --- TRIX ---
    for tp in [10, 20, 30]:
        cols[f"trix_{tp}"] = talib.TRIX(c, timeperiod=tp)

    # --- Ultimate Oscillator ---
    cols["ultosc"] = talib.ULTOSC(
        h, l, c, timeperiod1=7, timeperiod2=14, timeperiod3=28
    )

    # --- DX, MINUS_DI, PLUS_DI ---
    for tp in [7, 14]:
        cols[f"dx_{tp}"] = talib.DX(h, l, c, timeperiod=tp)
        cols[f"minus_di_{tp}"] = talib.MINUS_DI(h, l, c, timeperiod=tp)
        cols[f"plus_di_{tp}"] = talib.PLUS_DI(h, l, c, timeperiod=tp)

    # --- AROON ---
    for tp in [14, 25]:
        aroon_down, aroon_up = talib.AROON(h, l, timeperiod=tp)
        cols[f"aroon_up_{tp}"] = aroon_up
        cols[f"aroon_down_{tp}"] = aroon_down
        cols[f"aroonosc_{tp}"] = talib.AROONOSC(h, l, timeperiod=tp)

    # --- CMO ---
    for tp in [7, 14]:
        cols[f"cmo_{tp}"] = talib.CMO(c, timeperiod=tp)

    # --- MFI ---
    for tp in [7, 14]:
        cols[f"mfi_{tp}"] = talib.MFI(h, l, c, v, timeperiod=tp)

    # --- PPO ---
    cols["ppo"] = talib.PPO(c, fastperiod=12, slowperiod=26)

    # --- ROC (talib), ROCR ---
    for tp in [5, 10, 20]:
        cols[f"talib_roc_{tp}"] = talib.ROC(c, timeperiod=tp)
        cols[f"talib_rocr_{tp}"] = talib.ROCR(c, timeperiod=tp)

    # --- Overlap studies: DEMA, TEMA, KAMA, T3, MIDPOINT, MIDPRICE ---
    for tp in [10, 20, 30]:
        cols[f"dema_{tp}"] = (talib.DEMA(c, timeperiod=tp) - c) / (c + 1e-10)
        cols[f"tema_{tp}"] = (talib.TEMA(c, timeperiod=tp) - c) / (c + 1e-10)

    for tp in [10, 20]:
        cols[f"kama_{tp}"] = (talib.KAMA(c, timeperiod=tp) - c) / (c + 1e-10)

    cols["t3_5"] = (talib.T3(c, timeperiod=5) - c) / (c + 1e-10)

    for tp in [10, 20]:
        cols[f"midpoint_{tp}"] = (talib.MIDPOINT(c, timeperiod=tp) - c) / (c + 1e-10)
        cols[f"midprice_{tp}"] = (talib.MIDPRICE(h, l, timeperiod=tp) - c) / (c + 1e-10)

    # --- NATR, TRANGE ---
    cols["natr_14"] = talib.NATR(h, l, c, timeperiod=14)
    cols["trange"] = talib.TRANGE(h, l, c)

    # --- Candlestick pattern recognition ---
    cols["cdl_hammer"] = talib.CDLHAMMER(o, h, l, c)
    cols["cdl_engulfing"] = talib.CDLENGULFING(o, h, l, c)
    cols["cdl_doji"] = talib.CDLDOJI(o, h, l, c)
    cols["cdl_morningstar"] = talib.CDLMORNINGSTAR(o, h, l, c, penetration=0.3)
    cols["cdl_shootingstar"] = talib.CDLSHOOTINGSTAR(o, h, l, c)
    cols["cdl_harami"] = talib.CDLHARAMI(o, h, l, c)
    cols["cdl_eveningstar"] = talib.CDLEVENINGSTAR(o, h, l, c, penetration=0.3)
    cols["cdl_3whitesoldiers"] = talib.CDL3WHITESOLDIERS(o, h, l, c)
    cols["cdl_3blackcrows"] = talib.CDL3BLACKCROWS(o, h, l, c)
    cols["cdl_spinningtop"] = talib.CDLSPINNINGTOP(o, h, l, c)
    cols["cdl_marubozu"] = talib.CDLMARUBOZU(o, h, l, c)
    cols["cdl_dragonfly_doji"] = talib.CDLDRAGONFLYDOJI(o, h, l, c)

    # --- Fast Stochastic ---
    for fk, fd in [(5, 3), (14, 3)]:
        fastk, fastd = talib.STOCHF(h, l, c, fastk_period=fk, fastd_period=fd)
        cols[f"stochf_k_{fk}"] = fastk
        cols[f"stochf_d_{fk}"] = fastd

    # --- Directional Movement (+DM/-DM ratio) ---
    for tp in [7, 14]:
        plus_dm_val = talib.PLUS_DM(h, l, timeperiod=tp)
        minus_dm_val = talib.MINUS_DM(h, l, timeperiod=tp)
        cols[f"plus_dm_{tp}"] = plus_dm_val
        cols[f"minus_dm_{tp}"] = minus_dm_val
        cols[f"dm_ratio_{tp}"] = plus_dm_val / (minus_dm_val + 1e-10)

    # --- ADXR (smoothed ADX) ---
    cols["adxr_7"] = talib.ADXR(h, l, c, timeperiod=7)

    # --- Hilbert Transform (cycle indicators) ---
    sine_ht, leadsine_ht = talib.HT_SINE(c)
    cols["ht_sine"] = sine_ht
    cols["ht_leadsine"] = leadsine_ht
    inphase_ht, quadrature_ht = talib.HT_PHASOR(c)
    cols["ht_inphase"] = inphase_ht
    cols["ht_quadrature"] = quadrature_ht

    # --- Chaikin A/D Oscillator ---
    cols["adosc_3_10"] = talib.ADOSC(h, l, c, v, fastperiod=3, slowperiod=10)

    # --- Price-volume correlation ---
    for tp in [10, 30]:
        cols[f"correl_pv_{tp}"] = talib.CORREL(c, v, timeperiod=tp)

    # --- Balance of Power ---
    bop = talib.BOP(o, h, l, c)
    bop_s = pd.Series(bop, index=df.index)
    for w in [5, 10, 20]:
        cols[f"bop_ma_{w}"] = bop_s.rolling(w).mean()

    # --- Price auto-beta ---
    c_lag = np.roll(c, 1)
    c_lag[0] = c[0]
    for tp in [10, 20]:
        cols[f"beta_lag_{tp}"] = talib.BETA(c, c_lag, timeperiod=tp)

    # --- WMA / TRIMA distance ---
    cols["wma_dist_30"] = (c - talib.WMA(c, timeperiod=30)) / (c + 1e-10)
    cols["trima_dist_20"] = (c - talib.TRIMA(c, timeperiod=20)) / (c + 1e-10)

    # --- Linear regression slope / angle / residual ---
    for tp in [10, 20]:
        cols[f"linreg_slope_{tp}"] = talib.LINEARREG_SLOPE(c, timeperiod=tp)
    cols["linreg_angle_5"] = talib.LINEARREG_ANGLE(c, timeperiod=5)
    linreg_20 = talib.LINEARREG(c, timeperiod=20)
    cols["linreg_resid_20"] = (c - linreg_20) / (c + 1e-10)
    tsf_10 = talib.TSF(c, timeperiod=10)
    cols["tsf_diff_10"] = (tsf_10 - c) / (c + 1e-10)

    # --- APO (Absolute Price Oscillator) ---
    cols["apo_5_10"] = talib.APO(c, fastperiod=5, slowperiod=10)

    # --- StochRSI ---
    for tp in [7, 14]:
        _, fastd_srsi = talib.STOCHRSI(c, timeperiod=tp, fastk_period=5, fastd_period=3)
        cols[f"stochrsi_d_{tp}"] = fastd_srsi

    # --- MESA Adaptive Moving Average ---
    mama_val, fama_val = talib.MAMA(c, fastlimit=0.5, slowlimit=0.05)
    cols["mama_dist"] = (c - mama_val) / (c + 1e-10)
    cols["mama_fama_dist"] = (mama_val - fama_val) / (c + 1e-10)

    # --- MACDEXT with DEMA ---
    _, _, hist_dema = talib.MACDEXT(
        c,
        fastperiod=12,
        fastmatype=2,
        slowperiod=26,
        slowmatype=2,
        signalperiod=9,
        signalmatype=2,
    )
    cols["macdext_dema_hist"] = hist_dema

    # --- SAR distance ---
    sar = talib.SAR(h, l, acceleration=0.02, maximum=0.2)
    cols["sar_dist"] = (c - sar) / (c + 1e-10)

    # --- Typical price distance ---
    cols["typprice_dist"] = (c - talib.TYPPRICE(h, l, c)) / (c + 1e-10)

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 2. Momentum features (~50)
# ---------------------------------------------------------------------------


def _momentum_features(df: pd.DataFrame) -> pd.DataFrame:
    """Price return features at multiple lookback periods."""
    c = df["close"]
    cols: dict = {}

    # Extended return periods
    all_ret_periods = [1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 45, 60]
    for n in all_ret_periods:
        cols[f"ret_{n}"] = c.pct_change(n)

    # Log returns
    log_c = np.log(c)
    for n in [1, 3, 5, 10, 20, 30, 60]:
        cols[f"logret_{n}"] = log_c.diff(n)

    # Return acceleration (second derivative)
    for n in [3, 5, 10, 20]:
        ret_n = c.pct_change(n)
        cols[f"ret_accel_{n}"] = ret_n - ret_n.shift(n)

    # Z-score of returns
    ret_1 = c.pct_change()
    for w in [10, 20, 30, 60]:
        rmean = ret_1.rolling(w).mean()
        rstd = ret_1.rolling(w).std()
        cols[f"ret_zscore_{w}"] = (ret_1 - rmean) / (rstd + 1e-10)

    # EMA crossover signals (more pairs)
    for fast, slow in [
        (3, 7),
        (5, 10),
        (5, 15),
        (7, 21),
        (10, 20),
        (10, 30),
        (12, 26),
        (20, 50),
        (5, 30),
    ]:
        ema_f = c.ewm(span=fast, adjust=False).mean()
        ema_s = c.ewm(span=slow, adjust=False).mean()
        cols[f"ema_cross_{fast}_{slow}"] = (ema_f > ema_s).astype(np.int8)
        cols[f"ema_dist_{fast}_{slow}"] = (ema_f - ema_s) / (ema_s + 1e-10)

    # Distance from rolling max/min (drawdown/rally)
    for w in [10, 20, 30, 60]:
        rmax = c.rolling(w).max()
        rmin = c.rolling(w).min()
        cols[f"drawdown_{w}"] = (c - rmax) / (rmax + 1e-10)
        cols[f"rally_{w}"] = (c - rmin) / (rmin + 1e-10)

    # Rolling VWAP only (cumulative VWAP is start-point dependent)
    for w in [10, 20, 60]:
        cv = (df["close"] * df["volume"]).rolling(w).sum()
        vs = df["volume"].rolling(w).sum()
        rvwap = cv / (vs + 1e-10)
        cols[f"price_vs_rvwap_{w}"] = (c - rvwap) / (rvwap + 1e-10)

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 3. Volatility features (~60)
# ---------------------------------------------------------------------------


def _volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    """Rolling volatility and related features."""
    c = df["close"]
    h_s = df["high"]
    l_s = df["low"]
    ret = c.pct_change()
    log_ret = np.log(c).diff()
    cols: dict = {}

    # Basic rolling volatility
    for w in VOLATILITY_WINDOWS:
        cols[f"vol_{w}"] = ret.rolling(w).std()

    # Volatility ratios (short/long)
    for short, long in [(5, 30), (10, 60), (5, 15), (10, 30), (15, 60)]:
        vol_s = ret.rolling(short).std()
        vol_l = ret.rolling(long).std()
        cols[f"vol_ratio_{short}_{long}"] = vol_s / (vol_l + 1e-10)

    # High-low range
    for w in [5, 10, 20, 30, 60]:
        cols[f"hl_range_{w}"] = (h_s.rolling(w).max() - l_s.rolling(w).min()) / c

    # --- Parkinson volatility ---
    hl_log = np.log(h_s / l_s)
    parkinson_factor = 1.0 / (4.0 * np.log(2))
    for w in [5, 10, 20, 30, 60]:
        cols[f"parkinson_vol_{w}"] = np.sqrt(
            (hl_log**2).rolling(w).mean() * parkinson_factor
        )

    # --- Garman-Klass volatility ---
    log_hl = np.log(h_s / l_s) ** 2
    log_co = np.log(c / df["open"]) ** 2
    for w in [5, 10, 20, 30, 60]:
        cols[f"garman_klass_vol_{w}"] = np.sqrt(
            (0.5 * log_hl - (2 * np.log(2) - 1) * log_co).rolling(w).mean()
        )

    # --- Rogers-Satchell volatility ---
    log_hc = np.log(h_s / c)
    log_ho = np.log(h_s / df["open"])
    log_lc = np.log(l_s / c)
    log_lo = np.log(l_s / df["open"])
    rs_var = log_ho * log_hc + log_lo * log_lc
    for w in [10, 20, 30]:
        cols[f"rogers_satchell_vol_{w}"] = np.sqrt(
            rs_var.rolling(w).mean().clip(lower=0)
        )

    # --- Yang-Zhang volatility ---
    log_oc = np.log(df["open"] / c.shift(1))
    log_co_yz = np.log(c / df["open"])
    for w in [10, 20, 30]:
        var_o = log_oc.rolling(w).var()
        var_c = log_co_yz.rolling(w).var()
        var_rs = rs_var.rolling(w).mean().clip(lower=0)
        k = 0.34 / (1.34 + (w + 1) / (w - 1))
        cols[f"yang_zhang_vol_{w}"] = np.sqrt(
            (var_o + k * var_c + (1 - k) * var_rs).clip(lower=0)
        )

    # --- Realized volatility (sum of squared returns) ---
    sq_ret = log_ret**2
    for w in [5, 10, 20, 30, 60]:
        cols[f"realized_vol_{w}"] = np.sqrt(sq_ret.rolling(w).sum())

    # --- Volatility of volatility ---
    for w in [20, 60]:
        inner_vol = ret.rolling(10).std()
        cols[f"vol_of_vol_{w}"] = inner_vol.rolling(w).std()

    # --- Volatility regime: rolling percentile ---
    for w in [60, 120]:
        vol_10 = ret.rolling(10).std()
        cols[f"vol_percentile_{w}"] = vol_10.rolling(w).apply(
            lambda x: np.sum(x[-1] >= x[:-1]) / (len(x) - 1) if len(x) > 1 else np.nan,
            raw=True,
        )

    # --- ATR ratio at multiple scales ---
    for s, l in [(5, 20), (10, 30), (14, 60)]:
        atr_s = pd.Series(
            talib.ATR(h_s.values, l_s.values, c.values, timeperiod=s), index=df.index
        )
        atr_l = pd.Series(
            talib.ATR(h_s.values, l_s.values, c.values, timeperiod=l), index=df.index
        )
        cols[f"atr_ratio_{s}_{l}"] = atr_s / (atr_l + 1e-10)

    # --- Rolling skewness and kurtosis ---
    for w in [10, 20, 30, 60]:
        cols[f"skew_{w}"] = ret.rolling(w).skew()
        cols[f"kurt_{w}"] = ret.rolling(w).kurt()

    # --- Max drawdown over rolling windows ---
    for w in [20, 60, 120]:

        def _max_dd(x):
            cummax = np.maximum.accumulate(x)
            dd = (x - cummax) / (cummax + 1e-10)
            return np.min(dd)

        cols[f"max_dd_{w}"] = c.rolling(w).apply(_max_dd, raw=True)

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 4. Volume features (~40)
# ---------------------------------------------------------------------------


def _volume_features(df: pd.DataFrame) -> pd.DataFrame:
    """Volume-based features."""
    v = df["volume"]
    c = df["close"]
    h_s = df["high"]
    l_s = df["low"]
    tbv = df["taker_buy_base"]
    tbq = df["taker_buy_quote"]
    cols: dict = {}

    # Volume ratio vs rolling mean
    for w in VOLUME_WINDOWS:
        v_mean = v.rolling(w).mean()
        cols[f"vratio_{w}"] = v / (v_mean + 1e-10)
        cols[f"buy_ratio_{w}"] = tbv.rolling(w).sum() / (v.rolling(w).sum() + 1e-10)

    # Volume trend
    cols["vol_trend_10"] = v.rolling(10).mean().pct_change(5)

    # Volume momentum (change in volume)
    for n in [1, 3, 5, 10, 20]:
        cols[f"vol_mom_{n}"] = v.pct_change(n)

    # On-Balance Volume (rolling diffs only — raw cumsum is start-point dependent)
    obv = talib.OBV(c.values, v.values)
    obv_s = pd.Series(obv, index=df.index)
    for w in [5, 10, 20]:
        cols[f"obv_slope_{w}"] = obv_s.diff(w) / (w * (v.rolling(w).mean() + 1e-10))

    # Volume-Price Trend (rolling diffs only)
    vpt = (v * c.pct_change()).cumsum()
    for w in [5, 10, 20]:
        cols[f"vpt_slope_{w}"] = vpt.diff(w)
    # Rolling VPT sum (start-point invariant alternative to raw vpt)
    vpt_bar = v * c.pct_change()
    for w in [10, 20, 30]:
        cols[f"vpt_sum_{w}"] = vpt_bar.rolling(w).sum()

    # Accumulation/Distribution (rolling diffs only)
    clv = ((c - l_s) - (h_s - c)) / (h_s - l_s + 1e-10)
    ad_bar = clv * v
    ad = ad_bar.cumsum()
    cols["ad_slope_10"] = ad.diff(10)
    cols["ad_slope_20"] = ad.diff(20)
    # Rolling AD sum (start-point invariant)
    for w in [10, 20, 30]:
        cols[f"ad_sum_{w}"] = ad_bar.rolling(w).sum()

    # Chaikin Money Flow at multiple periods
    for w in [10, 20, 30]:
        cols[f"cmf_{w}"] = (clv * v).rolling(w).sum() / (v.rolling(w).sum() + 1e-10)

    # Taker buy ratio at more granularities
    for w in [3, 5, 7, 10, 15, 20, 30, 60]:
        cols[f"taker_buy_ratio_{w}"] = tbv.rolling(w).sum() / (
            v.rolling(w).sum() + 1e-10
        )

    # Trade count features (if column exists)
    if "trades" in df.columns:
        trades = df["trades"]
        for w in [5, 10, 20]:
            cols[f"trades_ratio_{w}"] = trades / (trades.rolling(w).mean() + 1e-10)
        # Average trade size
        avg_trade_size = v / (trades + 1e-10)
        cols["avg_trade_size"] = avg_trade_size
        cols["avg_trade_size_change_5"] = avg_trade_size.pct_change(5)
    elif "number_of_trades" in df.columns:
        trades = df["number_of_trades"]
        for w in [5, 10, 20]:
            cols[f"trades_ratio_{w}"] = trades / (trades.rolling(w).mean() + 1e-10)
        avg_trade_size = v / (trades + 1e-10)
        cols["avg_trade_size"] = avg_trade_size
        cols["avg_trade_size_change_5"] = avg_trade_size.pct_change(5)

    # Volume Z-score (abnormal volume detection)
    for w in [20, 60]:
        vmean = v.rolling(w).mean()
        vstd = v.rolling(w).std()
        cols[f"vol_zscore_{w}"] = (v - vmean) / (vstd + 1e-10)

    # Volume entropy/concentration (Herfindahl index over rolling window)
    for w in [10, 20]:

        def _vol_hhi(x):
            total = np.sum(x)
            if total <= 0:
                return np.nan
            shares = x / total
            return np.sum(shares**2)

        cols[f"vol_hhi_{w}"] = v.rolling(w).apply(_vol_hhi, raw=True)

    # Volume-weighted price momentum
    for w in [10, 20]:
        cv = (c * v).rolling(w).sum()
        vs = v.rolling(w).sum()
        vwap_w = cv / (vs + 1e-10)
        cols[f"vw_price_mom_{w}"] = c / (vwap_w + 1e-10) - 1

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 5. Qlib Alpha factors (~150)
# ---------------------------------------------------------------------------


def _qlib_alpha_factors(df: pd.DataFrame) -> pd.DataFrame:
    """Qlib Alpha158-inspired factors, hand-implemented for 1m klines.

    Reference: qlib/contrib/data/handler.py Alpha158
    """
    c, o, h, l, v = (
        df["close"],
        df["open"],
        df["high"],
        df["low"],
        df["volume"],
    )
    ret = c.pct_change()
    cols: dict = {}

    # --- Candle features ---
    # KLEN: candle body relative to open
    cols["KLEN"] = (c - o) / (o + 1e-10)
    # KSFT: close position within candle
    cols["KSFT"] = (2 * c - h - l) / (o + 1e-10)
    # KLEN2: candle range relative to open
    cols["KLEN2"] = (h - l) / (o + 1e-10)

    # KMID, KLOW, KUP at more scales
    for w in [5, 10, 20, 30]:
        sma = c.rolling(w).mean()
        cols[f"KMID_{w}"] = (c - sma) / (sma + 1e-10)
        cols[f"KUP_{w}"] = (h.rolling(w).max() - c) / (c + 1e-10)
        cols[f"KLOW_{w}"] = (c - l.rolling(w).min()) / (c + 1e-10)

    # --- ROC at various periods ---
    for n in [3, 5, 10, 15, 20, 30, 60]:
        cols[f"ROC_{n}"] = c / c.shift(n) - 1

    # --- DELTA: close - close.shift(n) ---
    for n in [1, 3, 5, 10, 20, 30]:
        cols[f"DELTA_{n}"] = (c - c.shift(n)) / (c.shift(n) + 1e-10)

    # --- RSQR: R-squared of linear regression on close ---
    for w in [10, 20, 30]:
        cols[f"RSQR_{w}"] = c.rolling(w).apply(
            lambda x: (
                np.corrcoef(np.arange(len(x)), x)[0, 1] ** 2 if len(x) >= 2 else np.nan
            ),
            raw=True,
        )

    # --- RESI: residual of linear regression ---
    for w in [10, 20, 30]:

        def _resi(x):
            if len(x) < 2:
                return np.nan
            t = np.arange(len(x))
            slope, intercept = np.polyfit(t, x, 1)
            predicted = slope * t[-1] + intercept
            return (x[-1] - predicted) / (np.std(x) + 1e-10)

        cols[f"RESI_{w}"] = c.rolling(w).apply(_resi, raw=True)

    # --- MAX/MIN: rolling max/min position ---
    for w in [5, 10, 20, 30, 60]:
        cols[f"MAX_{w}"] = h.rolling(w).max() / c - 1
        cols[f"MIN_{w}"] = c / l.rolling(w).min() - 1

    # --- QTLU/QTLD: quantile position ---
    for w in [5, 10, 15, 20, 30]:
        cols[f"QTLU_{w}"] = c.rolling(w).apply(
            lambda x: np.sum(x[-1] > x[:-1]) / (len(x) - 1) if len(x) > 1 else np.nan,
            raw=True,
        )

    # --- RANK: rolling percentile rank of close ---
    for w in [20, 60, 120]:
        cols[f"RANK_{w}"] = c.rolling(w).apply(
            lambda x: np.sum(x[-1] >= x) / len(x) if len(x) > 0 else np.nan,
            raw=True,
        )

    # --- CORR: rolling correlation of close change and volume change ---
    for w in [5, 10, 20, 30]:
        cols[f"CORR_{w}"] = c.pct_change().rolling(w).corr(v.pct_change())

    # --- CORD: correlation of delta(close) and delta(volume) ---
    dc = c.diff()
    dv = v.diff()
    for w in [10, 20, 30]:
        cols[f"CORD_{w}"] = dc.rolling(w).corr(dv)

    # --- CNTP/CNTN: proportion of positive/negative returns ---
    for w in [5, 10, 20, 30]:
        cols[f"CNTP_{w}"] = ret.rolling(w).apply(lambda x: np.mean(x > 0), raw=True)
        cols[f"CNTN_{w}"] = ret.rolling(w).apply(lambda x: np.mean(x < 0), raw=True)

    # --- SUMP/SUMN: sum of positive/negative returns ---
    for w in [5, 10, 20, 30]:
        cols[f"SUMP_{w}"] = ret.rolling(w).apply(lambda x: np.sum(x[x > 0]), raw=True)
        cols[f"SUMN_{w}"] = ret.rolling(w).apply(
            lambda x: np.abs(np.sum(x[x < 0])), raw=True
        )

    # --- SUMD: sum of consecutive same-direction moves ---
    for w in [5, 10, 20]:

        def _sumd(x):
            signs = np.sign(x)
            count = 0
            for i in range(1, len(x)):
                if signs[i] == signs[i - 1] and signs[i] != 0:
                    count += 1
                else:
                    count -= 1
            return count / len(x)

        cols[f"SUMD_{w}"] = ret.rolling(w).apply(_sumd, raw=True)

    # --- VMA: volume moving average ratio ---
    for w in [5, 10, 20, 30]:
        cols[f"VMA_{w}"] = v.rolling(w).mean() / (v + 1e-10)

    # --- VSTD: volume standard deviation ---
    for w in [5, 10, 20, 30]:
        cols[f"VSTD_{w}"] = v.rolling(w).std() / (v.rolling(w).mean() + 1e-10)

    # --- WVMA: volume-weighted moving average ---
    for w in [5, 10, 20]:
        wvma = (c * v).rolling(w).sum() / (v.rolling(w).sum() + 1e-10)
        cols[f"WVMA_{w}"] = (c - wvma) / (wvma + 1e-10)

    # --- STD at multiple windows ---
    for w in [5, 10, 20, 30, 60]:
        cols[f"STD_{w}"] = ret.rolling(w).std()

    # --- BETA: rolling beta vs lagged returns (pseudo-market) ---
    mkt = ret.shift(1)  # lagged return as pseudo-market
    for w in [20, 60]:
        cov_rm = ret.rolling(w).cov(mkt)
        var_m = mkt.rolling(w).var()
        cols[f"BETA_{w}"] = cov_rm / (var_m + 1e-10)

    # --- Autocorrelation of returns at multiple lags ---
    for lag in [1, 2, 3, 5, 10]:
        for w in [20, 60]:
            cols[f"autocorr_lag{lag}_{w}"] = ret.rolling(w).apply(
                lambda x: pd.Series(x).autocorr(lag=lag) if len(x) > lag else np.nan,
                raw=True,
            )

    # --- Hurst exponent approximation (rolling, R/S method) ---
    for w in [30, 60]:

        def _hurst(x):
            n = len(x)
            if n < 10:
                return np.nan
            mean_x = np.mean(x)
            dev = np.cumsum(x - mean_x)
            r = np.max(dev) - np.min(dev)
            s = np.std(x, ddof=1)
            if s < 1e-10:
                return np.nan
            return np.log(r / s + 1e-10) / np.log(n)

        cols[f"hurst_{w}"] = ret.rolling(w).apply(_hurst, raw=True)

    # --- Mean reversion indicator ---
    for w in [10, 20, 30, 60]:
        sma = c.rolling(w).mean()
        std = c.rolling(w).std()
        cols[f"mean_rev_{w}"] = (c - sma) / (std + 1e-10)

    # --- Trend strength: (close - close.shift(n)) / ATR ---
    atr_14 = pd.Series(
        talib.ATR(h.values, l.values, c.values, timeperiod=14), index=df.index
    )
    for n in [5, 10, 20, 30]:
        cols[f"trend_strength_{n}"] = (c - c.shift(n)) / (atr_14 + 1e-10)

    # --- Rolling Sharpe of returns ---
    for w in [20, 60, 120]:
        rmean = ret.rolling(w).mean()
        rstd = ret.rolling(w).std()
        cols[f"rolling_sharpe_{w}"] = rmean / (rstd + 1e-10)

    # --- Information ratio style: return / tracking error vs SMA ---
    for w in [20, 60]:
        sma = c.rolling(w).mean()
        tracking = (c - sma).rolling(w).std()
        cols[f"info_ratio_{w}"] = (c - sma).rolling(w).mean() / (tracking + 1e-10)

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 6. Time features (~20)
# ---------------------------------------------------------------------------


def _time_features(df: pd.DataFrame) -> pd.DataFrame:
    """Cyclical time encodings and session flags."""
    cols: dict = {}
    ts = df["open_time"]

    # Hour of day (sin/cos)
    hour = ts.dt.hour + ts.dt.minute / 60.0
    cols["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    cols["hour_cos"] = np.cos(2 * np.pi * hour / 24)

    # Day of week (sin/cos)
    dow = ts.dt.dayofweek
    cols["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    cols["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    # Minute of day (sin/cos)
    minute_of_day = ts.dt.hour * 60 + ts.dt.minute
    cols["mod_sin"] = np.sin(2 * np.pi * minute_of_day / 1440)
    cols["mod_cos"] = np.cos(2 * np.pi * minute_of_day / 1440)

    # Trading sessions (UTC-based)
    hour_int = ts.dt.hour
    # Asian session: 00:00-08:00 UTC
    cols["is_asian"] = ((hour_int >= 0) & (hour_int < 8)).astype(np.int8)
    # European session: 07:00-16:00 UTC
    cols["is_european"] = ((hour_int >= 7) & (hour_int < 16)).astype(np.int8)
    # US session: 13:00-22:00 UTC
    cols["is_us"] = ((hour_int >= 13) & (hour_int < 22)).astype(np.int8)

    # Session overlap periods (high volatility)
    cols["is_asia_europe_overlap"] = ((hour_int >= 7) & (hour_int < 8)).astype(np.int8)
    cols["is_europe_us_overlap"] = ((hour_int >= 13) & (hour_int < 16)).astype(np.int8)

    # Minutes since session open (cyclic for each session)
    # Asian session start at 00:00 UTC
    cols["min_since_asian_open"] = minute_of_day
    # European session start at 07:00 UTC
    cols["min_since_euro_open"] = (minute_of_day - 420) % 1440
    # US session start at 13:00 UTC
    cols["min_since_us_open"] = (minute_of_day - 780) % 1440

    # Weekend flag
    cols["is_weekend"] = (dow >= 5).astype(np.int8)

    # Hour of day (sin/cos for 4-hour cycle - captures intraday patterns)
    cols["hour4_sin"] = np.sin(2 * np.pi * hour / 4)
    cols["hour4_cos"] = np.cos(2 * np.pi * hour / 4)

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 7. Microstructure features (~30)
# ---------------------------------------------------------------------------


def _microstructure_features(df: pd.DataFrame) -> pd.DataFrame:
    """Market microstructure features from kline data."""
    c = df["close"]
    h_s = df["high"]
    l_s = df["low"]
    v = df["volume"]
    ret = c.pct_change()
    cols: dict = {}

    # --- Bid-ask spread proxy ---
    spread_proxy = 2 * (h_s - l_s) / (h_s + l_s + 1e-10)
    cols["spread_proxy"] = spread_proxy
    for w in [5, 10, 20]:
        cols[f"spread_proxy_ma_{w}"] = spread_proxy.rolling(w).mean()

    # --- Kyle's lambda approximation: |return| / volume ---
    abs_ret = ret.abs()
    kyle_lambda = abs_ret / (v + 1e-10)
    cols["kyle_lambda"] = kyle_lambda
    for w in [10, 20]:
        cols[f"kyle_lambda_ma_{w}"] = kyle_lambda.rolling(w).mean()

    # --- Amihud illiquidity: |return| / dollar_volume ---
    dollar_vol = c * v
    amihud = abs_ret / (dollar_vol + 1e-10)
    cols["amihud"] = amihud
    for w in [10, 20, 60]:
        cols[f"amihud_ma_{w}"] = amihud.rolling(w).mean()

    # --- Roll's spread estimator: 2*sqrt(-cov(ret_t, ret_{t-1})) ---
    for w in [20, 60]:

        def _roll_spread(x):
            if len(x) < 3:
                return np.nan
            cov = np.cov(x[1:], x[:-1])[0, 1]
            return 2 * np.sqrt(max(-cov, 0))

        cols[f"roll_spread_{w}"] = ret.rolling(w).apply(_roll_spread, raw=True)

    # --- Trade flow imbalance ---
    if "taker_buy_base" in df.columns:
        tbv = df["taker_buy_base"]
        sell_vol = v - tbv
        imbalance = (tbv - sell_vol) / (v + 1e-10)
        cols["flow_imbalance"] = imbalance
        for w in [3, 5, 10, 20, 30]:
            cols[f"flow_imbalance_ma_{w}"] = imbalance.rolling(w).mean()
        # Cumulative imbalance
        for w in [10, 20]:
            cols[f"cum_imbalance_{w}"] = imbalance.rolling(w).sum()

    # --- VPIN approximation ---
    # Volume-synchronized probability of informed trading
    abs_vol_delta = abs_ret * v
    for w in [20, 60]:
        cols[f"vpin_{w}"] = abs_vol_delta.rolling(w).sum() / (
            v.rolling(w).sum() + 1e-10
        )

    # --- Order flow toxicity ---
    for w in [10, 20, 30]:
        signed_vol = ret.apply(np.sign) * v
        cols[f"toxicity_{w}"] = signed_vol.rolling(w).sum() / (
            v.rolling(w).sum() + 1e-10
        )

    # --- Price impact: ret / log(volume) ---
    price_impact = ret / (np.log(v + 1) + 1e-10)
    cols["price_impact"] = price_impact
    for w in [10, 20]:
        cols[f"price_impact_ma_{w}"] = price_impact.rolling(w).mean()

    return pd.DataFrame(cols, index=df.index)


# ---------------------------------------------------------------------------
# 8. Futures data features (~50, conditional) + Hyperliquid (~14)
# ---------------------------------------------------------------------------


def _load_hl_funding(df_1m: pd.DataFrame) -> pd.Series | None:
    """Load Hyperliquid funding rate and align to 1m index.

    Returns aligned Series or None if data unavailable.
    """
    from pathlib import Path

    data_dir = Path(__file__).parent / "raw"
    raw = None

    # Try DuckDB first
    try:
        import db as _db
        raw = _db.read_hl_funding()
        if raw is not None and len(raw) > 0:
            pass
        else:
            raw = None
    except Exception:
        raw = None

    # Fall back to parquet
    if raw is None:
        fpath = data_dir / "hl_btc_funding_rate.parquet"
        try:
            raw = pd.read_parquet(fpath)
        except (FileNotFoundError, OSError):
            return None

    if raw is None or raw.empty:
        return None

    src = raw[["datetime", "fundingRate"]].copy()
    src.columns = ["dt", "val"]
    src["val"] = src["val"].astype(float)
    src["dt"] = pd.to_datetime(src["dt"], utc=True, errors="coerce")
    src = src.dropna(subset=["dt"]).sort_values("dt").drop_duplicates(subset=["dt"])

    temp = df_1m[["open_time"]].copy()
    temp["open_time"] = pd.to_datetime(temp["open_time"], utc=True).astype("datetime64[us, UTC]")
    src = src.rename(columns={"dt": "open_time"})
    src["open_time"] = src["open_time"].astype("datetime64[us, UTC]")
    merged = pd.merge_asof(
        temp.sort_values("open_time"),
        src.sort_values("open_time"),
        on="open_time",
        direction="backward",
    )
    return pd.Series(merged["val"].values, index=df_1m.index).ffill()


def _futures_features(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Features from futures data (funding rate, OI, long/short ratios, taker volume).

    Each data source is loaded from parquet, forward-filled to 1m frequency.
    If a file doesn't exist, it is silently skipped.
    """
    from pathlib import Path

    data_dir = Path(__file__).parent / "raw"
    cols: dict = {}

    # Mapping from parquet filename to DuckDB table name
    _parquet_to_table = {
        "btcusdt_funding_rate.parquet": "futures_funding_rate",
        "btcusdt_open_interest.parquet": "futures_open_interest",
        "btcusdt_top_ls_account.parquet": "futures_top_ls_account",
        "btcusdt_top_ls_position.parquet": "futures_top_ls_position",
        "btcusdt_global_ls.parquet": "futures_global_ls",
        "btcusdt_taker_volume.parquet": "futures_taker_volume",
    }

    # Helper: load, forward-fill, align to 1m index
    def _load_and_align(filename: str, value_col: str, prefix: str):
        raw = None

        # Try DuckDB first
        table_name = _parquet_to_table.get(filename)
        if table_name is not None:
            try:
                import db

                raw = db.read_futures(table_name)
                if raw is not None and len(raw) > 0:
                    pass  # success
                else:
                    raw = None
            except Exception:
                raw = None

        # Fall back to parquet
        if raw is None:
            fpath = data_dir / filename
            try:
                raw = pd.read_parquet(fpath)
            except (FileNotFoundError, OSError):
                return

        if raw is None or raw.empty:
            return

        # Use 'datetime' column if available, else fall back
        time_col = None
        for tc in ["datetime", "open_time", "timestamp"]:
            if tc in raw.columns:
                time_col = tc
                break
        if time_col is None:
            return

        # Pick value column
        if value_col not in raw.columns:
            num_cols = raw.select_dtypes(include=[np.number]).columns
            if len(num_cols) == 0:
                return
            value_col = num_cols[0]

        src = raw[[time_col, value_col]].copy()
        src.columns = ["dt", "val"]
        src["val"] = src["val"].astype(float)
        src["dt"] = pd.to_datetime(src["dt"], utc=True, errors="coerce")
        src = src.dropna(subset=["dt"]).sort_values("dt").drop_duplicates(subset=["dt"])

        # merge_asof: align to 1m open_time (backward fill)
        temp = df_1m[["open_time"]].copy()
        temp["open_time"] = pd.to_datetime(temp["open_time"], utc=True)
        src = src.rename(columns={"dt": "open_time"})
        # Normalize to same resolution to avoid MergeError
        temp["open_time"] = temp["open_time"].astype("datetime64[us, UTC]")
        src["open_time"] = src["open_time"].astype("datetime64[us, UTC]")
        merged = pd.merge_asof(
            temp.sort_values("open_time"),
            src.sort_values("open_time"),
            on="open_time",
            direction="backward",
        )
        aligned = pd.Series(merged["val"].values, index=df_1m.index).ffill()

        # Raw value (needed by v1_20260329 challenger; CatBoost handles regime shifts)
        al = pd.Series(aligned.values, index=df_1m.index)
        cols[f"{prefix}_raw"] = al
        for n in [1, 5, 10, 20]:
            cols[f"{prefix}_chg_{n}"] = al.diff(n)

        # Z-score vs rolling mean
        for w in [20, 60]:
            rmean = al.rolling(w).mean()
            rstd = al.rolling(w).std()
            cols[f"{prefix}_zscore_{w}"] = (al - rmean) / (rstd + 1e-10)

        # Rolling percentile
        for w in [60, 120]:
            cols[f"{prefix}_pctile_{w}"] = al.rolling(w).apply(
                lambda x: np.sum(x[-1] >= x) / len(x) if len(x) > 0 else np.nan,
                raw=True,
            )

    # --- Load each data source ---
    _load_and_align("btcusdt_funding_rate.parquet", "fundingRate", "funding")
    _load_and_align("btcusdt_open_interest.parquet", "sumOpenInterest", "oi")
    _load_and_align("btcusdt_top_ls_account.parquet", "longShortRatio", "ls_account")
    _load_and_align("btcusdt_top_ls_position.parquet", "longShortRatio", "ls_position")
    _load_and_align("btcusdt_global_ls.parquet", "longShortRatio", "global_ls")
    _load_and_align("btcusdt_taker_volume.parquet", "buySellRatio", "taker_vol")

    # --- Hyperliquid funding rate (hourly, CEX-DEX signal) ---
    hl_funding = _load_hl_funding(df_1m)
    if hl_funding is not None:
        hl_al = pd.Series(hl_funding.values, index=df_1m.index)
        # HL funding rate derivatives (same pattern as Binance futures)
        for n in [1, 5, 10, 20]:
            cols[f"hl_funding_chg_{n}"] = hl_al.diff(n)
        for w in [20, 60]:
            rmean = hl_al.rolling(w).mean()
            rstd = hl_al.rolling(w).std()
            cols[f"hl_funding_zscore_{w}"] = (hl_al - rmean) / (rstd + 1e-10)
        for w in [60, 120]:
            cols[f"hl_funding_pctile_{w}"] = hl_al.rolling(w).apply(
                lambda x: np.sum(x[-1] >= x) / len(x) if len(x) > 0 else np.nan,
                raw=True,
            )

        # CEX-DEX funding spread: Binance - Hyperliquid
        # This is the key alpha signal — divergence between CEX and DEX funding
        # Load Binance funding raw for spread calculation
        bn_raw = None
        table_name = "futures_funding_rate"
        try:
            import db as _db
            bn_raw = _db.read_futures(table_name)
        except Exception:
            pass
        if bn_raw is None:
            fpath = data_dir / "btcusdt_funding_rate.parquet"
            try:
                bn_raw = pd.read_parquet(fpath)
            except (FileNotFoundError, OSError):
                pass

        if bn_raw is not None and not bn_raw.empty:
            # Align Binance funding to 1m index
            time_col = "datetime" if "datetime" in bn_raw.columns else "open_time"
            bn_src = bn_raw[[time_col, "fundingRate"]].copy()
            bn_src.columns = ["dt", "val"]
            bn_src["val"] = bn_src["val"].astype(float)
            bn_src["dt"] = pd.to_datetime(bn_src["dt"], utc=True, errors="coerce")
            bn_src = bn_src.dropna(subset=["dt"]).sort_values("dt").drop_duplicates(subset=["dt"])

            temp = df_1m[["open_time"]].copy()
            temp["open_time"] = pd.to_datetime(temp["open_time"], utc=True).astype("datetime64[us, UTC]")
            bn_src = bn_src.rename(columns={"dt": "open_time"})
            bn_src["open_time"] = bn_src["open_time"].astype("datetime64[us, UTC]")
            bn_merged = pd.merge_asof(
                temp.sort_values("open_time"),
                bn_src.sort_values("open_time"),
                on="open_time",
                direction="backward",
            )
            bn_aligned = pd.Series(bn_merged["val"].values, index=df_1m.index).ffill()

            # Spread = Binance funding - Hyperliquid funding
            # Positive spread = Binance more bullish (higher cost to go long on Binance)
            spread = bn_aligned - hl_al
            for n in [1, 5, 10, 20]:
                cols[f"hl_bn_spread_chg_{n}"] = spread.diff(n)
            for w in [20, 60]:
                rmean = spread.rolling(w).mean()
                rstd = spread.rolling(w).std()
                cols[f"hl_bn_spread_zscore_{w}"] = (spread - rmean) / (rstd + 1e-10)

    return pd.DataFrame(cols, index=df_1m.index)


# ---------------------------------------------------------------------------
# 9. ADX helper (inlined from regime_detector to avoid training dependency)
# ---------------------------------------------------------------------------


def _calc_adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    """Calculate ADX (Average Directional Index)."""
    n = len(c)
    if n < period + 1:
        return 0.0
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = h[i] - h[i - 1]
        down = l[i - 1] - l[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
    atr = np.zeros(n)
    plus_di_arr = np.zeros(n)
    minus_di_arr = np.zeros(n)
    atr[period] = np.mean(tr[1 : period + 1])
    plus_di_arr[period] = np.mean(plus_dm[1 : period + 1])
    minus_di_arr[period] = np.mean(minus_dm[1 : period + 1])
    for i in range(period + 1, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
        plus_di_arr[i] = (plus_di_arr[i - 1] * (period - 1) + plus_dm[i]) / period
        minus_di_arr[i] = (minus_di_arr[i - 1] * (period - 1) + minus_dm[i]) / period
    if atr[-1] > 0:
        plus_di = 100 * plus_di_arr[-1] / atr[-1]
        minus_di = 100 * minus_di_arr[-1] / atr[-1]
    else:
        return 0.0
    di_sum = plus_di + minus_di
    if di_sum > 0:
        return 100 * abs(plus_di - minus_di) / di_sum
    return 0.0


# ---------------------------------------------------------------------------
# 10. CVD (Cumulative Volume Delta)
# ---------------------------------------------------------------------------


def cvd_features(df_1m: pd.DataFrame) -> pd.DataFrame:
    """从 1min K线计算 CVD 系列特征.

    CVD = cumulative(taker_buy_volume - taker_sell_volume)
    比 taker_buy_ratio 更有信息量:
      - ratio 会被总量 normalization 稀释
      - CVD 保留了绝对买卖力量差异

    Features:
      - cvd: 原始 CVD
      - cvd_slope_{w}: CVD 的斜率 (买方力量加速/减速)
      - cvd_divergence_{w}: 价格 vs CVD 背离 (经典信号)
      - cvd_zscore_{w}: CVD 异常检测
      - cvd_momentum_{n}: CVD 动量
    """
    if "taker_buy_base" not in df_1m.columns:
        return pd.DataFrame(index=df_1m.index)

    v = df_1m["volume"]
    tbv = df_1m["taker_buy_base"]
    sell_vol = v - tbv
    delta = tbv - sell_vol  # 每根 K线的 volume delta
    cvd = delta.cumsum()

    cols: dict = {}
    cols["cvd_delta"] = delta

    # CVD 斜率 (= rolling sum of delta, start-point invariant)
    for w in [5, 10, 20, 30]:
        cols[f"cvd_slope_{w}"] = delta.rolling(w).sum() / (w + 1e-10)

    # CVD 动量 (= rolling sum of delta over n bars)
    for n in [3, 5, 10, 20]:
        cols[f"cvd_mom_{n}"] = delta.rolling(n).sum()

    # Delta Z-score (use per-bar delta, not cumulative cvd)
    for w in [20, 60]:
        rmean = delta.rolling(w).mean()
        rstd = delta.rolling(w).std()
        cols[f"cvd_zscore_{w}"] = (delta - rmean) / (rstd + 1e-10)

    # 价格-CVD 背离 (用 rolling delta sum 替代 cumsum diff)
    c = df_1m["close"]
    for w in [10, 20, 30]:
        price_change = c.pct_change(w)
        delta_sum = delta.rolling(w).sum()
        delta_std = delta.rolling(max(w, 5)).std()
        cvd_change = delta_sum / (delta_std * w + 1e-10)
        cols[f"cvd_divergence_{w}"] = price_change - cvd_change

    # Delta ratio: 当前 delta 相对于平均水平
    for w in [5, 10, 20]:
        avg_delta = delta.abs().rolling(w).mean()
        cols[f"delta_ratio_{w}"] = delta / (avg_delta + 1e-10)

    # Rolling delta sum ratio (替代 cvd_change_pct, 避免依赖 cumsum 起点)
    for w in [30, 60]:
        recent = delta.rolling(w).sum()
        older = delta.shift(w).rolling(w).sum()
        cols[f"cvd_change_pct_{w}"] = recent / (older.abs() + 1e-10)

    return pd.DataFrame(cols, index=df_1m.index)


# ---------------------------------------------------------------------------
# 11. ETH/BTC 跨品种信号
# ---------------------------------------------------------------------------

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def _fetch_binance_klines(
    symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 120
) -> pd.DataFrame | None:
    """Fetch klines from Binance for any symbol/interval."""
    for k in [
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ]:
        os.environ.pop(k, None)

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _BINANCE_KLINES_URL,
                params={"symbol": symbol, "interval": interval, "limit": str(limit)},
            )
            resp.raise_for_status()
            raw = resp.json()

        df = pd.DataFrame(
            raw,
            columns=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
            df[col] = df[col].astype(float)
        df["trades"] = df["trades"].astype(int)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.drop(columns=["ignore"])
        return df
    except Exception:
        return None


def fetch_eth_klines(limit: int = 120) -> pd.DataFrame | None:
    """Fetch ETHUSDT 1min klines from Binance."""
    return _fetch_binance_klines("ETHUSDT", "1m", limit)


def fetch_btc_30m(limit: int = 500) -> pd.DataFrame | None:
    """Fetch BTCUSDT 30min klines from Binance.

    500 bars ≈ 10.4 天，足够 behavioral 特征热身。
    """
    return _fetch_binance_klines("BTCUSDT", "30m", limit)


def fetch_btc_4h(limit: int = 500) -> pd.DataFrame | None:
    """Fetch BTCUSDT 4h klines from Binance.

    500 bars ≈ 83 天，足够 multi-timeframe 特征热身。
    """
    return _fetch_binance_klines("BTCUSDT", "4h", limit)


def cross_asset_features(
    btc_1m: pd.DataFrame, eth_1m: pd.DataFrame | None = None
) -> pd.DataFrame:
    """BTC-ETH 跨品种特征.

    Key insight: ETH 有时领先 BTC 1-5 分钟 (或反之)。
    相关性突变 = regime shift 信号。

    Features:
      - eth_btc_ret_corr_{w}: 滚动收益率相关性
      - eth_btc_ret_diff_{n}: ETH vs BTC 收益率差
      - eth_lead_{n}: ETH 领先 BTC 的收益率 (lag)
      - eth_btc_corr_change_{w}: 相关性变化速度
    """
    if eth_1m is None or len(eth_1m) < 30:
        return pd.DataFrame(index=btc_1m.index)

    # Align by open_time
    btc = btc_1m.set_index("open_time")["close"]
    eth = eth_1m.set_index("open_time")["close"]

    # Inner join on timestamp
    aligned = pd.DataFrame({"btc": btc, "eth": eth}).dropna()
    if len(aligned) < 30:
        return pd.DataFrame(index=btc_1m.index)

    btc_ret = aligned["btc"].pct_change()
    eth_ret = aligned["eth"].pct_change()

    # Reindex back to original btc_1m index (keep tz-aware Series, not .values)
    btc_ot = btc_1m["open_time"]

    cols: dict = {}

    # Rolling correlation
    for w in [10, 20, 30, 60]:
        corr = btc_ret.rolling(w).corr(eth_ret)
        cols[f"eth_btc_corr_{w}"] = corr.reindex(btc_ot).values

    # Correlation change (spike = regime shift)
    for w in [20, 60]:
        corr = btc_ret.rolling(w).corr(eth_ret)
        cols[f"eth_btc_corr_chg_{w}"] = corr.diff(5).reindex(btc_ot).values

    # Return difference (ETH outperformance)
    for n in [1, 3, 5, 10]:
        diff = eth_ret.rolling(n).sum() - btc_ret.rolling(n).sum()
        cols[f"eth_btc_ret_diff_{n}"] = diff.reindex(btc_ot).values

    # ETH leading signal: does ETH return at t predict BTC at t+1?
    for lag in [1, 2, 3]:
        lead_signal = eth_ret.shift(lag)
        cols[f"eth_lead_{lag}"] = lead_signal.reindex(btc_ot).values

    # ETH/BTC ratio momentum
    ratio = aligned["eth"] / aligned["btc"]
    for n in [5, 10, 20]:
        cols[f"eth_btc_ratio_mom_{n}"] = (
            ratio.pct_change(n).reindex(btc_ot).values
        )

    return pd.DataFrame(cols, index=btc_1m.index)


# ---------------------------------------------------------------------------
# 12. Polymarket 赔率动态
# ---------------------------------------------------------------------------


def market_dynamics_features(market_prices: pd.Series | None = None) -> pd.DataFrame:
    """Polymarket 赔率变化速度和加速度.

    在实盘中，每次预测时传入最近的 market_price 历史。
    训练时不可用（Polymarket 历史数据有限），返回空。

    Features:
      - mkt_price_momentum: 赔率变化速度
      - mkt_price_accel: 赔率加速度
      - mkt_price_zscore: 赔率异常偏移
    """
    if market_prices is None or len(market_prices) < 5:
        return pd.DataFrame()

    cols: dict = {}
    cols["mkt_momentum"] = market_prices.diff()
    cols["mkt_accel"] = market_prices.diff().diff()
    cols["mkt_zscore"] = (market_prices - market_prices.rolling(10).mean()) / (
        market_prices.rolling(10).std() + 1e-10
    )
    return pd.DataFrame(cols, index=market_prices.index)


# ---------------------------------------------------------------------------
# 13. Regime 指标 — 直接作为模型输入特征
# ---------------------------------------------------------------------------


def regime_indicator_features(df_1m: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    """将 regime 检测的核心指标作为连续特征直接输入模型.

    不再通过分类路由，而是让 LightGBM 自行学习这些指标的非线性组合。

    Features:
      - rgm_adx: ADX 趋势强度 (0-100)
      - rgm_trend_strength: 趋势方向 (-1 to +1)
      - rgm_vol_ratio: 短期/长期波动率比 (vol_5/vol_30)
      - rgm_autocorr: 收益率自相关 (正=趋势, 负=均值回归)
      - rgm_vol_level: 预期 5min 波动幅度
      - rgm_vol_accel: 波动率加速度 (vol_ratio 的变化)
    """
    c = df_1m["close"].values.astype(float)
    h = df_1m["high"].values.astype(float)
    low = df_1m["low"].values.astype(float)

    if len(c) < lookback:
        return pd.DataFrame(index=df_1m.index)

    cols: dict = {}

    # Log returns
    log_c = np.log(pd.Series(c, index=df_1m.index))
    returns = log_c.diff()

    # --- ADX (rolling) ---

    # Rolling ADX: compute for trailing windows
    adx_vals = np.full(len(c), np.nan)
    for i in range(lookback, len(c)):
        adx_vals[i] = _calc_adx(
            h[i - lookback : i], low[i - lookback : i], c[i - lookback : i], period=14
        )
    cols["rgm_adx"] = adx_vals

    # --- Trend strength: normalized slope of last 30 bars ---
    close_s = pd.Series(c, index=df_1m.index)
    for w in [15, 30, 60]:

        def _trend_strength(x):
            if len(x) < 5 or np.std(x) == 0:
                return 0.0
            slope = np.polyfit(np.arange(len(x)), x, 1)[0]
            return np.clip(slope / (np.std(x) + 1e-10), -1, 1)

        cols[f"rgm_trend_{w}"] = close_s.rolling(w).apply(_trend_strength, raw=True)

    # --- Volatility ratio (short/long, regime acceleration signal) ---
    for short, long in [(5, 30), (5, 60), (10, 60)]:
        vol_s = returns.rolling(short).std()
        vol_l = returns.rolling(long).std()
        cols[f"rgm_vol_ratio_{short}_{long}"] = vol_s / (vol_l + 1e-10)

    # --- Expected 5-min move (absolute volatility level) ---
    for w in [5, 15, 30]:
        vol_w = returns.rolling(w).std()
        cols[f"rgm_exp5m_{w}"] = vol_w * np.sqrt(5)

    # --- Return autocorrelation (trending vs mean-reverting) ---
    for w in [20, 60]:

        def _autocorr(x):
            if len(x) < 5:
                return 0.0
            r = np.corrcoef(x[:-1], x[1:])[0, 1]
            return 0.0 if np.isnan(r) else r

        cols[f"rgm_autocorr_{w}"] = returns.rolling(w).apply(_autocorr, raw=True)

    # --- Volatility acceleration (vol_ratio 的变化速度) ---
    vol_5 = returns.rolling(5).std()
    vol_30 = returns.rolling(30).std()
    vol_ratio = vol_5 / (vol_30 + 1e-10)
    for n in [3, 5, 10]:
        cols[f"rgm_vol_accel_{n}"] = vol_ratio.diff(n)

    return pd.DataFrame(cols, index=df_1m.index)


def behavioral_state_features(
    df_1m: pd.DataFrame, bars_30m: pd.DataFrame | None = None
) -> pd.DataFrame:
    """基于 30min K 线的参与者行为状态特征.

    直接使用 Binance 30min K 线（服务端精确聚合）。
    训练和推理统一从 DuckDB 读取或 Binance API 直拉，不做 resample。

    5 个行为维度:
      1. 急迫度不对称 — 谁更急？买方脉冲 vs 卖方脉冲
      2. 参与者结构 — 大单少笔(机构) vs 小单多笔(散户)
      3. 流动性变化 — 价格对流量的敏感度在恶化还是改善
      4. 行为持续性 — 方向性力量是持续的还是脉冲式的
      5. 力量衰竭 — 趋势还在但推动力在减弱

    计算完成后对齐回 1min index，最终在 build_features 中按 5min 窗口采样。

    Args:
        df_1m: 1min K 线 (用于 index 对齐)
        bars_30m: 30min K 线 (来自 DuckDB 或 Binance API)。
    """
    if bars_30m is None or len(bars_30m) < 4:
        return pd.DataFrame(index=df_1m.index)

    bars = bars_30m.copy()

    if len(bars) < 4:
        return pd.DataFrame(index=df_1m.index)

    c = bars["close"].astype(float)
    v = bars["volume"].astype(float)
    ret = c.pct_change()

    has_taker = "taker_buy_base" in bars.columns
    trades_col = (
        "trades"
        if "trades" in bars.columns
        else ("number_of_trades" if "number_of_trades" in bars.columns else None)
    )

    if has_taker:
        tbv = bars["taker_buy_base"].astype(float)
        tsv = v - tbv
        delta = tbv - tsv  # 每 30min 的净买卖力量

    bf_cols: dict = {}  # 30min 级别特征

    # =================================================================
    # 维度 1: 买卖急迫度不对称
    # =================================================================
    if has_taker:
        bf_cols["bh_buy_ratio"] = tbv / (v + 1e-10)

        # 买卖力量偏度: 正偏=偶尔大买(吸筹)，负偏=偶尔大卖(恐慌)
        for w in [3, 6, 12]:  # 1.5h, 3h, 6h
            bf_cols[f"bh_delta_skew_{w}"] = delta.rolling(w).skew()

        # 买卖力量不对称的变化速度
        ratio = tbv / (v + 1e-10)
        for w in [2, 4, 8]:
            bf_cols[f"bh_ratio_chg_{w}"] = ratio.diff(w)

    # =================================================================
    # 维度 2: 参与者结构
    # =================================================================
    if trades_col:
        trades = bars[trades_col].astype(float)
        avg_size = v / (trades + 1e-10)

        # 散户 vs 机构: 笔数变化 - 单笔金额变化
        for w in [3, 6]:
            trades_chg = trades.pct_change(w)
            size_chg = avg_size.pct_change(w)
            bf_cols[f"bh_crowd_{w}"] = trades_chg - size_chg

        # 单笔金额 z-score (大单检测)
        for w in [6, 12]:
            avg_mean = avg_size.rolling(w).mean()
            avg_std = avg_size.rolling(w).std()
            bf_cols[f"bh_bigorder_z_{w}"] = (avg_size - avg_mean) / (avg_std + 1e-10)

        # 参与度: 笔数相对历史
        for w in [4, 8]:
            trades_ma = trades.rolling(w).mean()
            bf_cols[f"bh_participation_{w}"] = trades / (trades_ma + 1e-10)

    # =================================================================
    # 维度 3: 流动性 / 市场冲击变化
    # =================================================================
    abs_ret = ret.abs()
    kyle = abs_ret / (v + 1e-10)

    for s_w, l_w in [(2, 6), (3, 12)]:
        kl_s = kyle.rolling(s_w).mean()
        kl_l = kyle.rolling(l_w).mean()
        bf_cols[f"bh_liq_shift_{s_w}_{l_w}"] = kl_s / (kl_l + 1e-10)

    # 量价效率: 量大但价不动=分歧
    for w in [4, 8]:
        vol_z = (v - v.rolling(w).mean()) / (v.rolling(w).std() + 1e-10)
        ret_z = (abs_ret - abs_ret.rolling(w).mean()) / (
            abs_ret.rolling(w).std() + 1e-10
        )
        bf_cols[f"bh_vol_eff_{w}"] = vol_z * ret_z

    # 涨跌冲击不对称
    if has_taker:
        for w in [4, 8]:
            up = (ret > 0).astype(float)
            dn = (ret < 0).astype(float)
            up_impact = (abs_ret * up).rolling(w).sum() / (
                tbv.where(ret > 0, np.nan).rolling(w, min_periods=1).sum() + 1e-10
            )
            dn_impact = (abs_ret * dn).rolling(w).sum() / (
                tsv.where(ret < 0, np.nan).rolling(w, min_periods=1).sum() + 1e-10
            )
            bf_cols[f"bh_impact_asym_{w}"] = up_impact - dn_impact

    # =================================================================
    # 维度 4: 行为持续性 vs 脉冲性
    # =================================================================
    if has_taker:
        for w in [4, 8]:

            def _autocorr(x):
                if len(x) < 3:
                    return 0.0
                r = np.corrcoef(x[:-1], x[1:])[0, 1]
                return 0.0 if np.isnan(r) else r

            bf_cols[f"bh_persist_{w}"] = delta.rolling(w).apply(_autocorr, raw=True)

        for w in [4, 8]:

            def _streak(x):
                if len(x) < 2:
                    return 0.5
                s = np.sign(x)
                return np.mean(s[1:] == s[:-1])

            bf_cols[f"bh_streak_{w}"] = delta.rolling(w).apply(_streak, raw=True)

        # 方差比: >1=趋势, <1=均值回归
        cvd = delta.cumsum()
        for s_w, l_w in [(2, 8), (3, 12)]:
            var_s = cvd.diff(s_w).rolling(l_w).var()
            var_l = cvd.diff(l_w).rolling(l_w).var()
            bf_cols[f"bh_vr_{s_w}_{l_w}"] = (var_s * l_w) / (var_l * s_w + 1e-10)

    # =================================================================
    # 维度 5: 力量衰竭
    # =================================================================
    for w in [3, 6]:
        price_mom = c.pct_change(w)
        vol_mom = v.pct_change(w)
        bf_cols[f"bh_pv_agree_{w}"] = np.sign(price_mom) * vol_mom

    if has_taker:
        cvd = delta.cumsum()
        for w in [3, 6]:
            price_roc = c.pct_change(w)
            cvd_roc = cvd.diff(w) / (cvd.rolling(max(w, 3)).std() + 1e-10)
            bf_cols[f"bh_cvd_confirm_{w}"] = price_roc * cvd_roc

        for w in [4, 6]:
            half = w // 2
            first = delta.rolling(half).mean().shift(half)
            second = delta.rolling(half).mean()
            bf_cols[f"bh_fade_{w}"] = second - first

    bf_cols["bh_vol_accel"] = v.pct_change().diff()

    # =================================================================
    # 对齐回 1min index (shift by 30m to prevent lookahead bias)
    # =================================================================
    bf = pd.DataFrame(bf_cols, index=bars.index)
    bf["open_time"] = bars["open_time"] + pd.Timedelta(minutes=30)
    df_ts = df_1m[["open_time"]].copy()
    merged = pd.merge_asof(
        df_ts.sort_values("open_time"),
        bf.sort_values("open_time"),
        on="open_time",
        direction="backward",
    )
    cols: dict = {}
    for col in bf.columns:
        if col == "open_time":
            continue
        cols[col] = merged[col].values

    return pd.DataFrame(cols, index=df_1m.index)


# ---------------------------------------------------------------------------
# 14. Multi-Timeframe Features
# ---------------------------------------------------------------------------


def _mtf_bar_features(bars: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Compute a standard set of features on OHLCV bars with the given prefix.

    Works for any timeframe. Expects columns: open, high, low, close, volume.
    Optional: taker_buy_base.

    Returns a DataFrame indexed same as *bars* with ~80-100 feature columns.
    """
    cols: dict = {}

    c = bars["close"].astype(float)
    h = bars["high"].astype(float)
    lo = bars["low"].astype(float)
    o = bars["open"].astype(float)
    v = bars["volume"].astype(float)

    log_ret = np.log(c / c.shift(1))
    ret = c.pct_change()

    has_taker = "taker_buy_base" in bars.columns
    if has_taker:
        tbv = bars["taker_buy_base"].astype(float)
        tsv = v - tbv
        delta = tbv - tsv
        cvd = delta.cumsum()

    # =================================================================
    # A. Volatility features
    # =================================================================
    for w in [3, 6, 12]:
        cols[f"{prefix}vol_{w}"] = ret.rolling(w).std()

    for s_w, l_w in [(2, 6), (3, 12)]:
        vol_s = ret.rolling(s_w).std()
        vol_l = ret.rolling(l_w).std()
        cols[f"{prefix}vol_ratio_{s_w}_{l_w}"] = vol_s / (vol_l + 1e-10)

    # Parkinson volatility: sqrt( (1/4ln2) * mean((ln(H/L))^2) )
    hl_log = np.log(h / (lo + 1e-10))
    for w in [3, 6, 12]:
        cols[f"{prefix}parkinson_{w}"] = np.sqrt(
            hl_log.pow(2).rolling(w).mean() / (4 * np.log(2))
        )

    # Realized volatility: sqrt(sum(log_ret^2))
    log_ret_sq = log_ret**2
    for w in [3, 6, 12]:
        cols[f"{prefix}realized_vol_{w}"] = np.sqrt(log_ret_sq.rolling(w).sum())

    # Return skewness and kurtosis
    for w in [6, 12]:
        cols[f"{prefix}skew_{w}"] = ret.rolling(w).skew()
        cols[f"{prefix}kurt_{w}"] = ret.rolling(w).kurt()

    # Garman-Klass volatility:
    # GK = 0.5*(ln(H/L))^2 - (2ln2-1)*(ln(C/O))^2
    co_log = np.log(c / (o + 1e-10))
    gk_single = 0.5 * hl_log.pow(2) - (2 * np.log(2) - 1) * co_log.pow(2)
    for w in [3, 6, 12]:
        cols[f"{prefix}garman_klass_{w}"] = np.sqrt(
            gk_single.rolling(w).mean().clip(lower=0)
        )

    # =================================================================
    # B. Volume / flow features
    # =================================================================
    for w in [3, 6, 12]:
        v_mean = v.rolling(w).mean()
        cols[f"{prefix}vratio_{w}"] = v / (v_mean + 1e-10)

    if has_taker:
        for w in [3, 6, 12]:
            cols[f"{prefix}buy_ratio_{w}"] = tbv.rolling(w).sum() / (
                v.rolling(w).sum() + 1e-10
            )

        # Flow imbalance: rolling mean of (buy - sell) / total
        flow_imb = (tbv - tsv) / (v + 1e-10)
        for w in [3, 6, 12]:
            cols[f"{prefix}flow_imb_{w}"] = flow_imb.rolling(w).mean()

    # Volume z-score
    for w in [6, 12]:
        v_mean = v.rolling(w).mean()
        v_std = v.rolling(w).std()
        cols[f"{prefix}vol_zscore_{w}"] = (v - v_mean) / (v_std + 1e-10)

    # VPT (Volume-Price Trend) slope
    vpt = (ret * v).cumsum()
    for w in [3, 6]:
        cols[f"{prefix}vpt_slope_{w}"] = vpt.diff(w) / (w + 1e-10)

    # =================================================================
    # C. CVD features (conditional on taker data)
    # =================================================================
    if has_taker:
        for w in [3, 6, 12]:
            cols[f"{prefix}cvd_slope_{w}"] = cvd.diff(w) / (w + 1e-10)

        for w in [6, 12]:
            cvd_mean = cvd.rolling(w).mean()
            cvd_std = cvd.rolling(w).std()
            cols[f"{prefix}cvd_zscore_{w}"] = (cvd - cvd_mean) / (cvd_std + 1e-10)

        # Price vs CVD divergence
        for w in [3, 6]:
            p_chg = c.pct_change(w)
            cvd_chg = cvd.diff(w) / (cvd.rolling(max(w, 3)).std() + 1e-10)
            cols[f"{prefix}cvd_divergence_{w}"] = p_chg - cvd_chg

        # Delta ratio: current delta vs average
        for w in [3, 6]:
            avg_delta = delta.abs().rolling(w).mean()
            cols[f"{prefix}delta_ratio_{w}"] = delta / (avg_delta + 1e-10)

    # =================================================================
    # D. Momentum / trend features
    # =================================================================
    for n in [1, 2, 3, 6, 12]:
        cols[f"{prefix}ret_{n}"] = c.pct_change(n)

    for w in [6, 12]:
        ret_mean = ret.rolling(w).mean()
        ret_std = ret.rolling(w).std()
        cols[f"{prefix}ret_zscore_{w}"] = (ret - ret_mean) / (ret_std + 1e-10)

    # EMA distance
    for f_span, s_span in [(3, 6), (6, 12)]:
        ema_f = c.ewm(span=f_span, adjust=False).mean()
        ema_s = c.ewm(span=s_span, adjust=False).mean()
        cols[f"{prefix}ema_dist_{f_span}_{s_span}"] = (ema_f - ema_s) / (ema_s + 1e-10)

    # Drawdown from rolling max
    for w in [6, 12]:
        roll_max = c.rolling(w).max()
        cols[f"{prefix}drawdown_{w}"] = (c - roll_max) / (roll_max + 1e-10)

    # Trend strength: normalized slope
    for w in [3, 6, 12]:

        def _trend(x):
            if len(x) < 3 or np.std(x) == 0:
                return 0.0
            slope = np.polyfit(np.arange(len(x)), x, 1)[0]
            return np.clip(slope / (np.std(x) + 1e-10), -3, 3)

        cols[f"{prefix}trend_strength_{w}"] = c.rolling(w).apply(_trend, raw=True)

    # =================================================================
    # E. Microstructure features
    # =================================================================
    abs_ret = ret.abs()

    # Kyle's lambda: |ret| / volume
    kyle = abs_ret / (v + 1e-10)
    for w in [3, 6]:
        cols[f"{prefix}kyle_lambda_{w}"] = kyle.rolling(w).mean()

    # Amihud illiquidity: |ret| / dollar_volume
    dollar_vol = v * c
    amihud = abs_ret / (dollar_vol + 1e-10)
    for w in [3, 6]:
        cols[f"{prefix}amihud_{w}"] = amihud.rolling(w).mean()

    # Spread proxy: 2*(H-L)/(H+L)
    spread_proxy = 2 * (h - lo) / (h + lo + 1e-10)
    for w in [3, 6]:
        cols[f"{prefix}spread_proxy_{w}"] = spread_proxy.rolling(w).mean()

    return pd.DataFrame(cols, index=bars.index)


def multi_timeframe_features(
    df_1m: pd.DataFrame,
    bars_30m: pd.DataFrame | None = None,
    bars_4h: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Generate features across multiple timeframes.

    Applies a common set of feature computations to 30min and 4h bars,
    then aligns back to the 1min index via merge_asof (backward fill).

    1min features are already computed in features.py, so we only add
    30min and 4h here.
    """
    # Map prefix to bar duration for lookahead prevention
    _BAR_DURATION = {
        "mtf30_": pd.Timedelta(minutes=30),
        "mtf4h_": pd.Timedelta(hours=4),
    }

    tf_configs = [
        ("mtf30_", bars_30m),
        ("mtf4h_", bars_4h),
    ]

    aligned_parts: list[pd.DataFrame] = []

    for prefix, bars in tf_configs:
        if bars is None or len(bars) < 4:
            continue

        bars = bars.copy()

        # Compute features on this timeframe's bars
        bf = _mtf_bar_features(bars, prefix)

        # Fill NaN from incomplete rolling windows with 0
        bf = bf.fillna(0)

        # Prevent lookahead bias: shift open_time forward by one bar duration.
        # A 30m bar with open_time=10:00 covers 10:00–10:29:59 and completes
        # at 10:30. By setting its merge key to 10:30, merge_asof(backward)
        # will only match it to 1m candles at 10:30 or later — i.e., after
        # the bar has fully closed.
        bar_duration = _BAR_DURATION[prefix]
        bf["open_time"] = bars["open_time"] + bar_duration
        df_ts = df_1m[["open_time"]].copy()

        merged = pd.merge_asof(
            df_ts.sort_values("open_time"),
            bf.sort_values("open_time"),
            on="open_time",
            direction="backward",
        )

        # Drop open_time, reindex to match df_1m
        merged = merged.drop(columns=["open_time"])
        merged.index = df_1m.index
        aligned_parts.append(merged)

    if aligned_parts:
        result = pd.concat(aligned_parts, axis=1)
        result.index = df_1m.index
        return result

    return pd.DataFrame(index=df_1m.index)


# ---------------------------------------------------------------------------
# 15. Coinbase Premium — US institutional flow signal
# ---------------------------------------------------------------------------


def coinbase_premium_features(
    btc_1m: pd.DataFrame, cb_1m: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Coinbase BTC-USD vs Binance BTC-USDT premium features.

    The Coinbase premium reflects US institutional buying pressure.
    When Coinbase price > Binance price, US institutions are net buyers.

    Features:
      - cb_premium_bps: raw premium in basis points
      - cb_premium_ma_{w}: smoothed premium (noise reduction)
      - cb_premium_zscore_{w}: premium anomaly detection
      - cb_premium_mom_{n}: premium momentum (acceleration of institutional flow)
      - cb_premium_regime: premium sign persistence (sustained buying vs selling)
      - cb_premium_vol_{w}: premium volatility (uncertainty in institutional flow)
    """
    if cb_1m is None or len(cb_1m) < 30:
        return pd.DataFrame(index=btc_1m.index)

    # Merge Coinbase close onto BTC 1m by open_time
    merged = pd.merge(
        btc_1m[["open_time", "close"]].rename(columns={"close": "binance"}),
        cb_1m[["open_time", "close"]].rename(columns={"close": "coinbase"}),
        on="open_time",
        how="inner",
    )
    if len(merged) < 30:
        return pd.DataFrame(index=btc_1m.index)

    # Raw premium in basis points
    premium = (merged["coinbase"] - merged["binance"]) / merged["binance"] * 10_000
    premium.index = merged.index

    # Build a mapping: merged row → original btc_1m row
    # Use open_time to map back
    ot_to_idx = {ot: i for i, ot in zip(btc_1m.index, btc_1m["open_time"])}
    target_idx = [ot_to_idx[ot] for ot in merged["open_time"] if ot in ot_to_idx]

    cols: dict = {}

    def _assign(series: pd.Series, name: str) -> None:
        """Assign computed series back to cols dict aligned by original index."""
        vals = pd.Series(np.nan, index=btc_1m.index)
        vals.iloc[target_idx[: len(series)]] = series.values[: len(target_idx)]
        cols[name] = vals.values

    # Raw premium
    _assign(premium, "cb_premium_bps")

    # Smoothed premium (reduce noise)
    for w in [5, 10, 20, 60]:
        _assign(premium.rolling(w).mean(), f"cb_premium_ma_{w}")

    # Premium Z-score (anomaly detection)
    for w in [20, 60]:
        p_mean = premium.rolling(w).mean()
        p_std = premium.rolling(w).std()
        zscore = (premium - p_mean) / (p_std + 1e-10)
        _assign(zscore, f"cb_premium_zscore_{w}")

    # Premium momentum (change over n bars)
    for n in [3, 5, 10, 20]:
        _assign(premium.diff(n), f"cb_premium_mom_{n}")

    # Premium sign persistence: fraction of positive premium in rolling window
    for w in [10, 30, 60]:
        sign_pct = (premium > 0).astype(float).rolling(w).mean()
        _assign(sign_pct, f"cb_premium_regime_{w}")

    # Premium volatility (instability of institutional flow)
    for w in [10, 30]:
        _assign(premium.rolling(w).std(), f"cb_premium_vol_{w}")

    # Premium-price divergence: premium rising but BTC price falling (or vice versa)
    btc_ret = merged["binance"].pct_change()
    for w in [5, 10, 20]:
        prem_chg = premium.rolling(w).mean().diff(w)
        price_chg = btc_ret.rolling(w).sum()
        prem_std = premium.rolling(max(w, 5)).std()
        divergence = prem_chg / (prem_std + 1e-10) - price_chg * 100
        _assign(divergence, f"cb_premium_divergence_{w}")

    return pd.DataFrame(cols, index=btc_1m.index)


def hyperliquid_premium_features(btc_1m: pd.DataFrame) -> pd.DataFrame:
    """Hyperliquid BTC-PERP vs Binance BTC-USDT premium features.

    Uses 5m candles (HL 1m API only has ~3d history; 5m has ~17d).
    The HL 5m close is aligned to 1m via merge_asof + forward-fill,
    so each 1m bar gets the latest completed HL 5m close.

    Features:
      - hl_premium_bps: raw premium in basis points
      - hl_premium_zscore_{w}: premium anomaly detection
      - hl_premium_mom_{n}: premium momentum
      - hl_premium_vol_{w}: premium volatility
      - hl_premium_divergence_{w}: premium-price divergence
      - hl_vol_ratio_{w}: HL/Binance volume ratio (5m aligned)
    """
    from pathlib import Path

    # Load HL 5m klines
    hl_5m = None
    try:
        import db as _db
        hl_5m = _db.read_hl_klines()
        if hl_5m is not None and len(hl_5m) < 30:
            hl_5m = None
    except Exception:
        pass

    if hl_5m is None:
        fpath = Path(__file__).parent / "raw" / "hl_btc_klines_5m.parquet"
        try:
            hl_5m = pd.read_parquet(fpath)
        except (FileNotFoundError, OSError):
            return pd.DataFrame(index=btc_1m.index)

    if hl_5m is None or len(hl_5m) < 30:
        return pd.DataFrame(index=btc_1m.index)

    # Align HL 5m close to 1m index via merge_asof (backward fill)
    # HL 5m bar at :00 covers :00-:04; its close is known at :05.
    # Shift open_time by 5m so it's only used after the bar closes.
    hl_src = hl_5m[["open_time", "close", "volume"]].copy()
    hl_src["open_time"] = hl_src["open_time"] + pd.Timedelta(minutes=5)
    hl_src = hl_src.rename(columns={"close": "hl_close", "volume": "hl_volume"})

    temp = btc_1m[["open_time", "close", "volume"]].copy()
    temp = temp.rename(columns={"close": "bn_close", "volume": "bn_volume"})

    # Normalize timestamps
    temp["open_time"] = pd.to_datetime(temp["open_time"], utc=True).astype("datetime64[us, UTC]")
    hl_src["open_time"] = pd.to_datetime(hl_src["open_time"], utc=True).astype("datetime64[us, UTC]")

    merged = pd.merge_asof(
        temp.sort_values("open_time"),
        hl_src.sort_values("open_time"),
        on="open_time",
        direction="backward",
    )

    # Premium in basis points
    premium = ((merged["hl_close"] - merged["bn_close"]) / merged["bn_close"] * 10_000)
    premium = pd.Series(premium.values, index=btc_1m.index)

    cols: dict = {}

    # Raw premium
    cols["hl_premium_bps"] = premium.values

    # Smoothed premium
    for w in [5, 10, 20, 60]:
        cols[f"hl_premium_ma_{w}"] = premium.rolling(w).mean().values

    # Premium Z-score
    for w in [20, 60]:
        p_mean = premium.rolling(w).mean()
        p_std = premium.rolling(w).std()
        cols[f"hl_premium_zscore_{w}"] = ((premium - p_mean) / (p_std + 1e-10)).values

    # Premium momentum
    for n in [3, 5, 10, 20]:
        cols[f"hl_premium_mom_{n}"] = premium.diff(n).values

    # Premium volatility
    for w in [10, 30]:
        cols[f"hl_premium_vol_{w}"] = premium.rolling(w).std().values

    # Premium-price divergence
    btc_ret = pd.Series(merged["bn_close"].pct_change().values, index=btc_1m.index)
    for w in [5, 10, 20]:
        prem_chg = premium.rolling(w).mean().diff(w)
        price_chg = btc_ret.rolling(w).sum()
        prem_std = premium.rolling(max(w, 5)).std()
        cols[f"hl_premium_divergence_{w}"] = (prem_chg / (prem_std + 1e-10) - price_chg * 100).values

    # HL volume ratio: HL 5m volume / Binance 5m volume (DEX activity level)
    vol_ratio = pd.Series(
        (merged["hl_volume"].values / (merged["bn_volume"].values + 1e-10)),
        index=btc_1m.index,
    )
    for w in [5, 20, 60]:
        cols[f"hl_vol_ratio_{w}"] = vol_ratio.rolling(w).mean().values
    for w in [20, 60]:
        vr_mean = vol_ratio.rolling(w).mean()
        vr_std = vol_ratio.rolling(w).std()
        cols[f"hl_vol_ratio_zscore_{w}"] = ((vol_ratio - vr_mean) / (vr_std + 1e-10)).values

    return pd.DataFrame(cols, index=btc_1m.index)


# ---------------------------------------------------------------------------
# 15. Orderbook microstructure features (10s snapshots → 1m aggregation)
# ---------------------------------------------------------------------------


def _aggregate_snapshots_to_1m(
    snap_df: pd.DataFrame,
    time_col: str,
    value_cols: list[str],
) -> pd.DataFrame:
    """Aggregate 10-second snapshot data to 1-minute bars.

    For each minute, compute last/mean/std of each value column.
    Returns DataFrame indexed by minute (UTC, floor to minute).
    """
    df = snap_df.copy()
    df[time_col] = pd.to_datetime(df[time_col], utc=True)
    df["minute"] = df[time_col].dt.floor("1min")

    agg_dict: dict = {}
    for col in value_cols:
        if col in df.columns:
            agg_dict[f"{col}_last"] = (col, "last")
            agg_dict[f"{col}_mean"] = (col, "mean")
            agg_dict[f"{col}_std"] = (col, "std")

    if not agg_dict:
        return pd.DataFrame()

    result = df.groupby("minute").agg(**agg_dict)
    result.index.name = "open_time"
    return result


def _align_1m_to_klines(
    agg_1m: pd.DataFrame,
    df_1m: pd.DataFrame,
    prefix: str,
) -> dict[str, np.ndarray]:
    """Align 1m-aggregated snapshot data to kline index via merge_asof."""
    if agg_1m.empty:
        return {}

    agg = agg_1m.reset_index()
    agg["open_time"] = agg["open_time"].astype("datetime64[us, UTC]")

    temp = df_1m[["open_time"]].copy()
    temp["open_time"] = pd.to_datetime(temp["open_time"], utc=True).astype(
        "datetime64[us, UTC]"
    )

    merged = pd.merge_asof(
        temp.sort_values("open_time"),
        agg.sort_values("open_time"),
        on="open_time",
        direction="backward",
    )

    cols: dict[str, np.ndarray] = {}
    for c in agg.columns:
        if c == "open_time":
            continue
        key = f"{prefix}_{c}"
        cols[key] = merged[c].values

    return cols


def orderbook_microstructure_features(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Orderbook-derived microstructure features from 10s snapshots.

    Sources:
      - orderbook_snapshots (Binance): imbalance, spread, depth, OFI proxy
      - hl_orderbook (Hyperliquid): imbalance, spread
      - hl_asset_ctx (Hyperliquid): impact spread, OI dynamics

    All 10s data is aggregated to 1m, then rolling features are computed.
    Features use 'ob_' prefix (short coverage, registered in FUTURES_PREFIXES).
    """
    cols: dict = {}

    # ------------------------------------------------------------------
    # 1. Binance orderbook snapshots
    # ------------------------------------------------------------------
    bn_ob = None
    try:
        import db as _db

        bn_ob = _db.read_orderbook_snapshots()
    except Exception:
        pass

    if bn_ob is not None and len(bn_ob) >= 60:
        bn_value_cols = [
            "imbalance_5", "imbalance_10", "imbalance_20",
            "spread_bps", "mid_price",
            "bid_depth_5", "ask_depth_5", "bid_depth_10", "ask_depth_10",
        ]
        bn_agg = _aggregate_snapshots_to_1m(bn_ob, "snapshot_time", bn_value_cols)
        bn_aligned = _align_1m_to_klines(bn_agg, df_1m, "ob_bn")

        if bn_aligned:
            # --- Imbalance features ---
            for level in [5, 10, 20]:
                key_last = f"ob_bn_imbalance_{level}_last"
                if key_last not in bn_aligned:
                    continue
                imb = pd.Series(bn_aligned[key_last], index=df_1m.index)

                # Raw (centered: 0.5 = balanced)
                cols[f"ob_imb{level}"] = (imb - 0.5).values

                # Change
                for n in [1, 3, 5, 10]:
                    cols[f"ob_imb{level}_chg_{n}"] = imb.diff(n).values

                # Z-score
                for w in [20, 60]:
                    m = imb.rolling(w).mean()
                    s = imb.rolling(w).std()
                    cols[f"ob_imb{level}_zscore_{w}"] = ((imb - m) / (s + 1e-10)).values

                # Percentile
                for w in [60, 120]:
                    cols[f"ob_imb{level}_pctile_{w}"] = imb.rolling(w).apply(
                        lambda x: np.sum(x[-1] >= x) / len(x) if len(x) > 0 else np.nan,
                        raw=True,
                    ).values

                # Intra-minute stability (std of 10s snapshots within each minute)
                key_std = f"ob_bn_imbalance_{level}_std"
                if key_std in bn_aligned:
                    cols[f"ob_imb{level}_intra_std"] = bn_aligned[key_std]

            # --- Spread features ---
            spread_key = "ob_bn_spread_bps_last"
            if spread_key in bn_aligned:
                spread = pd.Series(bn_aligned[spread_key], index=df_1m.index)

                cols["ob_spread"] = spread.values

                for n in [1, 3, 5, 10]:
                    cols[f"ob_spread_chg_{n}"] = spread.diff(n).values

                for w in [20, 60]:
                    m = spread.rolling(w).mean()
                    s = spread.rolling(w).std()
                    cols[f"ob_spread_zscore_{w}"] = ((spread - m) / (s + 1e-10)).values

                for w in [60, 120]:
                    cols[f"ob_spread_pctile_{w}"] = spread.rolling(w).apply(
                        lambda x: np.sum(x[-1] >= x) / len(x) if len(x) > 0 else np.nan,
                        raw=True,
                    ).values

                # Intra-minute spread volatility
                spread_std_key = "ob_bn_spread_bps_std"
                if spread_std_key in bn_aligned:
                    cols["ob_spread_intra_std"] = bn_aligned[spread_std_key]

            # --- OFI proxy: change in (bid_depth - ask_depth) ---
            bid5_key = "ob_bn_bid_depth_5_last"
            ask5_key = "ob_bn_ask_depth_5_last"
            if bid5_key in bn_aligned and ask5_key in bn_aligned:
                bid5 = pd.Series(bn_aligned[bid5_key], index=df_1m.index)
                ask5 = pd.Series(bn_aligned[ask5_key], index=df_1m.index)

                depth_imb = bid5 - ask5
                for n in [1, 3, 5]:
                    cols[f"ob_ofi_proxy_{n}"] = depth_imb.diff(n).values

                # Depth ratio (bid/ask)
                depth_ratio = bid5 / (ask5 + 1e-10)
                cols["ob_depth_ratio_5"] = depth_ratio.values
                for w in [10, 20]:
                    m = depth_ratio.rolling(w).mean()
                    s = depth_ratio.rolling(w).std()
                    cols[f"ob_depth_ratio_5_zscore_{w}"] = (
                        (depth_ratio - m) / (s + 1e-10)
                    ).values

            bid10_key = "ob_bn_bid_depth_10_last"
            ask10_key = "ob_bn_ask_depth_10_last"
            if bid10_key in bn_aligned and ask10_key in bn_aligned:
                bid10 = pd.Series(bn_aligned[bid10_key], index=df_1m.index)
                ask10 = pd.Series(bn_aligned[ask10_key], index=df_1m.index)

                depth_imb10 = bid10 - ask10
                for n in [1, 3, 5]:
                    cols[f"ob_ofi_proxy10_{n}"] = depth_imb10.diff(n).values

                depth_ratio10 = bid10 / (ask10 + 1e-10)
                cols["ob_depth_ratio_10"] = depth_ratio10.values

    # ------------------------------------------------------------------
    # 2. Hyperliquid orderbook
    # ------------------------------------------------------------------
    hl_ob = None
    try:
        import db as _db

        hl_ob = _db.read_hl_orderbook()
    except Exception:
        pass

    if hl_ob is not None and len(hl_ob) >= 60:
        hl_value_cols = [
            "imbalance5", "imbalance10", "spread_bps",
            "bidDepth5", "askDepth5", "bidDepth10", "askDepth10",
        ]
        hl_agg = _aggregate_snapshots_to_1m(hl_ob, "datetime", hl_value_cols)
        hl_aligned = _align_1m_to_klines(hl_agg, df_1m, "ob_hl")

        if hl_aligned:
            # HL imbalance
            for level in [5, 10]:
                key_last = f"ob_hl_imbalance{level}_last"
                if key_last not in hl_aligned:
                    continue
                imb = pd.Series(hl_aligned[key_last], index=df_1m.index)
                cols[f"ob_hl_imb{level}"] = (imb - 0.5).values
                for n in [1, 3, 5]:
                    cols[f"ob_hl_imb{level}_chg_{n}"] = imb.diff(n).values
                for w in [20, 60]:
                    m = imb.rolling(w).mean()
                    s = imb.rolling(w).std()
                    cols[f"ob_hl_imb{level}_zscore_{w}"] = (
                        (imb - m) / (s + 1e-10)
                    ).values

            # HL spread
            hl_spread_key = "ob_hl_spread_bps_last"
            if hl_spread_key in hl_aligned:
                hl_spread = pd.Series(hl_aligned[hl_spread_key], index=df_1m.index)
                cols["ob_hl_spread"] = hl_spread.values
                for n in [1, 3, 5]:
                    cols[f"ob_hl_spread_chg_{n}"] = hl_spread.diff(n).values

            # HL OFI proxy
            hl_bid5_key = "ob_hl_bidDepth5_last"
            hl_ask5_key = "ob_hl_askDepth5_last"
            if hl_bid5_key in hl_aligned and hl_ask5_key in hl_aligned:
                hl_bid5 = pd.Series(hl_aligned[hl_bid5_key], index=df_1m.index)
                hl_ask5 = pd.Series(hl_aligned[hl_ask5_key], index=df_1m.index)
                hl_depth_imb = hl_bid5 - hl_ask5
                for n in [1, 3, 5]:
                    cols[f"ob_hl_ofi_proxy_{n}"] = hl_depth_imb.diff(n).values

    # ------------------------------------------------------------------
    # 3. Cross-exchange orderbook divergence
    # ------------------------------------------------------------------
    if "ob_imb5" in cols and "ob_hl_imb5" in cols:
        bn_imb = pd.Series(cols["ob_imb5"], index=df_1m.index)
        hl_imb = pd.Series(cols["ob_hl_imb5"], index=df_1m.index)
        divergence = bn_imb - hl_imb
        cols["ob_cross_imb_div"] = divergence.values
        for w in [10, 20]:
            cols[f"ob_cross_imb_div_ma_{w}"] = divergence.rolling(w).mean().values

    if "ob_spread" in cols and "ob_hl_spread" in cols:
        bn_sp = pd.Series(cols["ob_spread"], index=df_1m.index)
        hl_sp = pd.Series(cols["ob_hl_spread"], index=df_1m.index)
        sp_div = bn_sp - hl_sp
        cols["ob_cross_spread_div"] = sp_div.values
        for w in [10, 20]:
            cols[f"ob_cross_spread_div_ma_{w}"] = sp_div.rolling(w).mean().values

    # ------------------------------------------------------------------
    # 4. Hyperliquid asset context (impact spread, OI dynamics)
    # ------------------------------------------------------------------
    hl_ctx = None
    try:
        import db as _db

        hl_ctx = _db.read_hl_asset_ctx()
    except Exception:
        pass

    if hl_ctx is not None and len(hl_ctx) >= 60:
        ctx_value_cols = [
            "openInterest", "premium", "impactBid", "impactAsk",
            "dayNtlVlm",
        ]
        ctx_agg = _aggregate_snapshots_to_1m(hl_ctx, "datetime", ctx_value_cols)
        ctx_aligned = _align_1m_to_klines(ctx_agg, df_1m, "ob_hlctx")

        if ctx_aligned:
            # Impact spread: impactAsk - impactBid (market impact cost)
            ia_key = "ob_hlctx_impactAsk_last"
            ib_key = "ob_hlctx_impactBid_last"
            if ia_key in ctx_aligned and ib_key in ctx_aligned:
                impact_ask = pd.Series(ctx_aligned[ia_key], index=df_1m.index)
                impact_bid = pd.Series(ctx_aligned[ib_key], index=df_1m.index)
                impact_mid = (impact_ask + impact_bid) / 2
                impact_spread = (impact_ask - impact_bid) / (impact_mid + 1e-10) * 10_000
                cols["ob_impact_spread_bps"] = impact_spread.values
                for n in [1, 3, 5]:
                    cols[f"ob_impact_spread_chg_{n}"] = impact_spread.diff(n).values
                for w in [20, 60]:
                    m = impact_spread.rolling(w).mean()
                    s = impact_spread.rolling(w).std()
                    cols[f"ob_impact_spread_zscore_{w}"] = (
                        (impact_spread - m) / (s + 1e-10)
                    ).values

            # HL OI from snapshots (higher frequency than futures endpoint)
            oi_key = "ob_hlctx_openInterest_last"
            if oi_key in ctx_aligned:
                hl_oi = pd.Series(ctx_aligned[oi_key], index=df_1m.index)
                for n in [1, 5, 10]:
                    cols[f"ob_hl_oi_chg_{n}"] = hl_oi.pct_change(n).values
                for w in [20, 60]:
                    m = hl_oi.rolling(w).mean()
                    s = hl_oi.rolling(w).std()
                    cols[f"ob_hl_oi_zscore_{w}"] = ((hl_oi - m) / (s + 1e-10)).values

            # HL intraday notional volume momentum
            vol_key = "ob_hlctx_dayNtlVlm_last"
            if vol_key in ctx_aligned:
                ntl_vol = pd.Series(ctx_aligned[vol_key], index=df_1m.index)
                # dayNtlVlm is cumulative within the day, so take diff for flow
                ntl_flow = ntl_vol.diff()
                for w in [5, 10, 20]:
                    cols[f"ob_hl_ntl_flow_ma_{w}"] = ntl_flow.rolling(w).mean().values

    if not cols:
        return pd.DataFrame(index=df_1m.index)

    return pd.DataFrame(cols, index=df_1m.index)


# ---------------------------------------------------------------------------
# Enhanced features orchestrator
# ---------------------------------------------------------------------------


def build_enhanced_features(
    df_1m: pd.DataFrame,
    eth_1m: pd.DataFrame | None = None,
    btc_30m: pd.DataFrame | None = None,
    btc_4h: pd.DataFrame | None = None,
    coinbase_1m: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build all enhanced features and concat with original index.

    Args:
        df_1m: BTC 1min klines
        eth_1m: ETH 1min klines (optional, for cross-asset)
        btc_30m: BTC 30min klines (optional, from Binance; fallback: resample)
        btc_4h: BTC 4h klines (optional, for multi-timeframe features)
        coinbase_1m: Coinbase BTC-USD 1min klines (optional, for premium)
    """
    parts = [
        cvd_features(df_1m),
        cross_asset_features(df_1m, eth_1m),
        regime_indicator_features(df_1m),
        behavioral_state_features(df_1m, btc_30m),
        multi_timeframe_features(df_1m, btc_30m, btc_4h),
        coinbase_premium_features(df_1m, coinbase_1m),
        hyperliquid_premium_features(df_1m),
        orderbook_microstructure_features(df_1m),
    ]

    result = pd.concat([p for p in parts if not p.empty], axis=1)
    return result


# ---------------------------------------------------------------------------
# Interaction features (cross-feature combinations)
# ---------------------------------------------------------------------------


def _interaction_features(features_df: pd.DataFrame) -> pd.DataFrame:
    """Build interaction features from the v10 core 5 features.

    Unlike other builder functions that take raw 1m klines, this operates on
    the already-concatenated feature DataFrame.  It is called after
    ``pd.concat(feat_parts)`` but before 5-minute sampling.

    Core 5: taker_vol_raw, price_vs_rvwap_60, cvd_slope_10, hour4_sin, vpt_sum_30
    """
    cols: dict[str, pd.Series] = {}

    has_taker = "taker_vol_raw" in features_df.columns
    has_pvr = "price_vs_rvwap_60" in features_df.columns
    has_cvd = "cvd_slope_10" in features_df.columns
    has_h4 = "hour4_sin" in features_df.columns
    has_vpt = "vpt_sum_30" in features_df.columns

    tv = features_df["taker_vol_raw"] if has_taker else None
    pvr = features_df["price_vs_rvwap_60"] if has_pvr else None
    cvd = features_df["cvd_slope_10"] if has_cvd else None
    h4 = features_df["hour4_sin"] if has_h4 else None
    vpt = features_df["vpt_sum_30"] if has_vpt else None

    # --- Products (prefix ix_mul_) ---
    if tv is not None and cvd is not None:
        cols["ix_mul_taker_cvd"] = tv * cvd
    if tv is not None and pvr is not None:
        cols["ix_mul_taker_pvr"] = tv * pvr
    if tv is not None and vpt is not None:
        cols["ix_mul_taker_vpt"] = tv * vpt
    if cvd is not None and pvr is not None:
        cols["ix_mul_cvd_pvr"] = cvd * pvr
    if pvr is not None and vpt is not None:
        cols["ix_mul_pvr_vpt"] = pvr * vpt

    # --- Ratios (prefix ix_ratio_) ---
    if tv is not None and vpt is not None:
        cols["ix_ratio_taker_vpt"] = tv / (vpt + 1e-10)
    if tv is not None and cvd is not None:
        cols["ix_ratio_taker_cvd"] = tv / (cvd.abs() + 1e-10)
    if cvd is not None and pvr is not None:
        cols["ix_ratio_cvd_pvr"] = cvd / (pvr.abs() + 1e-10)

    # --- Conditional / directional (prefix ix_cond_) ---
    if tv is not None and cvd is not None:
        cols["ix_cond_taker_cvdpos"] = tv * (cvd > 0).astype(float)
    if tv is not None and pvr is not None:
        cols["ix_cond_taker_pvrpos"] = tv * (pvr > 0).astype(float)
    if pvr is not None and cvd is not None:
        cols["ix_cond_pvr_cvdpos"] = pvr * (cvd > 0).astype(float)

    # --- Time-conditional (prefix ix_time_) ---
    if tv is not None and h4 is not None:
        cols["ix_time_taker_h4sin"] = tv * h4
    if cvd is not None and h4 is not None:
        cols["ix_time_cvd_h4sin"] = cvd * h4

    if not cols:
        return pd.DataFrame(index=features_df.index)
    return pd.DataFrame(cols, index=features_df.index)


# ---------------------------------------------------------------------------
# Build & export
# ---------------------------------------------------------------------------


def build_features(
    df_1m: pd.DataFrame,
    btc_30m: pd.DataFrame | None = None,
    btc_4h: pd.DataFrame | None = None,
    coinbase_1m: pd.DataFrame | None = None,
    eth_1m: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build feature matrix aligned to 5-minute window boundaries.

    For window [t, t+5min), features are sampled at t-1min
    (the last complete 1m candle before the window opens).
    This means we take rows where minute % 5 == 4 (i.e., :04, :09, :14...).

    Args:
        df_1m: BTC 1-minute klines
        btc_30m: BTC 30-minute klines (optional; for behavioral state + MTF features)
        btc_4h: BTC 4-hour klines (optional; for MTF features)
        coinbase_1m: Coinbase BTC-USD 1-minute klines (optional; for premium features)
        eth_1m: ETH 1-minute klines (optional; for cross-asset features)
    """
    # Compute all features on the full 1m series
    feat_parts = [
        _talib_features(df_1m),
        _momentum_features(df_1m),
        _volatility_features(df_1m),
        _volume_features(df_1m),
        _qlib_alpha_factors(df_1m),
        _time_features(df_1m),
        _microstructure_features(df_1m),
        _futures_features(df_1m),
    ]

    # Enhanced features (CVD, regime indicators, behavioral state, Coinbase premium)
    enhanced = build_enhanced_features(
        df_1m, eth_1m=eth_1m, btc_30m=btc_30m, btc_4h=btc_4h, coinbase_1m=coinbase_1m
    )
    if not enhanced.empty:
        feat_parts.append(enhanced)

    features = pd.concat(feat_parts, axis=1)

    # Interaction features (depend on columns from multiple builders)
    ix_feats = _interaction_features(features)
    if not ix_feats.empty:
        features = pd.concat([features, ix_feats], axis=1)

    features["open_time"] = df_1m["open_time"]

    # Sample at 5-minute boundaries minus 1 minute
    # For window starting at :05, sample features at :04
    minutes = features["open_time"].dt.minute
    mask = minutes % WINDOW_MINUTES == (WINDOW_MINUTES - 1)
    features = features[mask].copy()

    # Map to window_start: feature at :04 → window :05, feature at :09 → window :10
    features["window_start"] = features["open_time"] + pd.Timedelta(minutes=1)
    features = features.drop(columns=["open_time"])

    return features


def get_feature_columns(features: pd.DataFrame) -> list[str]:
    """Return list of feature column names (excludes window_start and tick_offset)."""
    return [c for c in features.columns if c not in ("window_start", "tick_offset")]


if __name__ == "__main__":
    from config import PARQUET_FILE

    df_1m = pd.read_parquet(PARQUET_FILE)
    features = build_features(df_1m)
    feat_cols = get_feature_columns(features)
    print(f"Feature matrix: {features.shape}")
    print(f"Feature count: {len(feat_cols)}")
    print(f"\nNaN ratio per feature (top 10):")
    nan_ratio = features[feat_cols].isna().mean().sort_values(ascending=False)
    print(nan_ratio.head(10))

    # Feature count per category
    print(f"\n--- Features per category ---")
    categories = {
        "TA-Lib": _talib_features(df_1m),
        "Momentum": _momentum_features(df_1m),
        "Volatility": _volatility_features(df_1m),
        "Volume": _volume_features(df_1m),
        "Qlib Alpha": _qlib_alpha_factors(df_1m),
        "Time": _time_features(df_1m),
        "Microstructure": _microstructure_features(df_1m),
        "Futures": _futures_features(df_1m),
    }
    total = 0
    for name, cat_df in categories.items():
        n = len(cat_df.columns)
        total += n
        print(f"  {name:20s}: {n:4d}")
    print(f"  {'TOTAL':20s}: {total:4d}")
