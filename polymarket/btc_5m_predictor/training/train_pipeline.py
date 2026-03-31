"""Dual-model training pipeline: base model (150d) + enhanced model (base + futures, 29d).

Base model:   Trained on full ~150-day data using long-coverage features only.
Enhanced model: Trained on ~29-day data using base features + futures features.

Inference: futures available → enhanced model; otherwise → base model.

Feature selection uses category-aware grouping: features are clustered by type,
top-K per category are selected, then a final unified pass picks the best across
categories. This prevents any single category from dominating.

Usage:
    uv run python -m training.train_pipeline
"""

import warnings
from datetime import UTC, datetime
from pathlib import Path

import catboost as cb
import lightgbm as lgb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    classification_report,
    roc_auc_score,
)

from config import (
    DATA_DIR,
    LABEL_THRESHOLD_PCT,
    PARQUET_FILE,
    PROJECT_DIR,
)
from .train_config import (
    EARLY_STOPPING_ROUNDS,
    MODELS_DIR,
    NUM_BOOST_ROUND,
)
from data.features import build_features, get_feature_columns
from data.labels import generate_labels

warnings.filterwarnings("ignore", category=UserWarning)
optuna.logging.set_verbosity(optuna.logging.WARNING)

# Futures feature prefixes (short-coverage, ~29 days)
FUTURES_PREFIXES = (
    "oi_", "global_ls_", "ls_account_", "ls_position_", "taker_vol_",
    "eth_btc_", "eth_lead_",
    "hl_funding_", "hl_bn_spread_", "hl_premium_", "hl_vol_ratio",
    "ob_",
)

# Feature category definitions for category-aware selection
FEATURE_CATEGORIES = {
    "futures_oi":         lambda c: c.startswith("oi_"),
    "futures_ls_account": lambda c: c.startswith("ls_account_"),
    "futures_ls_position": lambda c: c.startswith("ls_position_"),
    "futures_global_ls":  lambda c: c.startswith("global_ls_"),
    "futures_taker_vol":  lambda c: c.startswith("taker_vol_"),
    "hyperliquid":        lambda c: c.startswith("hl_funding_") or c.startswith("hl_bn_spread_") or c.startswith("hl_premium_") or c.startswith("hl_vol_ratio"),
    "cross_asset_eth":    lambda c: c.startswith("eth_btc_") or c.startswith("eth_lead_"),
    "coinbase_premium":   lambda c: c.startswith("cb_premium"),
    "mtf_30m":            lambda c: c.startswith("mtf30_"),
    "mtf_4h":             lambda c: c.startswith("mtf4h_") or c.startswith("hour4_"),
    "ta_oscillator":      lambda c: any(c.startswith(p) for p in (
        "rsi_", "macd", "stoch", "cci_", "mfi_", "willr_", "adx_",
        "plus_di", "minus_di", "aroon", "bband", "atr_", "natr_",
    )),
    "ta_trend":           lambda c: any(c.startswith(p) for p in (
        "ema_", "sma_", "kama_", "mama_", "sar_", "wma_", "trima_",
    )),
    "ta_regression":      lambda c: any(c.startswith(p) for p in ("linreg_", "tsf_", "beta_")),
    "ta_correlation":     lambda c: any(c.startswith(p) for p in ("correl_", "RSQR", "CORD")),
    "volume_flow":        lambda c: any(c.startswith(p) for p in (
        "vol_", "obv_", "ad_", "vpt_", "mfm_",
    )),
    "orderbook":          lambda c: c.startswith("ob_"),
    "microstructure":     lambda c: any(c.startswith(p) for p in ("cvd_", "flow_", "trade_")),
    "microstructure_stat": lambda c: any(c.startswith(p) for p in (
        "autocorr_", "hurst_", "mean_rev_",
    )),
    "momentum":           lambda c: any(c.startswith(p) for p in (
        "ret_", "mom_", "trend_strength",
    )),
    "risk_adjusted":      lambda c: c.startswith("rolling_sharpe") or c.startswith("info_ratio"),
    "volatility":         lambda c: any(c.startswith(p) for p in (
        "realized_vol", "garman_klass", "parkinson_", "vol_of_vol", "range_",
    )),
    "qlib_kbar":          lambda c: any(c.startswith(p) for p in (
        "KBAR_", "KMID", "KLEN", "KSFT", "KUP", "KLOW", "KMAX", "KMIN",
    )),
    "qlib_alpha":         lambda c: any(c.startswith(p) for p in (
        "STD_", "RESI", "MAX_", "MIN_", "QTLU", "QTLD", "RANK_", "RSV",
        "IMAX", "IMIN", "IMXD", "CNTP", "CNTN", "CNTD", "SUMP", "SUMN",
        "SUMD", "VMA", "VSTD",
    )),
    "regime":             lambda c: any(c.startswith(p) for p in ("regime_", "behavioral_")),
    "time":               lambda c: any(c.startswith(p) for p in (
        "hour_", "dow_", "is_", "session_",
    )),
}


def _next_version_dir() -> tuple[str, Path]:
    """Generate auto-incrementing version directory."""
    existing = sorted(MODELS_DIR.glob("v*"))
    max_num = 0
    for d in existing:
        parts = d.name.lstrip("v").split("_", 1)
        try:
            max_num = max(max_num, int(parts[0]))
        except ValueError:
            continue
    next_num = max_num + 1
    date_str = datetime.now(UTC).strftime("%Y%m%d")
    version_name = f"v{next_num}_{date_str}"
    version_dir = MODELS_DIR / version_name
    version_dir.mkdir(parents=True, exist_ok=True)
    return version_name, version_dir


def _update_active_version(version_name: str) -> None:
    """Update ACTIVE_MODEL_VERSION in config.py to point to the new version."""
    config_path = PROJECT_DIR / "config.py"
    text = config_path.read_text()
    import re
    new_text = re.sub(
        r'^ACTIVE_MODEL_VERSION\s*=\s*"[^"]*"',
        f'ACTIVE_MODEL_VERSION = "{version_name}"',
        text,
        flags=re.MULTILINE,
    )
    config_path.write_text(new_text)
    print(f"  config.py updated: ACTIVE_MODEL_VERSION = \"{version_name}\"")


def _save_feature_manifest(version_dir: Path, base_features: list[str],
                           enhanced_features: list[str] | None = None) -> Path:
    """Save a feature manifest with source file hashes for deployment verification."""
    import hashlib
    import json

    source_files = [PROJECT_DIR / "data" / "features.py"]
    file_hashes = {}
    for sf in source_files:
        if sf.exists():
            h = hashlib.sha256(sf.read_bytes()).hexdigest()[:16]
            file_hashes[sf.name] = h

    all_features = enhanced_features or base_features
    futures_feats = [f for f in all_features if any(f.startswith(p) for p in FUTURES_PREFIXES)]
    base_only = [f for f in all_features if f not in futures_feats]

    manifest = {
        "version": version_dir.name,
        "created_at": datetime.now(UTC).isoformat(),
        "dual_model": enhanced_features is not None,
        "base_model_features": base_features,
        "enhanced_model_features": enhanced_features or [],
        "n_base_features": len(base_features),
        "n_enhanced_features": len(enhanced_features) if enhanced_features else 0,
        "n_futures_in_enhanced": len(futures_feats),
        "source_hashes": file_hashes,
    }

    manifest_path = version_dir / "feature_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"  Feature manifest saved to {manifest_path}")
    return manifest_path


