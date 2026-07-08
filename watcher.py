#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伊東園ホテルズ 空室監視ツール
============================

対象ページ（ユーザー確認済みの実URL）:
  https://www5.489pro.com/asp/g/c/calendar.asp?kp=itoen&ty=&sp=&lan=JPN

やること：
  1. Playwright(ヘッドレスブラウザ)で上記の空室検索フォームを開く
  2. 「エリアまたは施設」プルダウンは1回につき1施設しか選べないため、
     監視したいホテルの数だけ検索を繰り返す
  3. 指定した宿泊日・人数構成（大人1名×2部屋、など）で検索実行
  4. 検索結果ページのスクリーンショットを保存し、テキストから
     空室有無を推定する
  5. 前回チェック時と比較し、「空きなし→空きあり」に変わったら
     ntfy.sh / Discordで通知する

前提：
  pip install playwright
  playwright install chromium

【重要な注意】
  - 「エリアまたは施設」プルダウンは単一選択なので、3施設を見るには
    このスクリプトは内部で3回検索を実行します（1回の実行で3施設分
    チェックします）。
  - 検索結果ページの正確なDOM構造は未確認のため、空室判定は
    「テキストからの正規表現抽出」＋「念のためスクリーンショット保存」
    の二段構えにしています。最初のうちはスクリーンショットも
    目視で確認し、誤判定がないか確かめてください。
  - このスクリプトは予約の自動化はしません。空きを見つけて通知する
    だけです。予約は必ず自分の手で行ってください。
  - アクセス間隔は15〜30分に1回程度を目安にしてください。
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import stats

# ============================================================
# CONFIG - ここを自分の状況に合わせて書き換える
# ============================================================

SEARCH_URL = "https://www5.489pro.com/asp/g/c/calendar.asp?kp=itoen&ty=&sp=&lan=JPN"

# 監視したいホテル名（プルダウンの表示テキストと一致させる。
# 全角/半角スペースの有無はコード側で自動的に無視して照合するので、
# ここでは気にせず見たままの名前を書けばOK）
TARGET_HOTELS = [
    "伊東園ホテル",
    "伊東園ホテル別館",
    "伊東園ホテル松川館",
    "伊東園ホテル熱川",
    "伊東園ホテル 稲取",
]
TARGET_DATE = "2026-07-25"

# 人数・部屋構成
ADULTS_PER_ROOM = 1   # 「大人(1部屋あたり)」の数
NIGHTS = 1            # 泊数
ROOMS = 1             # 部屋数（大人1名・1部屋で検索）

# 空室ありとみなす記号／キーワード
AVAILABLE_MARKS = {"○", "◎", "△"}
FULL_MARKS = {"×", "－", "―", "満室", "×満室"}

# 通知方法は「空きが出た瞬間の即時通知」と「定期診断(ハートビート)」で
# それぞれ別々に指定する（1つの変数で両方を兼ねると、どちらかを変更した
# ときにもう片方まで巻き込んで変わってしまう事故が起きやすいため分離）。
# 選べる値: "ntfy" (お手軽・無料・登録不要) / "discord" / "email" (Gmail経由)
IMMEDIATE_NOTIFY_METHOD = "ntfy"   # 空きが出た瞬間はスマホにすぐ気づけるntfy
HEARTBEAT_NOTIFY_METHOD = "email"  # 定期診断はGmailに届くメールでまとめて確認
NTFY_TOPIC = "itoen-20260725yoyaku"
DISCORD_WEBHOOK_URL = ""

# --- email(Gmail)を使う場合 ---
# 送信元Gmailアカウントと、Googleの「アプリパスワード」（通常のログイン
# パスワードとは別物）が必要。取得方法はREADME.md参照。
#
# 【重要】ここには直接パスワードを書かないでください。
# GitHub Actions等で動かす場合は、リポジトリの Secrets に登録した
# 環境変数から読み込みます。ローカルでどうしても直書きしたい場合のみ、
# os.environ.get の第2引数（デフォルト値）部分を書き換えてください。
# ただしその場合、このファイルを絶対にGitHubなど公開の場に上げないこと。
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "zakusenwig@gmail.com")

# 状態保存・スクリーンショット保存先
STATE_FILE = Path(__file__).parent / "state.json"
SHOT_DIR = Path(__file__).parent / "screenshots"
SHOT_DIR.mkdir(exist_ok=True)

