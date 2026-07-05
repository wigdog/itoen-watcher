# -*- coding: utf-8 -*-
"""
stats.py — 空室発生・消滅のベイズ推定モジュール
================================================

考え方（チャットでの導出をそのままコード化）:

  部屋の状態を「満室(×)」⇔「空き(○)」を行き来する2状態の交代更新過程とみなす。

  - 満室が続く時間 ~ 指数分布(λ)   → 平均 1/λ 日おきに1回、空きが発生する
  - 空きが続く時間 ~ 指数分布(μ)   → 平均 1/μ 日、他人に取られず残っている

  λ: 事後分布 Gamma(1 + 観測された発生件数, 1 + 観測期間[日])
     事前分布は Gamma(1,1)（緩い事前知識）。
     事後平均 λ_hat = (1+k) / (1+T)

  μ: 「○→×」に戻った実例（＝空きの窓が閉じた実例）の平均継続時間の逆数。
     観測例が無いうちは推定不能なので、保守的な仮定値を使い、
     「まだ実測していません」と明示する。

  チェック間隔Δt(日)で、その窓を少なくとも1回のチェックで捕まえられる確率:
     P(catch) = (1 - e^(-μΔt)) / (μΔt)

  実効検知率 λ' = λ_hat × P(catch)
  残り日数D日の間に、少なくとも1回捕まえられる確率:
     P(success) = 1 - e^(-λ' × D)
"""

import math
from datetime import datetime
from typing import Optional


def estimate_lambda(arrivals: int, observed_days: float) -> float:
    """
    λ(1日あたりの「空き発生」件数)の事後平均を返す。
    事前分布: Gamma(1,1)
    """
    observed_days = max(observed_days, 1e-6)
    return (1 + arrivals) / (1 + observed_days)


def estimate_mu(open_window_hours: list) -> Optional[float]:
    """
    μ(1日あたりの「空きが閉じる」レート)の推定値を返す。
    観測された「窓の継続時間(時間)」のリストから平均継続時間を出し、
    その逆数を日単位のレートに変換する。
    観測例が無ければ None を返す（=推定不能、まだ実測していない）。
    """
    if not open_window_hours:
        return None
    mean_hours = sum(open_window_hours) / len(open_window_hours)
    mean_hours = max(mean_hours, 1e-6)
    return 24.0 / mean_hours  # 1日あたりの「閉じる」レート


def p_catch_per_check(mu_per_day: float, check_interval_hours: float) -> float:
    """
    1回のチェックで、開いている窓を捕まえられる確率。
    P(catch) = (1 - e^(-a)) / a,  a = μ × Δt（単位を日にそろえる）
    """
    delta_t_days = check_interval_hours / 24.0
    a = mu_per_day * delta_t_days
    if a < 1e-9:
        return 1.0
    return (1 - math.exp(-a)) / a


def p_success_by_deadline(lambda_per_day: float, p_catch: float, remaining_days: float) -> float:
    """
    残りremaining_days日の間に、少なくとも1回「空きを見つけて捕まえられる」確率。
    """
    remaining_days = max(remaining_days, 0.0)
    effective_lambda = lambda_per_day * p_catch
    return 1 - math.exp(-effective_lambda * remaining_days)


def build_report(
    arrivals: int,
    observed_days: float,
    open_window_hours: list,
    check_interval_hours: float,
    remaining_days: float,
    assumed_window_hours_if_unknown: float = 1.0,
) -> dict:
    """
    現在のデータから、λ・μ・捕まえられる確率一式をまとめて返す。
    """
    lam = estimate_lambda(arrivals, observed_days)
    mu = estimate_mu(open_window_hours)
    mu_is_assumed = mu is None
    if mu_is_assumed:
        mu = 24.0 / assumed_window_hours_if_unknown

    p_catch = p_catch_per_check(mu, check_interval_hours)
    p_success = p_success_by_deadline(lam, p_catch, remaining_days)

    return {
        "lambda_per_day": lam,
        "mu_per_day": mu,
        "mu_is_assumed": mu_is_assumed,
        "mean_window_hours": (24.0 / mu) if mu > 0 else None,
        "p_catch_per_check": p_catch,
        "p_success_by_deadline": p_success,
        "arrivals_observed": arrivals,
        "observed_days": observed_days,
        "windows_observed": len(open_window_hours),
    }


def format_report_ja(report: dict, remaining_days: float, target_date: str) -> str:
    mu_note = "（未観測のため仮定値）" if report["mu_is_assumed"] else "（実測値ベース）"
    mean_window = report["mean_window_hours"]
    mean_window_str = f"{mean_window:.2f}時間" if mean_window else "不明"

    return (
        f"■ 統計推定（自動更新）\n"
        f"観測期間: {report['observed_days']:.2f}日 / 発生件数: {report['arrivals_observed']}件\n"
        f"λ(1日あたりの空き発生率): {report['lambda_per_day']:.3f}件/日\n"
        f"μ(空きの平均継続): {mean_window_str} {mu_note}"
        f"（観測窓数: {report['windows_observed']}件）\n"
        f"1回のチェックで捕まえる確率: {report['p_catch_per_check']*100:.1f}%\n"
        f"残り{remaining_days:.1f}日（〜{target_date}）で"
        f"少なくとも1回捕まえられる確率: {report['p_success_by_deadline']*100:.1f}%"
    )