# ---------------------------------------------------------------------------
# Purged TimeSeriesSplit
# ---------------------------------------------------------------------------

class PurgedTimeSeriesSplit:
    """Time-series cross-validation with purge gap to prevent leakage."""

    def __init__(self, n_splits: int = 5, purge_gap: int = 12, test_ratio: float = 0.15):
        self.n_splits = n_splits
        self.purge_gap = purge_gap
        self.test_ratio = test_ratio

    def split(self, X):
        n = len(X) if hasattr(X, '__len__') else X.shape[0]
        test_size = int(n * self.test_ratio)
        min_train = max(test_size * 2, 500)

        for i in range(self.n_splits):
            test_end = n - i * test_size
            test_start = test_end - test_size
            train_end = test_start - self.purge_gap

            if train_end < min_train:
                break

            train_idx = np.arange(0, train_end)
            test_idx = np.arange(test_start, test_end)
            yield train_idx, test_idx


# ---------------------------------------------------------------------------
# Feature classification
# ---------------------------------------------------------------------------

_FUTURES_PREFIXES = (
    "funding_", "oi_", "ls_account_", "ls_position_",
    "global_ls_", "taker_vol_",
    "hl_funding_", "hl_bn_spread_", "hl_premium_", "hl_vol_ratio",
    "ob_",
)


def split_feature_cols(all_feat_cols: list[str], features_df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """Split features into base (long-coverage) and futures (short-coverage).

    Futures features are identified by prefix (data source), not by NaN ratio,
    so the enhanced model is always trained on the futures-available subset.
    """
    futures_cols = [c for c in all_feat_cols if any(c.startswith(p) for p in _FUTURES_PREFIXES)]
    base_cols = [c for c in all_feat_cols if c not in futures_cols]
    return base_cols, futures_cols


def categorize_features(feat_cols: list[str]) -> dict[str, list[str]]:
    """Assign each feature to a category. Unmatched go to 'other'."""
    result: dict[str, list[str]] = {}
    assigned = set()

    for cat_name, matcher in FEATURE_CATEGORIES.items():
        cols = [c for c in feat_cols if matcher(c)]
        if cols:
            result[cat_name] = cols
            assigned.update(cols)

    # Remaining → other
    other = [c for c in feat_cols if c not in assigned]
    if other:
        result["other"] = other

    return result


# ---------------------------------------------------------------------------
# Category-aware feature selection
# ---------------------------------------------------------------------------

def _lgb_importance(X: pd.DataFrame, y: pd.Series, feat_cols: list[str]) -> pd.Series:
    """Get LightGBM gain importance for given features using purged CV."""
    params = {
        "objective": "binary", "metric": "auc",
        "learning_rate": 0.05, "num_leaves": 31, "max_depth": 6,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
        "verbose": -1, "seed": 42,
    }
    cv_splitter = PurgedTimeSeriesSplit(n_splits=3, purge_gap=12)
    importance_sum = np.zeros(len(feat_cols))

    for train_idx, test_idx in cv_splitter.split(X):
        ds_tr = lgb.Dataset(X.iloc[train_idx][feat_cols], label=y.iloc[train_idx])
        ds_va = lgb.Dataset(X.iloc[test_idx][feat_cols], label=y.iloc[test_idx], reference=ds_tr)
        model = lgb.train(
            params, ds_tr, valid_sets=[ds_va],
            num_boost_round=300,
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)],
        )
        importance_sum += model.feature_importance(importance_type="gain")

    return pd.Series(importance_sum / 3, index=feat_cols).sort_values(ascending=False)


def _noise_threshold(X: pd.DataFrame, y: pd.Series, feat_cols: list[str],
                     n_shadow: int = 5, n_iter: int = 3) -> float:
    """Boruta-lite: determine importance noise floor using shadow features.

    Creates ``n_shadow`` random (row-shuffled) shadow features, trains LightGBM
    ``n_iter`` times, and returns the max importance observed across all shadow
    features and iterations.  Real features must exceed this threshold to be
    considered signal.
    """
    rng = np.random.RandomState(42)
    max_shadow_imp = 0.0

    for i in range(n_iter):
        # Create shadow features by shuffling random real columns
        shadow_cols = []
        chosen = rng.choice(feat_cols, size=min(n_shadow, len(feat_cols)), replace=False)
        X_aug = X[feat_cols].copy()
        for col in chosen:
            shadow_name = f"_shadow_{col}"
            X_aug[shadow_name] = rng.permutation(X[col].values)
            shadow_cols.append(shadow_name)

        all_cols = feat_cols + shadow_cols
        imp = _lgb_importance(X_aug, y, all_cols)
        shadow_imp = imp[shadow_cols]
        if len(shadow_imp) > 0:
            max_shadow_imp = max(max_shadow_imp, shadow_imp.max())

    return max_shadow_imp


def _forward_select_cv(X: pd.DataFrame, y: pd.Series,
                       candidates: list[str],
                       min_gain: float = 0.001,
                       max_features: int = 50,
                       top_k_try: int = 50,
                       seed_features: list[str] | None = None,
                       ) -> tuple[list[str], list[tuple]]:
    """Forward feature selection: greedily add features maximising CV AUC.

    If ``seed_features`` is provided, these are forced into the selected set
    first (baseline).  Forward selection then searches ``candidates`` for
    additional features that improve CV AUC beyond the seed baseline.

    Returns (selected_features, history) where history contains
    (feature, auc, gain) tuples.
    """
    cv = PurgedTimeSeriesSplit(n_splits=4, purge_gap=12)

    def _cv_auc(feat_list: list[str]) -> float:
        Xm = X[feat_list].values
        ym = y.values
        aucs = []
        for tr_idx, va_idx in cv.split(X):
            model = cb.CatBoostClassifier(
                iterations=200, depth=6, learning_rate=0.05,
                l2_leaf_reg=3, random_seed=42, verbose=0,
                early_stopping_rounds=30,
            )
            model.fit(Xm[tr_idx], ym[tr_idx],
                      eval_set=(Xm[va_idx], ym[va_idx]), verbose=0)
            pred = model.predict_proba(Xm[va_idx])[:, 1]
            aucs.append(roc_auc_score(ym[va_idx], pred))
        return float(np.mean(aucs))

    # Start from seed features (forced baseline)
    if seed_features:
        selected = list(seed_features)
        best_auc = _cv_auc(selected)
        print(f"    Seed baseline: {len(selected)} features, AUC={best_auc:.4f}")
    else:
        selected = []
        best_auc = 0.5

    history: list[tuple] = []

    for step in range(max_features - len(selected)):
        remaining = [f for f in candidates if f not in selected]
        if not remaining:
            break
        pool = remaining[:top_k_try]

        best_feat = None
        best_step_auc = best_auc
        for feat in pool:
            auc = _cv_auc(selected + [feat])
            if auc > best_step_auc:
                best_step_auc = auc
                best_feat = feat

        if best_feat is None or (best_step_auc - best_auc) < min_gain:
            break

        gain = best_step_auc - best_auc
        selected.append(best_feat)
        best_auc = best_step_auc
        history.append((best_feat, best_auc, gain))
        print(f"    Step {step+1:2d}: +{best_feat:40s} AUC={best_auc:.4f} (+{gain:.4f})")

    return selected, history