# --loop で連続実行する場合のチェック間隔（分）。
# 短すぎるとサーバーに負荷をかけるので、20〜30分を推奨。
CHECK_INTERVAL_MINUTES = 20

# 定期診断通知（ハートビート）: 空きがなくても定期的に「動いてます、まだ×です」
# を1通にまとめて送る間隔（時間）。GitHub Actionsのスケジュールは正確ではないため、
# 実際には設定値より間延びする（例: 3時間設定→実態6時間程度）ことを想定済み。
HEARTBEAT_INTERVAL_HOURS = 3

# ベイズ推定用の設定
# 実際のチェック間隔（時間）。GitHub Actionsのcron設定と合わせる。
NOMINAL_CHECK_INTERVAL_HOURS = 1
# μ(空きの継続時間)がまだ1件も実測できていないときに使う仮定値(時間)。
# 「早押し合戦で一瞬で消える」想定の保守的な値。
ASSUMED_WINDOW_HOURS_IF_UNKNOWN = 0.25


# ============================================================
# 通知処理
# ============================================================

def notify(message: str, method: str) -> None:
    print(f"[NOTIFY via {method}] {message}")
    try:
        if method == "ntfy":
            req = urllib.request.Request(
                url=f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": "伊東園ホテルズ 空室あり".encode("utf-8")},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        elif method == "discord" and DISCORD_WEBHOOK_URL:
            payload = json.dumps({"content": message}).encode("utf-8")
            req = urllib.request.Request(
                url=DISCORD_WEBHOOK_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        elif method == "email":
            if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
                print("[NOTIFY] GMAIL_ADDRESS / GMAIL_APP_PASSWORD が未設定です。"
                      "環境変数(ローカルなら.env等、GitHub ActionsならSecrets)"
                      "を確認してください。")
                return
            msg = MIMEText(message)
            msg["Subject"] = "伊東園ホテルズ 空室あり"
            msg["From"] = GMAIL_ADDRESS
            msg["To"] = EMAIL_TO
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_ADDRESS, [EMAIL_TO], msg.as_string())
        else:
            print(f"通知方法「{method}」が正しく設定されていません。CONFIGを確認してください。")
    except Exception as e:
        print(f"通知の送信に失敗しました: {e}")


# ============================================================
# 状態の保存・読み込み
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# スクレイピング本体
# ============================================================

@dataclass
class CheckResult:
    hotel: str
    date: str
    available: bool
    raw_mark: str
    screenshot: str


def run_search_for_hotel(page, hotel_name: str, date_str: str) -> CheckResult:
    page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)

    y, m, d = date_str.split("-")
    m, d = str(int(m)), str(int(d))  # 先頭ゼロを落とす（"07"→"7"など、表記ゆれ対策）

    try:
        # --- 宿泊日（実体はテキスト入力欄。datepicker(thickbox)がついているが
        #     .fill()で直接値をセットすればポップアップは開かず干渉しない）---
        page.fill("#s_year", y)
        page.fill("#s_month", m)
        page.fill("#s_day", d)

        # --- エリアまたは施設（表示テキストに全角スペースが入っているため
        #     labelではなくvalue属性で選択する）---
        # --- エリアまたは施設（表示テキストに全角/半角スペースが混ざることが
        #     あるため、空白を除いた文字列で比較して一致するoptionのvalueを
        #     その場で拾う。これでvalue属性を事前に調べておく必要がなくなる）---
        target_norm = hotel_name.replace(" ", "").replace("\u3000", "").strip()
        options = page.eval_on_selector_all(
            "select[name='area_yado_id'] option",
            "els => els.map(el => ({value: el.value, text: el.textContent}))",
        )
        hotel_value = None
        for opt in options:
            opt_norm = (opt["text"] or "").replace(" ", "").replace("\u3000", "").strip()
            if opt_norm == target_norm:
                hotel_value = opt["value"]
                break
        if not hotel_value:
            raise ValueError(
                f"プルダウンに「{hotel_name}」に一致する施設が見つかりません。"
                f"表記が違う可能性があるので、TARGET_HOTELSの名前を"
                f"サイト上の表示と見比べてください。"
            )
        page.select_option("select[name='area_yado_id']", value=hotel_value)

        # --- 人数・泊数・部屋数 ---
        page.select_option("select[name='obj_per_num']", value=str(ADULTS_PER_ROOM))
        page.select_option("select[name='obj_stay_num']", value=str(NIGHTS))
        page.select_option("select[name='obj_room_num']", value=str(ROOMS))
    except Exception as e:
        # うまく選べなかった場合は、原因調査用にその時点の画面とHTMLを保存する
        safe_name = re.sub(r"[^\w]", "_", hotel_name)
        debug_png = SHOT_DIR / f"DEBUG_{safe_name}.png"
        debug_html = SHOT_DIR / f"DEBUG_{safe_name}.html"
        try:
            page.screenshot(path=str(debug_png), full_page=True)
            debug_html.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            f"フォーム入力に失敗しました。{debug_png} と {debug_html} を確認してください。"
            f" 元のエラー: {e}"
        )

    # --- 検索実行 ---
    # 実際のボタンは <input type="button" value="この条件で空室状況を表示" ...>
    search_button = page.locator("input[value='この条件で空室状況を表示']").first
    search_button.click()
    page.wait_for_load_state("networkidle", timeout=30000)

    # --- 結果を保存・判定 ---
    # 実際のページ構造を確認済み: 結果テーブルは id="ypro_stock_calendar" で、
    # 検索した宿泊日は必ず一番左のデータ列(id="ypro_stock_calendar0_0")に来る。
    # なので該当セルをピンポイントで読めば確実（正規表現による曖昧な推測は不要）。
    safe_name = re.sub(r"[^\w]", "_", hotel_name)
    shot_path = SHOT_DIR / f"{safe_name}_{date_str}.png"
    html_path = SHOT_DIR / f"{safe_name}_{date_str}.html"
    page.screenshot(path=str(shot_path), full_page=True)
    html_path.write_text(page.content(), encoding="utf-8")

    target_cell = page.locator("#ypro_stock_calendar0_0")

    if target_cell.count() == 0:
        # 万一テーブル構造が変わっていた場合の保険として、
        # 従来の正規表現ベースの推測にフォールバックする
        body_text = page.locator("body").inner_text()
        day_int = int(d)
        pattern = re.compile(rf"{day_int}\D{{0,6}}([○◎△×－―]|満室)")
        match = pattern.search(body_text)
        mark = match.group(1) if match else "不明"
    else:
        mark = target_cell.inner_text().strip()

    is_available = mark in AVAILABLE_MARKS
    return CheckResult(hotel=hotel_name, date=date_str, available=is_available,
                        raw_mark=mark, screenshot=str(shot_path))


def update_stats_and_report(state: dict, combined_available: bool) -> str:
    """
    今回のチェック結果(combined_available)を反映してλ・μの推定を更新し、
    レポート文字列を返す。state は呼び出し側で save_state() される想定。
    """
    now = datetime.now()

    if "_stats_log_first_ts" not in state:
        # 初回のみ: これまでチャットで手計算していた実績
        # (約8.2日の観測期間中に1件の発生を確認済み)を初期値としてシードする。
        # ゼロから再スタートさせず、これまでの実績を引き継ぐための措置。
        seed_days_ago = now - timedelta(days=8.2)
        state["_stats_log_first_ts"] = seed_days_ago.isoformat()
        state["_stats_arrivals"] = 1

    prev_combined = state.get("_stats_last_combined_available", False)
    arrivals = state.get("_stats_arrivals", 0)
    open_windows = state.get("_stats_open_windows", [])

    if combined_available and not prev_combined:
        # 満室 → 空き への遷移。新しい「発生」を1件カウントし、
        # 窓の開始時刻を記録しておく（後で閉じた時に継続時間を測るため）
        arrivals += 1
        state["_stats_available_since_ts"] = now.isoformat()

    if (not combined_available) and prev_combined:
        # 空き → 満室 への遷移。窓が閉じたので、継続時間(時間)を記録する
        since_ts_str = state.get("_stats_available_since_ts")
        if since_ts_str:
            since_ts = datetime.fromisoformat(since_ts_str)
            duration_hours = (now - since_ts).total_seconds() / 3600
            open_windows.append(duration_hours)

    state["_stats_last_combined_available"] = combined_available
    state["_stats_arrivals"] = arrivals
    state["_stats_open_windows"] = open_windows

    first_ts = datetime.fromisoformat(state["_stats_log_first_ts"])
    observed_days = (now - first_ts).total_seconds() / 86400

    target_dt = datetime.strptime(TARGET_DATE, "%Y-%m-%d")
    remaining_days = (target_dt - now).total_seconds() / 86400

    report = stats.build_report(
        arrivals=arrivals,
        observed_days=observed_days,
        open_window_hours=open_windows,
        check_interval_hours=NOMINAL_CHECK_INTERVAL_HOURS,
        remaining_days=remaining_days,
        assumed_window_hours_if_unknown=ASSUMED_WINDOW_HOURS_IF_UNKNOWN,
    )
    return stats.format_report_ja(report, remaining_days, TARGET_DATE)