def select_features_by_category(
    X: pd.DataFrame,
    y: pd.Series,
    feat_cols: list[str],
    top_per_cat: int | None = None,
    final_top_n: int | None = None,
    noise_multiplier: float = 0.8,
    forward_min_gain: float = 0.001,
    label: str = "",
) -> list[str]:
    """Three-stage category-aware feature selection.

    Stage 1 (per-category): keep features with importance > 0.
            If top_per_cat is set, use fixed count instead.
    Stage 2 (Boruta-lite): compute noise floor via shadow features,
            keep candidates above ``noise_multiplier × noise_floor``.
            If final_top_n is set, use fixed count instead.
    Stage 3 (Forward Selection): greedily add features from Boruta
            survivors to maximise CV AUC, capturing interaction effects.
            Skipped when final_top_n is set (backward compatible).
    """
    print(f"\n{'='*60}")
    print(f"CATEGORY-AWARE FEATURE SELECTION{' — ' + label if label else ''}")
    if final_top_n is not None:
        print(f"  Mode: S1={'top '+str(top_per_cat)+'/cat' if top_per_cat else 'imp>0'}, S2=top {final_top_n}")
    else:
        print(f"  Mode: S1={'top '+str(top_per_cat)+'/cat' if top_per_cat else 'imp>0'}, "
              f"S2=boruta(x{noise_multiplier}), S3=forward-select")
    print(f"{'='*60}")

    categories = categorize_features(feat_cols)
    print(f"  {len(feat_cols)} features in {len(categories)} categories")

    # Stage 1: intra-category selection
    category_winners = []
    for cat_name in sorted(categories.keys()):
        cat_cols = categories[cat_name]
        importance = _lgb_importance(X, y, cat_cols)

        if top_per_cat is not None:
            if len(cat_cols) <= top_per_cat:
                winners = cat_cols
                print(f"  {cat_name:25s}: {len(cat_cols):4d} → {len(winners)} (all kept)")
            else:
                winners = importance.head(top_per_cat).index.tolist()
                print(f"  {cat_name:25s}: {len(cat_cols):4d} → {len(winners)}  (top: {importance.index[0]} gain={importance.iloc[0]:.1f})")
        else:
            positive = importance[importance > 0]
            winners = positive.index.tolist()
            print(f"  {cat_name:25s}: {len(cat_cols):4d} → {len(winners)}  (dropped {len(cat_cols)-len(winners)} zero-imp)")

        category_winners.extend(winners)

    # Deduplicate
    category_winners = list(dict.fromkeys(category_winners))
    print(f"\n  Stage 1 pool: {len(category_winners)} features")

    # Stage 2: global filter
    if final_top_n is not None:
        # Fixed count mode (backward compatible, skip Stage 3)
        importance = _lgb_importance(X, y, category_winners)
        selected = importance.head(final_top_n).index.tolist()
        print(f"  Stage 2: top {final_top_n} from pool → {len(selected)}")
    else:
        # Boruta-lite noise floor
        print(f"\n  Stage 2: Boruta-lite noise floor...")
        noise_floor = _noise_threshold(X, y, category_winners, n_shadow=10, n_iter=5)
        threshold = noise_floor * noise_multiplier
        importance = _lgb_importance(X, y, category_winners)
        boruta_survivors = importance[importance > threshold].index.tolist()
        print(f"  Noise floor: {noise_floor:.2f} × {noise_multiplier} = threshold {threshold:.2f}")
        print(f"  Boruta survivors: {len(boruta_survivors)} / {len(category_winners)}")

        # Stage 3: Forward selection
        # Boruta survivors are forced into the model (baseline).
        # Forward selection searches the remaining Stage 1 pool for
        # interaction partners that improve CV AUC beyond the baseline.
        interaction_pool = [f for f in importance.index if f not in boruta_survivors
                            and importance[f] > 0]
        print(f"\n  Stage 3: Forward selection (base={len(boruta_survivors)} Boruta survivors, "
              f"interaction pool={len(interaction_pool)}, "
              f"min_gain={forward_min_gain})...")
        selected, history = _forward_select_cv(
            X, y, interaction_pool,
            min_gain=forward_min_gain,
            max_features=50,
            top_k_try=min(50, len(interaction_pool)),
            seed_features=boruta_survivors,
        )
        n_added = len(selected) - len(boruta_survivors)
        print(f"\n  Forward selection: {len(boruta_survivors)} base + {n_added} interaction "
              f"= {len(selected)} features"
              + (f", AUC={history[-1][1]:.4f}" if history else f", AUC unchanged"))

    # Print top features
    print(f"\n  Selected features:")
    for i, feat in enumerate(selected):
        gain_val = importance.get(feat, 0)
        cat = "?"
        for cn, cols in categories.items():
            if feat in cols:
                cat = cn
                break
        src = "boruta+fwd" if final_top_n is None else "top-n"
        print(f"    {i+1:3d}. {feat:40s} imp={gain_val:.1f}  [{cat}]")

    print(f"\n  Final selected: {len(selected)}")
    return selected


# ---------------------------------------------------------------------------
# Flat feature selection (for enhanced model on futures rows)
# ---------------------------------------------------------------------------

def select_features(
    X: pd.DataFrame,
    y: pd.Series,
    feat_cols: list[str],
    top_n: int = 80,
    label: str = "",
) -> list[str]:
    """Select top_n features using purged time-series CV importance."""
    print(f"\n{'='*60}")
    print(f"FEATURE SELECTION{' — ' + label if label else ''}: {len(feat_cols)} → top {top_n}")
    print(f"{'='*60}")

    importance = _lgb_importance(X, y, feat_cols)
    selected = importance.head(top_n).index.tolist()

    print(f"\nTop 20 features:")
    for i, (feat, gain) in enumerate(importance.head(20).items()):
        marker = "✓" if feat in selected else " "
        print(f"  {marker} {i+1:3d}. {feat:35s} gain={gain:.1f}")

    zero_imp = (importance == 0).sum()
    print(f"\nZero-importance: {zero_imp} | Selected: {len(selected)}")
    return selected


# ---------------------------------------------------------------------------
# Optuna hyperparameter search
# ---------------------------------------------------------------------------