def run_once() -> None:
    state = load_state()
    changed_any = False
    results_summary = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP")

        any_available_this_run = False

        for hotel_name in TARGET_HOTELS:
            key = f"{hotel_name}_{TARGET_DATE}"
            try:
                result = run_search_for_hotel(page, hotel_name, TARGET_DATE)
            except Exception as e:
                print(f"[ERROR] {hotel_name} のチェック中にエラー: {e}")
                results_summary.append(f"{hotel_name}: チェック失敗（{e}）")
                continue

            prev_available = state.get(key, {}).get("available", False)
            print(f"{hotel_name} / {TARGET_DATE}: 記号={result.raw_mark} "
                  f"空きあり={result.available} (前回: {prev_available}) "
                  f"[{result.screenshot}]")
            results_summary.append(f"{hotel_name}: {result.raw_mark}"
                                    f"（{'空きあり' if result.available else '空きなし'}）")

            if result.available:
                any_available_this_run = True

            if result.available and not prev_available:
                notify(
                    f"{hotel_name} {TARGET_DATE} 大人{ADULTS_PER_ROOM}名×{ROOMS}部屋"
                    f" の空室が見つかりました！（記号: {result.raw_mark}）\n{SEARCH_URL}",
                    method=IMMEDIATE_NOTIFY_METHOD,
                )
                changed_any = True

            state[key] = asdict(result)

        browser.close()

    # --- ベイズ推定の更新（全施設合算で「どこか1つでも空いていたか」を見る）---
    stats_report_text = update_stats_and_report(state, any_available_this_run)
    print(stats_report_text)

    # --- 定期診断通知（ハートビート） ---
    # 空きの有無にかかわらず、一定時間おきに「監視は生きています」を
    # TARGET_HOTELS全施設まとめて1通で知らせる。
    last_heartbeat_str = state.get("_last_heartbeat")
    now = datetime.now()
    should_send_heartbeat = True
    if last_heartbeat_str:
        last_heartbeat = datetime.fromisoformat(last_heartbeat_str)
        elapsed_hours = (now - last_heartbeat).total_seconds() / 3600
        should_send_heartbeat = elapsed_hours >= HEARTBEAT_INTERVAL_HOURS

    if should_send_heartbeat:
        summary_text = "\n".join(results_summary)
        notify(
            f"【定期診断】{TARGET_DATE} の空室監視レポート\n"
            f"{summary_text}\n\n"
            f"{stats_report_text}\n\n"
            f"（{HEARTBEAT_INTERVAL_HOURS}時間おき設定のハートビート通知です。"
            f"空きが出た場合は別途、変化があった瞬間に通知します）",
            method=HEARTBEAT_NOTIFY_METHOD,
        )
        state["_last_heartbeat"] = now.isoformat()

    save_state(state)

    if not changed_any:
        print("変化なし。")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        print(f"連続実行モード開始（{CHECK_INTERVAL_MINUTES}分おきにチェックします。"
              f"止めるにはCtrl+Cを押してください）")
        while True:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n=== {now} チェック開始 ===")
            try:
                run_once()
            except Exception as e:
                # 1回のエラーでループ自体は止めない
                # （サイト側の一時的な不調やネットワーク瞬断を想定）
                print(f"[ERROR] チェック中に例外が発生しましたが、ループは継続します: {e}")
            print(f"次回チェックまで{CHECK_INTERVAL_MINUTES}分待機します。")
            time.sleep(CHECK_INTERVAL_MINUTES * 60)
    else:
        run_once()