def optuna_search(
    X: pd.DataFrame,
    y: pd.Series,
    feat_cols: list[str],
    n_trials: int = 80,
    n_cv_folds: int = 4,
    label: str = "",
    loss_function: str = "Logloss",
    eval_metric: str = "AUC",
) -> dict:
    """Optuna hyperparameter search for CatBoost with purged CV."""
    print(f"\n{'='*60}")
    print(f"OPTUNA{' — ' + label if label else ''} ({n_trials} trials, {n_cv_folds}-fold)")
    print(f"{'='*60}")

    cv_splitter = PurgedTimeSeriesSplit(n_splits=n_cv_folds, purge_gap=12)

    def objective(trial: optuna.Trial) -> float:
        params = {
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
            "depth": trial.suggest_int("depth", 3, 10),
            "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 0.1, 10.0, log=True),
            "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 10, 100),
            "random_strength": trial.suggest_float("random_strength", 0.1, 10.0, log=True),
            "bagging_temperature": trial.suggest_float("bagging_temperature", 0.0, 5.0),
            "border_count": trial.suggest_int("border_count", 32, 255),
            "grow_policy": trial.suggest_categorical("grow_policy",
                                                      ["SymmetricTree", "Depthwise", "Lossguide"]),
        }

        auc_scores = []
        for train_idx, test_idx in cv_splitter.split(X):
            X_tr = X.iloc[train_idx][feat_cols].values
            y_tr = y.iloc[train_idx].values
            X_va = X.iloc[test_idx][feat_cols].values
            y_va = y.iloc[test_idx].values

            model = cb.CatBoostClassifier(
                loss_function=loss_function, eval_metric=eval_metric,
                random_seed=42, verbose=0,
                iterations=NUM_BOOST_ROUND,
                early_stopping_rounds=EARLY_STOPPING_ROUNDS,
                **params,
            )
            model.fit(X_tr, y_tr, eval_set=(X_va, y_va))
            y_prob = model.predict_proba(X_va)[:, 1]
            auc_scores.append(roc_auc_score(y_va, y_prob))

        return float(np.mean(auc_scores))

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = study.best_params
    print(f"\nBest CV AUC: {study.best_value:.4f}")
    for k, v in best.items():
        print(f"  {k}: {v}")

    _plot_optuna(study, label)
    return best


def _plot_optuna(study: optuna.Study, label: str = "") -> None:
    """Save Optuna optimization history plot."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    trials = study.trials
    values = [t.value for t in trials if t.value is not None]
    best_so_far = np.maximum.accumulate(values)
    axes[0].plot(values, alpha=0.4, label="Trial AUC")
    axes[0].plot(best_so_far, color="red", linewidth=2, label="Best so far")
    axes[0].set_xlabel("Trial")
    axes[0].set_ylabel("CV AUC")
    axes[0].set_title(f"Optimization History{' — ' + label if label else ''}")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    param_names = list(study.best_params.keys())
    param_values = {p: [] for p in param_names}
    obj_values = []
    for t in trials:
        if t.value is not None:
            obj_values.append(t.value)
            for p in param_names:
                param_values[p].append(t.params.get(p, np.nan))

    correlations = {}
    for p in param_names:
        pv = np.array(param_values[p])
        try:
            pv = pv.astype(float)
            valid = ~np.isnan(pv)
            if valid.sum() > 5:
                correlations[p] = abs(np.corrcoef(pv[valid], np.array(obj_values)[valid])[0, 1])
        except (ValueError, TypeError):
            continue

    if correlations:
        sorted_params = sorted(correlations.items(), key=lambda x: x[1], reverse=True)
        names, corrs = zip(*sorted_params[:10])
        axes[1].barh(range(len(names)), corrs)
        axes[1].set_yticks(range(len(names)))
        axes[1].set_yticklabels(names)
        axes[1].set_xlabel("|Correlation| with AUC")
        axes[1].set_title("Parameter Sensitivity")
        axes[1].invert_yaxis()
        axes[1].grid(True, alpha=0.3)

    fig.tight_layout()
    suffix = f"_{label.lower().replace(' ', '_')}" if label else ""
    path = PROJECT_DIR / "reports" / f"optuna_history{suffix}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"Optuna plot saved to {path}")


# ---------------------------------------------------------------------------
# Train + evaluate with purged CV
# ---------------------------------------------------------------------------

def train_and_evaluate(
    X: pd.DataFrame,
    y: pd.Series,
    labels_df: pd.DataFrame,
    feat_cols: list[str],
    best_params: dict,
    model_save_path: Path,
    n_cv_folds: int = 5,
    label: str = "",
    loss_function: str = "Logloss",
    eval_metric: str = "AUC",
) -> tuple[pd.DataFrame, cb.CatBoostClassifier]:
    """Train CatBoost with best params, evaluate with purged CV. Returns (cv_preds, final_model)."""
    print(f"\n{'='*60}")
    print(f"FINAL MODEL{' — ' + label if label else ''}: {n_cv_folds}-fold purged CV")
    print(f"{'='*60}")

    cv_splitter = PurgedTimeSeriesSplit(n_splits=n_cv_folds, purge_gap=12)
    fold_results = []
    all_test_preds = []

    for fold_i, (train_idx, test_idx) in enumerate(cv_splitter.split(X)):
        X_tr = X.iloc[train_idx][feat_cols].values
        y_tr = y.iloc[train_idx].values
        X_va = X.iloc[test_idx][feat_cols].values
        y_va = y.iloc[test_idx].values

        model = cb.CatBoostClassifier(
            loss_function=loss_function, eval_metric=eval_metric,
            random_seed=42, verbose=0,
            iterations=NUM_BOOST_ROUND,
            early_stopping_rounds=EARLY_STOPPING_ROUNDS,
            **best_params,
        )
        model.fit(X_tr, y_tr, eval_set=(X_va, y_va))

        y_prob = model.predict_proba(X_va)[:, 1]
        y_pred = (y_prob > 0.5).astype(int)

        acc = accuracy_score(y_va, y_pred)
        auc = roc_auc_score(y_va, y_prob)
        brier = brier_score_loss(y_va, y_prob)

        best_iter = model.get_best_iteration() or model.tree_count_
        fold_results.append({"fold": fold_i + 1, "acc": acc, "auc": auc, "brier": brier,
                             "n_train": len(train_idx), "n_test": len(test_idx),
                             "best_iter": best_iter})

        fold_preds = X.iloc[test_idx][["window_start"]].copy()
        fold_preds["label"] = y_va
        fold_preds["y_prob"] = y_prob
        fold_preds["fold"] = fold_i + 1
        fold_preds = fold_preds.merge(
            labels_df[["window_start", "open_price", "close_price"]],
            on="window_start", how="left",
        )
        all_test_preds.append(fold_preds)

        print(f"  Fold {fold_i+1}: AUC={auc:.4f}  Acc={acc:.4f}  Brier={brier:.4f}  "
              f"(train={len(train_idx)}, test={len(test_idx)}, iter={best_iter})")

    results_df = pd.DataFrame(fold_results)
    print(f"\n{'─'*60}")
    print(f"  Mean AUC:   {results_df['auc'].mean():.4f} ± {results_df['auc'].std():.4f}")
    print(f"  Mean Acc:   {results_df['acc'].mean():.4f} ± {results_df['acc'].std():.4f}")
    print(f"  Mean Brier: {results_df['brier'].mean():.4f} ± {results_df['brier'].std():.4f}")

    all_preds = pd.concat(all_test_preds, ignore_index=True)

    # Train final production model on train data only (NOT holdout)
    print(f"\nTraining final production model on {len(X)} samples...")
    final_model = cb.CatBoostClassifier(
        loss_function="Logloss", eval_metric="AUC",
        random_seed=42, verbose=0,
        iterations=int(results_df["best_iter"].mean()),
        **best_params,
    )
    final_model.fit(X[feat_cols].values, y.values)
    model_save_path.parent.mkdir(parents=True, exist_ok=True)
    final_model.save_model(str(model_save_path))
    print(f"Model saved to {model_save_path}")

    return all_preds, final_model


# ---------------------------------------------------------------------------
# Holdout validation
# ---------------------------------------------------------------------------

def holdout_validation(
    holdout_set: pd.DataFrame,
    y_holdout: pd.Series,
    feat_cols: list[str],
    model_path: Path,
    label: str = "",
) -> tuple[float, float, float]:
    """Evaluate model on holdout data. Returns (acc, auc, brier)."""
    print(f"\n{'='*60}")
    print(f"HOLDOUT VALIDATION{' — ' + label if label else ''} ({len(holdout_set)} samples)")
    print(f"{'='*60}")

    model = cb.CatBoostClassifier()
    model.load_model(str(model_path))
    X_ho = holdout_set[feat_cols].values
    y_ho = y_holdout.values
    prob_ho = model.predict_proba(X_ho)[:, 1]
    pred_ho = (prob_ho > 0.5).astype(int)

    ho_acc = accuracy_score(y_ho, pred_ho)
    ho_auc = roc_auc_score(y_ho, prob_ho)
    ho_brier = brier_score_loss(y_ho, prob_ho)
    print(f"  Accuracy:    {ho_acc:.4f}")
    print(f"  ROC-AUC:     {ho_auc:.4f}")
    print(f"  Brier Score: {ho_brier:.4f}")

    if ho_acc < 0.52:
        print(f"  *** WARNING: Holdout accuracy {ho_acc:.1%} near random ***")
    if ho_auc < 0.55:
        print(f"  *** WARNING: Holdout AUC {ho_auc:.4f} near random — DO NOT deploy ***")

    import json
    suffix = label.lower().replace(" ", "_") if label else "holdout"
    ho_meta = {
        "label": label,
        "n_samples": len(holdout_set),
        "accuracy": ho_acc,
        "auc": ho_auc,
        "brier": ho_brier,
        "time_start": str(holdout_set["window_start"].min()) if "window_start" in holdout_set.columns else None,
        "time_end": str(holdout_set["window_start"].max()) if "window_start" in holdout_set.columns else None,
    }
    ho_path = PROJECT_DIR / "reports" / f"holdout_{suffix}.json"
    ho_path.write_text(json.dumps(ho_meta, indent=2))

    return ho_acc, ho_auc, ho_brier


# ---------------------------------------------------------------------------
# Calibration + feature importance
# ---------------------------------------------------------------------------

def print_calibration(y_true: np.ndarray, y_prob: np.ndarray) -> None:
    print(f"\nCalibration (predicted prob → actual Up%):")
    print(f"{'Bucket':<15} {'Count':>6} {'Actual Up%':>10} {'Avg Pred':>10}")
    print("-" * 45)
    edges = [0.0, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 1.0]
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (y_prob >= lo) & (y_prob < hi)
        n = mask.sum()
        if n > 0:
            actual = y_true[mask].mean()
            avg_pred = y_prob[mask].mean()
            print(f"[{lo:.2f}, {hi:.2f})  {n:>6}   {actual:>9.1%}   {avg_pred:>9.3f}")


def comprehensive_evaluation(y_true: np.ndarray, y_prob: np.ndarray, label: str = "") -> dict:
    """Evaluate model beyond AUC: log loss, ECE, Brier decomposition, Kelly PnL simulation."""
    from sklearn.metrics import log_loss as sk_log_loss

    print(f"\n{'='*60}")
    print(f"COMPREHENSIVE EVALUATION{' — ' + label if label else ''}")
    print(f"{'='*60}")

    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_prob, dtype=float)
    n = len(y)

    auc = roc_auc_score(y, p)
    logloss = sk_log_loss(y, p)
    brier = brier_score_loss(y, p)
    acc = accuracy_score(y, (p > 0.5).astype(int))

    print(f"\n  1) Core Metrics")
    print(f"     AUC:       {auc:.4f}")
    print(f"     Log Loss:  {logloss:.4f}  (baseline 0.6931 = random)")
    print(f"     Brier:     {brier:.4f}  (baseline 0.2500 = random)")
    print(f"     Accuracy:  {acc:.4f}")

    n_bins = 10
    bin_edges = np.linspace(0, 1, n_bins + 1)
    reliability = 0.0
    resolution = 0.0
    base_rate = y.mean()
    uncertainty = base_rate * (1 - base_rate)

    bin_stats = []
    for i in range(n_bins):
        mask = (p >= bin_edges[i]) & (p < bin_edges[i + 1])
        if i == n_bins - 1:
            mask = mask | (p == bin_edges[i + 1])
        n_k = mask.sum()
        if n_k == 0:
            continue
        avg_pred = p[mask].mean()
        avg_true = y[mask].mean()
        reliability += n_k * (avg_pred - avg_true) ** 2
        resolution += n_k * (avg_true - base_rate) ** 2
        bin_stats.append((bin_edges[i], bin_edges[i + 1], n_k, avg_pred, avg_true))

    reliability /= n
    resolution /= n

    print(f"\n  2) Brier Decomposition")
    print(f"     Reliability (↓):  {reliability:.6f}")
    print(f"     Resolution (↑):   {resolution:.6f}")
    print(f"     Uncertainty:      {uncertainty:.6f}")

    ece = sum((n_k / n) * abs(ap - at) for _, _, n_k, ap, at in bin_stats)
    mce = max((abs(ap - at) for _, _, _, ap, at in bin_stats), default=0)
    print(f"\n  3) Calibration Error")
    print(f"     ECE:  {ece:.4f}    MCE:  {mce:.4f}")

    above_50 = p > 0.5
    below_50 = p <= 0.5
    if above_50.sum() > 0:
        print(f"\n  4) Confidence Bias")
        over_pred, over_actual = p[above_50].mean(), y[above_50].mean()
        under_pred, under_actual = p[below_50].mean(), y[below_50].mean()
        print(f"     Pred > 0.5: avg_pred={over_pred:.4f}  actual={over_actual:.4f}  → {'over' if over_pred > over_actual else 'under'}confident")
        print(f"     Pred ≤ 0.5: avg_pred={under_pred:.4f}  actual={under_actual:.4f}  → {'over' if under_pred < under_actual else 'under'}confident")

    return {"auc": auc, "log_loss": logloss, "brier": brier, "accuracy": acc,
            "reliability": reliability, "resolution": resolution, "ece": ece, "mce": mce}


def plot_feature_importance(model: cb.CatBoostClassifier, feat_cols: list[str], label: str = "") -> None:
    importance = model.get_feature_importance()
    feat_imp = pd.Series(importance, index=feat_cols).sort_values(ascending=False)

    fig, ax = plt.subplots(figsize=(10, 8))
    feat_imp.head(30).plot.barh(ax=ax)
    ax.set_title(f"Top 30 Feature Importance{' — ' + label if label else ''}")
    ax.set_xlabel("Gain")
    ax.invert_yaxis()
    fig.tight_layout()

    suffix = f"_{label.lower().replace(' ', '_')}" if label else ""
    path = PROJECT_DIR / "reports" / f"feature_importance{suffix}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"Feature importance plot saved to {path}")


# ---------------------------------------------------------------------------
# Run backtest + report for a model
# ---------------------------------------------------------------------------

def _run_backtest_report(cv_preds: pd.DataFrame, label: str) -> pd.DataFrame:
    """Run backtest and print report on CV predictions."""
    print(f"\n{'='*60}")
    print(f"BACKTEST — {label}")
    print(f"{'='*60}")
    try:
        from .backtest import print_report, plot_equity_curve, run_backtest, sensitivity_analysis
    except ImportError:
        from backtest import print_report, plot_equity_curve, run_backtest, sensitivity_analysis

    for fold in sorted(cv_preds["fold"].unique()):
        fold_preds = cv_preds[cv_preds["fold"] == fold]
        trades = run_backtest(fold_preds)
        if not trades.empty:
            wr = trades["correct"].mean()
            pnl = trades["pnl"].sum()
            print(f"  Fold {fold}: {len(trades)} trades, WR={wr:.1%}, PnL=${pnl:+.2f}")

    print(f"\n--- Combined (all folds) ---")
    all_trades = run_backtest(cv_preds)
    print_report(all_trades)
    sensitivity_analysis(cv_preds)
    return all_trades


# ---------------------------------------------------------------------------
# Main pipeline — dual model
# ---------------------------------------------------------------------------

def main(
    *,
    sample_start: str | None = None,
    sample_end: str | None = None,
    label_threshold: float | None = None,
    features_include: list[str] | None = None,
    features_exclude: list[str] | None = None,
    loss_function: str = "Logloss",
    eval_metric: str = "AUC",
    parent_run_id: str | None = None,
    tags: str | None = None,
) -> dict:
    """Train a model with configurable parameters.

    All parameters have defaults matching the original hardcoded behavior.

    Returns:
        dict with run_id, cv_auc, ho_auc, bt_sharpe, overfit_report, etc.
    """
    run_id = f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}"

    version_name, version_dir = _next_version_dir()
    print(f"New model version: {version_name}")
    print(f"  Directory: {version_dir}")

    # ===================================================================
    # 1. Load data
    # ===================================================================
    import db
    db.init_db()

    print("Loading 1m klines...")
    df_1m = None
    try:
        df_1m = db.read_klines()
        if df_1m is not None and len(df_1m) > 0:
            print(f"  {len(df_1m)} candles (from DuckDB)")
        else:
            df_1m = None
    except Exception:
        pass
    if df_1m is None:
        print("  Falling back to parquet...")
        df_1m = pd.read_parquet(PARQUET_FILE)
        print(f"  {len(df_1m)} candles (from parquet)")

    print("Loading 30m klines...")
    df_30m = None
    try:
        df_30m = db.read_klines_30m()
        if df_30m is not None and len(df_30m) > 0:
            print(f"  {len(df_30m)} 30m candles")
        else:
            df_30m = None
    except Exception:
        pass
    if df_30m is None:
        p30 = DATA_DIR / "raw" / "btcusdt_30m.parquet"
        if p30.exists():
            df_30m = pd.read_parquet(p30)
            print(f"  {len(df_30m)} 30m candles (from parquet)")

    print("Loading 4h klines...")
    df_4h = None
    try:
        df_4h = db.read_klines_4h()
        if df_4h is not None and len(df_4h) > 0:
            print(f"  {len(df_4h)} 4h candles")
        else:
            df_4h = None
    except Exception:
        pass
    if df_4h is None:
        p4h = DATA_DIR / "raw" / "btcusdt_4h.parquet"
        if p4h.exists():
            df_4h = pd.read_parquet(p4h)
            print(f"  {len(df_4h)} 4h candles (from parquet)")

    print("Loading Coinbase klines...")
    df_coinbase = None
    try:
        df_coinbase = db.read_coinbase_klines()
        if df_coinbase is not None and len(df_coinbase) > 0:
            print(f"  {len(df_coinbase)} Coinbase 1m candles")
        else:
            df_coinbase = None
    except Exception:
        pass

    print("Loading ETH klines...")
    df_eth = None
    try:
        df_eth = db.read_eth_klines()
        if df_eth is not None and len(df_eth) > 0:
            print(f"  {len(df_eth)} ETH 1m candles")
        else:
            df_eth = None
    except Exception:
        pass

    # --- Sample range filtering ---
    if sample_start or sample_end:
        if sample_start:
            df_1m = df_1m[df_1m["open_time"] >= pd.Timestamp(sample_start, tz="UTC")]
        if sample_end:
            df_1m = df_1m[df_1m["open_time"] <= pd.Timestamp(sample_end, tz="UTC")]
        print(f"  Filtered to {len(df_1m)} candles ({sample_start or '...'} → {sample_end or '...'})")

    # ===================================================================
    # 2. Labels + features
    # ===================================================================
    print("\nGenerating labels...")
    labels = generate_labels(df_1m)
    n_up = (labels["zone"] == "up").sum()
    n_down = (labels["zone"] == "down").sum()
    n_neutral = (labels["zone"] == "neutral").sum()
    print(f"  {len(labels)} windows (threshold ±{LABEL_THRESHOLD_PCT:.2f}%)")
    print(f"  Up: {n_up} ({n_up/len(labels):.1%})  Down: {n_down} ({n_down/len(labels):.1%})  "
          f"Neutral: {n_neutral} ({n_neutral/len(labels):.1%})")

    print("\nBuilding features (1m + 30m + 4h + Coinbase + ETH + futures)...")
    features = build_features(df_1m, btc_30m=df_30m, btc_4h=df_4h,
                              coinbase_1m=df_coinbase, eth_1m=df_eth)
    all_feat_cols = get_feature_columns(features)
    print(f"  {len(all_feat_cols)} raw features, {len(features)} samples")

    # Split features by coverage
    base_cols, futures_cols = split_feature_cols(all_feat_cols, features)

    # --- Feature include/exclude filtering ---
    if features_include or features_exclude:
        try:
            from data.feature_metadata import FEATURE_META
        except ImportError:
            FEATURE_META = {}

        def _expand_names(names: list[str]) -> set[str]:
            """Expand category names to individual feature names."""
            expanded = set()
            categories = {m.get("category", "") for m in FEATURE_META.values() if m.get("category")}
            for name in names:
                if name in categories:
                    expanded |= {k for k, m in FEATURE_META.items() if m.get("category") == name}
                else:
                    expanded.add(name)
            return expanded

        if features_include:
            keep = _expand_names(features_include)
            unknown = keep - set(all_feat_cols)
            if unknown:
                print(f"  WARNING: unknown features/categories: {unknown}")
            base_cols = [c for c in base_cols if c in keep]
            futures_cols = [c for c in futures_cols if c in keep]
            print(f"  Filtered to {len(base_cols) + len(futures_cols)} features (include: {features_include})")

        if features_exclude:
            drop = _expand_names(features_exclude)
            base_cols = [c for c in base_cols if c not in drop]
            futures_cols = [c for c in futures_cols if c not in drop]
            print(f"  Filtered to {len(base_cols) + len(futures_cols)} features (exclude: {features_exclude})")

    print(f"  Base features (long-coverage): {len(base_cols)}")
    print(f"  Futures features (short-coverage): {len(futures_cols)}")

    # --- Leakage checks ---
    from training.leakage_check import run_all_checks, LeakageError
    try:
        actual_days = (features["window_start"].max() - features["window_start"].min()).days
        try:
            from data.feature_metadata import FEATURE_META as _fm
        except ImportError:
            _fm = None
        leak_warnings = run_all_checks(
            purge_gap=12,
            feature_cols=base_cols + futures_cols,
            actual_days=actual_days,
            feature_meta=_fm,
        )
        for w in leak_warnings:
            print(f"  LEAK WARNING: {w}")
    except LeakageError as e:
        print(f"\n  LEAKAGE DETECTED: {e}")
        raise

    # ===================================================================
    # 3. Merge + prepare datasets
    # ===================================================================
    dataset_full = features.merge(
        labels[["window_start", "label", "zone", "ret_pct", "open_price", "close_price"]],
        on="window_start", how="inner",
    )
    dataset_full = dataset_full.dropna(subset=["label"])
    dataset_full = dataset_full.sort_values("window_start").reset_index(drop=True)

    # All trainable samples (up + down)
    dataset_all = dataset_full[dataset_full["zone"] != "neutral"].copy()
    dataset_all["label"] = dataset_all["label"].astype(int)
    neutral_set = dataset_full[dataset_full["zone"] == "neutral"].copy()
    print(f"\n  Total trainable: {len(dataset_all)}, neutral: {len(neutral_set)}")
    print(f"  Date range: {dataset_all['window_start'].min()} → {dataset_all['window_start'].max()}")

    # Drop 100%-NaN base features
    all_nan_base = [c for c in base_cols if dataset_all[c].isna().all()]
    if all_nan_base:
        base_cols = [c for c in base_cols if c not in all_nan_base]
        print(f"  Dropped {len(all_nan_base)} all-NaN base features")

    # Futures dataset: rows where futures are available
    usable_futures = [c for c in futures_cols if dataset_all[c].isna().mean() < 0.99]
    dropped_futures = len(futures_cols) - len(usable_futures)
    if dropped_futures:
        print(f"  Dropped {dropped_futures} all-NaN futures features")
    futures_cols = usable_futures

    dataset_futures = dataset_all.dropna(subset=futures_cols).copy() if futures_cols else pd.DataFrame()
    print(f"\n  Base dataset: {len(dataset_all)} samples ({len(dataset_all['window_start'].dt.date.unique())} days)")
    if len(dataset_futures) > 0:
        print(f"  Futures dataset: {len(dataset_futures)} samples ({len(dataset_futures['window_start'].dt.date.unique())} days)")
    print(f"  Base candidates: {len(base_cols)} | Futures candidates: {len(futures_cols)}")

    # ===================================================================
    # 4. BASE MODEL — full 150d data, category-aware feature selection
    # ===================================================================
    print(f"\n{'#'*60}")
    print(f"#  BASE MODEL (150d, base features only)")
    print(f"{'#'*60}")

    HOLDOUT_RATIO = 0.20
    base_holdout_idx = int(len(dataset_all) * (1 - HOLDOUT_RATIO))
    base_train = dataset_all.iloc[:base_holdout_idx].copy()
    base_holdout = dataset_all.iloc[base_holdout_idx:].copy()
    y_base_train = base_train["label"]
    y_base_holdout = base_holdout["label"]
    print(f"  Train: {len(base_train)} | Holdout: {len(base_holdout)}")

    # Category-aware feature selection on base features
    base_selected = select_features_by_category(
        base_train, y_base_train, base_cols,
        top_per_cat=5, final_top_n=80, label="Base",
    )

    base_model_path = version_dir / "model_base.cbm"
    base_features_path = version_dir / "selected_features_base.txt"
    base_features_path.write_text("\n".join(base_selected) + "\n")

    # Optuna for base model
    base_best_params = optuna_search(base_train, y_base_train, base_selected,
                                      n_trials=80, n_cv_folds=4, label="Base",
                                      loss_function=loss_function, eval_metric=eval_metric)

    # Train + evaluate base model
    base_cv_preds, base_model = train_and_evaluate(
        base_train, y_base_train, labels, base_selected, base_best_params,
        model_save_path=base_model_path, n_cv_folds=4, label="Base",
        loss_function=loss_function, eval_metric=eval_metric,
    )
    holdout_validation(base_holdout, y_base_holdout, base_selected, base_model_path, label="Base")
    plot_feature_importance(base_model, base_selected, label="Base")
    comprehensive_evaluation(base_cv_preds["label"].values, base_cv_preds["y_prob"].values, label="Base")
    base_trades = _run_backtest_report(base_cv_preds, label="Base")

    # ===================================================================
    # 5. ENHANCED MODEL — futures rows, base selected + all futures
    # ===================================================================
    enhanced_model = None
    enhanced_cv_preds = None
    enhanced_selected = []
    enhanced_trades = pd.DataFrame()

    if len(dataset_futures) > 500:
        print(f"\n{'#'*60}")
        print(f"#  ENHANCED MODEL (futures rows, base + futures features)")
        print(f"{'#'*60}")

        enh_holdout_idx = int(len(dataset_futures) * (1 - HOLDOUT_RATIO))
        enh_train = dataset_futures.iloc[:enh_holdout_idx].copy()
        enh_holdout = dataset_futures.iloc[enh_holdout_idx:].copy()
        y_enh_train = enh_train["label"]
        y_enh_holdout = enh_holdout["label"]
        print(f"  Train: {len(enh_train)} | Holdout: {len(enh_holdout)}")

        # Enhanced features = base selected + all futures
        enhanced_selected = base_selected + futures_cols
        print(f"  Enhanced features: {len(base_selected)} base + {len(futures_cols)} futures = {len(enhanced_selected)}")

        enhanced_model_path = version_dir / "model_enhanced.cbm"
        enhanced_features_path = version_dir / "selected_features_enhanced.txt"
        enhanced_features_path.write_text("\n".join(enhanced_selected) + "\n")

        # Optuna for enhanced model
        enh_best_params = optuna_search(enh_train, y_enh_train, enhanced_selected,
                                         n_trials=80, n_cv_folds=4, label="Enhanced",
                                         loss_function=loss_function, eval_metric=eval_metric)

        # Train + evaluate enhanced model
        enhanced_cv_preds, enhanced_model = train_and_evaluate(
            enh_train, y_enh_train, labels, enhanced_selected, enh_best_params,
            model_save_path=enhanced_model_path, n_cv_folds=4, label="Enhanced",
            loss_function=loss_function, eval_metric=eval_metric,
        )
        holdout_validation(enh_holdout, y_enh_holdout, enhanced_selected, enhanced_model_path, label="Enhanced")
        plot_feature_importance(enhanced_model, enhanced_selected, label="Enhanced")
        comprehensive_evaluation(enhanced_cv_preds["label"].values, enhanced_cv_preds["y_prob"].values, label="Enhanced")
        enhanced_trades = _run_backtest_report(enhanced_cv_preds, label="Enhanced")
    else:
        print(f"\n  Skipping enhanced model — only {len(dataset_futures)} futures rows (need 500+)")

    # ===================================================================
    # 6. Save artifacts + manifest
    # ===================================================================
    _save_feature_manifest(version_dir, base_selected,
                           enhanced_selected if enhanced_selected else None)

    # Save CV predictions
    base_cv_preds.to_parquet(version_dir / "cv_predictions_base.parquet", index=False)
    if enhanced_cv_preds is not None:
        enhanced_cv_preds.to_parquet(version_dir / "cv_predictions_enhanced.parquet", index=False)

    # ===================================================================
    # 7. Log to DuckDB (use enhanced if available, else base)
    # ===================================================================
    primary_cv = enhanced_cv_preds if enhanced_cv_preds is not None else base_cv_preds
    primary_trades = enhanced_trades if not enhanced_trades.empty else base_trades
    primary_model = enhanced_model if enhanced_model is not None else base_model
    primary_selected = enhanced_selected if enhanced_selected else base_selected

    try:
        y_all = primary_cv["label"]
        p_all = primary_cv["y_prob"]
        pred_all = (p_all > 0.5).astype(int)

        fold_aucs = []
        for fold in sorted(primary_cv["fold"].unique()):
            fp = primary_cv[primary_cv["fold"] == fold]
            fold_aucs.append(roc_auc_score(fp["label"], fp["y_prob"]))

        cv_auc = roc_auc_score(y_all, p_all)
        bt_sharpe = float(primary_trades["pnl"].mean() / (primary_trades["pnl"].std() + 1e-10) * np.sqrt(len(primary_trades))) if not primary_trades.empty else 0.0

        # --- Overfitting analysis ---
        from training.overfit_report import analyze as overfit_analyze, format_report as overfit_format

        # Compute train AUC from the final model on training data
        train_data = enh_train if enhanced_model else base_train
        train_y = y_enh_train if enhanced_model else y_base_train
        train_feats = primary_selected
        try:
            train_prob = primary_model.predict_proba(train_data[train_feats].values)[:, 1]
            _train_auc = float(roc_auc_score(train_y, train_prob))
        except Exception:
            _train_auc = None

        # Holdout AUC
        ho_data = enh_holdout if enhanced_model else base_holdout
        ho_y = y_enh_holdout if enhanced_model else y_base_holdout
        try:
            ho_prob = primary_model.predict_proba(ho_data[train_feats].values)[:, 1]
            _ho_auc = float(roc_auc_score(ho_y, ho_prob))
        except Exception:
            _ho_auc = None

        overfit_result = overfit_analyze(
            train_auc=_train_auc,
            cv_auc=cv_auc,
            ho_auc=_ho_auc,
            fold_aucs=fold_aucs,
        )
        print(f"\n{overfit_format(overfit_result)}")
        overfit_dict = overfit_result.to_dict()

        run_dict = {
            "created_at": datetime.now(UTC),
            "data_start": dataset_all["window_start"].min(),
            "data_end": dataset_all["window_start"].max(),
            "n_samples": len(dataset_all),
            "n_features": len(primary_selected),
            "optuna_trials": 80,
            "best_cv_auc": float(max(fold_aucs)) if fold_aucs else 0.0,
            "best_params": enh_best_params if enhanced_model else base_best_params,
            "cv_mean_auc": cv_auc,
            "cv_std_auc": float(np.std(fold_aucs)),
            "cv_mean_acc": accuracy_score(y_all, pred_all),
            "cv_mean_brier": brier_score_loss(y_all, p_all),
            "cv_folds": len(fold_aucs),
            "bt_total_trades": len(primary_trades) if not primary_trades.empty else 0,
            "bt_win_rate": float(primary_trades["correct"].mean()) if not primary_trades.empty else 0.0,
            "bt_total_pnl": primary_trades["pnl"].sum() if not primary_trades.empty else 0.0,
            "bt_max_drawdown": float((primary_trades["pnl"].cumsum() - primary_trades["pnl"].cumsum().cummax()).min()) if not primary_trades.empty else 0.0,
            "bt_sharpe": bt_sharpe,
            "bt_profit_factor": float(primary_trades[primary_trades["correct"]]["pnl"].sum() / (abs(primary_trades[~primary_trades["correct"]]["pnl"].sum()) + 1e-10)) if not primary_trades.empty else 0.0,
            "model_path": str(version_dir),
            "status": "completed",
            # New iteration-platform fields
            "feature_set": primary_selected,
            "parent_run_id": parent_run_id,
            "tags": tags,
            "loss_function": loss_function,
            "eval_metric": eval_metric,
            **overfit_dict,
        }
        db_run_id = db.insert_model_run(run_dict)
        importance_series = pd.Series(primary_model.get_feature_importance(), index=primary_selected)
        db.insert_feature_importance(db_run_id, importance_series)
        db.insert_cv_predictions(db_run_id, primary_cv)
        if not primary_trades.empty:
            db.insert_backtest_trades(db_run_id, primary_trades)
        print(f"\nResults logged to DuckDB (run_id={db_run_id})")
        run_id = db_run_id
    except Exception as e:
        print(f"\nWARNING: Failed to log to DuckDB: {e}")
        import traceback
        traceback.print_exc()
        cv_auc = 0.0
        bt_sharpe = 0.0
        _ho_auc = None
        overfit_result = None

    # --- Save reference distribution for PSI (Unit 6) ---
    try:
        ref_dist = {}
        train_data_for_ref = enh_train if enhanced_model else base_train
        for feat in primary_selected:
            if feat in train_data_for_ref.columns:
                vals = train_data_for_ref[feat].dropna()
                if len(vals) > 10:
                    quantiles = [float(vals.quantile(q / 10)) for q in range(1, 10)]
                    ref_dist[feat] = quantiles
        import json as _json
        ref_path = version_dir / "reference_distribution.json"
        ref_path.write_text(_json.dumps(ref_dist, indent=2))
        print(f"  Reference distribution saved: {ref_path}")
    except Exception as e:
        print(f"  WARNING: Failed to save reference distribution: {e}")

    # ===================================================================
    # 8. Activate new version
    # ===================================================================
    _update_active_version(version_name)

    try:
        from models.generate_model_report import generate_report
        report_path = generate_report(version_name)
        print(f"Model report: {report_path}")
    except Exception as e:
        print(f"WARNING: Failed to generate model report: {e}")

    # --- Auto-compare with parent or last run ---
    if parent_run_id:
        try:
            from cli.cmd_experiment import _compare
            print(f"\n--- Auto-compare with parent {parent_run_id} ---")
            _compare(parent_run_id, run_id, as_json=False)
        except Exception as e:
            print(f"  WARNING: Auto-compare failed: {e}")

    # ===================================================================
    # Summary
    # ===================================================================
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE — {version_name}")
    print(f"  Base model:     {base_model_path} ({len(base_selected)} features)")
    if enhanced_selected:
        print(f"  Enhanced model: {version_dir / 'model_enhanced.cbm'} ({len(enhanced_selected)} features)")
    print(f"  Inference: futures available → enhanced; otherwise → base")
    print(f"{'='*60}")

    return {
        "run_id": run_id,
        "version": version_name,
        "cv_auc": cv_auc,
        "ho_auc": _ho_auc,
        "bt_sharpe": bt_sharpe,
        "n_features": len(primary_selected),
        "overfit_report": overfit_result,
        "model_path": str(version_dir),
    }


if __name__ == "__main__":
    main()
