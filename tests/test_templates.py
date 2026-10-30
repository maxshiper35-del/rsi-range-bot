"""Тесты единого HTML-рендерера постов (app/delivery/templates.py)."""
from app.core.signal_engine import Side, Signal
from app.delivery import templates


def _sig(**kw):
    base = dict(symbol="BTC/USDT:USDT", side=Side.SHORT, timeframe="15m", entry=62340.0,
                stop=62980.0, take_profits=[60893.0, 59000.0], risk_reward=2.26,
                confidence=0.65, reasons=["свип хая", "слом CHOCH"],
                created_at="2026-01-01T00:00:00+00:00", indicators_snapshot={"setup": "6226"})
    base.update(kw)
    return Signal(**base)


def _closed(**kw):
    base = {"id": 5, "symbol": "ETH/USDT:USDT", "side": "LONG", "timeframe": "1h",
            "status": "tp1", "result_r": 2.26, "shadow": False, "setup": "1:1",
            "entry": 100.0, "exit_price": 101.0, "price_move_pct": 1.0,
            "filled_at": "2026-01-01T01:00:00+00:00"}
    base.update(kw)
    return base


def test_price_full_precision():
    # {:g} режет BTC>100k до 6 значащих цифр и даёт экспоненту на мелких альтах —
    # _price обязан отдавать полную запись
    assert templates._price(104250.55) == "104250.55"
    assert templates._price(0.00012345) == "0.00012345"
    assert templates._price(67450.0) == "67450"


def test_render_signal_channel_html():
    txt = templates.render_signal_channel(_sig(), 7)
    assert "<b>RSI RANGE — Сигнал #7</b>" in txt and "🔴 SHORT" in txt
    assert "<code>62340</code>" in txt
    assert "<code>60893</code> (2.3R)" in txt and "<code>59000</code> (5.2R)" in txt
    assert "(ход 1.0%)" in txt   # дистанция входа до стопа
    assert "<blockquote expandable>" in txt and "свип хая" in txt
    assert "Не финсовет" in txt and "@rsirange_bot" in txt


def test_render_signal_escapes_injection():
    sig = _sig(reasons=["<b>инъекция</b> & прочее"])
    for txt in (templates.render_signal_channel(sig, 1), templates.render_signal_dm(sig, 1)):
        assert "&lt;b&gt;инъекция&lt;/b&gt; &amp; прочее" in txt
        assert "<b>инъекция" not in txt


def test_render_signal_dm():
    txt = templates.render_signal_dm(_sig(confidence=0.8), 3)
    assert "<b>СИГНАЛ #3</b>" in txt
    assert "▰▰▰▰▰▰▰▰▱▱ 80%" in txt
    assert "TP1: <code>60893</code>" in txt and "TP2: <code>59000</code>" in txt
    assert "<blockquote expandable>" in txt
    assert "Не финансовый совет" in txt


def test_render_teaser_hides_levels():
    txt = templates.render_teaser(_sig(), 9)
    assert "#9" in txt and "по подписке" in txt
    assert "62340" not in txt and "62980" not in txt   # ни входа, ни стопа



def test_render_teaser_open_channel_tells_truth():
    """Канал в full-режиме публикует уровни открыто — тизер не обещает их «по подписке»,
    а говорит, за что платят на самом деле, и уровни по-прежнему не раскрывает."""
    txt = templates.render_teaser(_sig(), 9, open_channel="@rsi_range")
    assert "в канале @rsi_range" in txt and "без лимита" in txt and "/subscribe" in txt
    assert "Вход, стоп, тейки и разбор — по подписке" not in txt
    assert "62340" not in txt and "62980" not in txt

def test_render_fill():
    txt = templates.render_fill(_closed(status="filled"), show_price=True)
    assert "вход исполнен" in txt and "#5" in txt and "<code>100</code>" in txt
    hidden = templates.render_fill(_closed(status="filled"), show_price=False)
    assert "100" not in hidden                          # teaser-канал: цену не раскрываем


def test_render_expired():
    txt = templates.render_expired(_closed(status="expired", result_r=None))
    assert "отменён" in txt and "сделки не было" in txt and "#5" in txt


def test_render_result_with_display_leverage():
    txt = templates.render_result(_closed(), display_leverage=10.0)
    assert "+2.26R" in txt
    assert "При плече ×10: <b>+10.0%</b>" in txt       # 1.0% × 10
    assert "движение цены +1.00%" in txt
    assert "✅" in txt and "TP1" in txt and "/stats" in txt


def test_render_result_leverage_off_and_stop():
    txt = templates.render_result(
        _closed(status="stopped", result_r=-1.0, price_move_pct=-0.8), display_leverage=0.0)
    assert "🛑" in txt and "-1.00R" in txt and "📉" in txt
    assert "плече" not in txt
    assert "Движение цены: -0.80%" in txt


def test_render_result_without_price_move():
    # старые записи без расширенных полей не роняют рендер
    c = _closed()
    del c["price_move_pct"]
    txt = templates.render_result(c, display_leverage=10.0)
    assert "+2.26R" in txt and "плече" not in txt


def test_render_milestone():
    st = {"trades": 25, "winrate": 0.56, "expectancy_r": 0.08, "total_r": 2.1}
    txt = templates.render_milestone(25, st)
    assert "Веха: 25 закрытых сделок" in txt and "56%" in txt and "+0.08R" in txt
    assert "/trackrecord" in txt and "Не финансовый совет" in txt


def test_trial_drip_texts():
    assert set(templates.TRIAL_DRIP) == {1, 2}                # 3 — render_trial_last_day
    assert "стоп" in templates.TRIAL_DRIP[1].lower()         # день 1: риск-дисциплина
    assert "/depo" in templates.TRIAL_DRIP[1] and "/notify" in templates.TRIAL_DRIP[1]
    assert "/trackrecord" in templates.TRIAL_DRIP[2]          # день 3: проверь сам
    assert "/check" in templates.TRIAL_DRIP[2] and "@rsi_range" in templates.TRIAL_DRIP[2]


def test_trial_last_day_says_what_stays_free_before_price():
    st = {"trades": 417, "winrate": 0.393, "total_r": 48.8}
    txt = templates.render_trial_last_day(7, st)
    assert "7 полных сигналов в неделю" in txt
    assert "417 закрытых сделок" in txt and "+48.8R" in txt and "минусы не прячем" in txt
    # порядок: сначала «что остаётся бесплатно», потом подписка
    assert txt.index("Бесплатно остаётся") < txt.index("/subscribe")
    assert "не за обещание прибыли" in txt                    # честность и в продаже
    assert "края не показал" not in txt
    no_free = templates.render_trial_last_day(0, {})
    assert "полных сигналов" not in no_free and "Журнал как есть" not in no_free


def test_trial_ended_personal_result_and_honest_sign():
    """К-24 (аудит текстов 30.10): личный итог в R и $, минус не прячем, правда о бесплатном."""
    up = templates.render_trial_ended({"signals": 23, "closed": 11, "total_r": 4.2}, 7)
    assert "Пробный доступ закончился" in up and "сигналов 23, закрылось 11" in up
    assert "<b>+4.2R</b>" in up and "≈ +42$ при риске $10" in up and "до комиссий" in up
    assert "7 полных сигналов в неделю" in up and "/check" in up and "минус" not in up
    down = templates.render_trial_ended({"signals": 9, "closed": 6, "total_r": -2.0}, 7,
                                        depo=(1000.0, 2.0))
    assert "<b>-2.0R</b>" in down and "≈ -40$ на депозит $1 000 при риске 2%" in down
    assert "вышли в минус" in down and "/stats" in down
    pending = templates.render_trial_ended({"signals": 3, "closed": 0, "total_r": 0.0}, 0)
    assert "закрытых пока нет" in pending and "полных сигналов" not in pending
    assert "R</b>" not in templates.render_trial_ended({"signals": 0, "closed": 0,
                                                        "total_r": 0.0}, 7)


def test_access_ended_says_what_stays_free():
    txt = templates.render_access_ended(7)
    assert "бесплатный режим: 7 полных сигналов в неделю и итоги всех сделок" in txt
    assert "полных сигналов" not in templates.render_access_ended(0)


def test_render_winback_has_promo_and_proof():
    stats = {"trades": 30, "winrate": 0.5, "expectancy_r": 0.1}
    txt = templates.render_winback("wb123", days=7, valid_days=5, stats=stats)
    assert "<code>wb123</code>" in txt and "+7 дн" in txt and "5 дн" in txt
    assert "/promo wb123" in txt
    assert "30 закрытых" in txt                             # соц-пруф присутствует
    assert "Не финсовет" in txt


def test_render_winback_escapes_code():
    txt = templates.render_winback("<b>x</b>", 7, 5, stats=None)
    assert "&lt;b&gt;x&lt;/b&gt;" in txt              # промокод из юзердаты экранирован


def test_render_weekly_digest():
    wk = {"trades": 5, "wins": 3, "losses": 2, "winrate": 0.6, "total_r": 2.4}
    st = {"trades": 40, "expectancy_r": 0.08, "total_r": 6.1, "open": 2}
    txt = templates.render_weekly_digest("01.06–07.06", wk, st)
    assert "01.06–07.06" in txt
    assert "Закрыто: <b>5</b>" in txt and "✅ 3 / 🛑 2" in txt
    assert "60%" in txt and "+2.4R" in txt
    assert "Открыто сейчас: 2" in txt
    assert "40 сделок" in txt and "+0.08R" in txt
    assert "/stats" in txt


def test_render_referral_reward():
    txt = templates.render_referral_reward(7)
    assert "+7 дн" in txt and "приглашени" in txt.lower()


def test_render_referral_invite():
    link = "https://t.me/rsirange_bot?start=ref_123"
    txt = templates.render_referral_invite(link, {"invited": 4, "rewarded": 2}, 7)
    assert f"<code>{link}</code>" in txt
    assert "Пригласили: <b>4</b>" in txt and "оплатили: <b>2</b>" in txt
    assert "+7 дн" in txt
    assert "не за регистрацию/триал" in txt          # честность: награда только за оплату


def test_render_notify_states():
    on = templates.render_notify({"muted": False, "quiet_start": None, "quiet_end": None})
    assert "включены" in on
    muted = templates.render_notify({"muted": True, "quiet_start": None, "quiet_end": None})
    assert "выключены" in muted
    quiet = templates.render_notify({"muted": False, "quiet_start": 22, "quiet_end": 6})
    assert "22:00–06:00 UTC" in quiet


def test_render_result_leverage_clamped_at_liquidation():
    """Аудит-2 математики [10]: «−120% депозита» физически невозможен — клэмп на −100%."""
    txt = templates.render_result(
        _closed(status="stopped", result_r=-1.0, price_move_pct=-3.0), display_leverage=40.0)
    assert "-100.0%" in txt and "-120" not in txt


def test_trail_line_published_matches_scoring_rule():
    """Правило трейлинга обязано быть В ПОСТЕ: журнал судит по нему, значит подписчик
    должен его видеть. Сигнал без трейлинга — строки нет (старая семантика)."""
    from app.core.signal_engine import Side, Signal
    from app.delivery import templates

    def _sig(side=Side.LONG, snap=None):
        return Signal(symbol="BTC/USDT:USDT", side=side, timeframe="1h",
                      entry=100.0, stop=90.0, take_profits=[130.0], risk_reward=3.0,
                      confidence=0.7, reasons=["тест"],
                      created_at="2026-01-01T00:00:00+00:00",
                      indicators_snapshot=snap if snap is not None else {"setup": "6226"})

    trail = {"setup": "6226", "trail": {"activate_r": 1.5, "distance_r": 0.5}}
    line = templates.trail_line(_sig(snap=trail))
    assert "115" in line and "+1.5R" in line and "0.5R" in line   # LONG: 100 + 1.5*10
    # параметры нативного трейлинг-ордера: отступ ценой 0.5*10=5 и % от активации 5/115
    assert "активация" in line and "отступ <code>5" in line and "4.35%" in line
    short = templates.trail_line(_sig(side=Side.SHORT, snap=trail))
    assert "85" in short                                           # SHORT: 100 - 1.5*10
    assert templates.trail_line(_sig()) == ""                      # без правила — пусто
    assert templates.trail_line(_sig(snap={"trail": {"activate_r": 0}})) == ""

    # строка реально попадает в оба поста
    assert "Трейлинг" in templates.render_signal_channel(_sig(snap=trail), 1)
    assert "Трейлинг" in templates.render_signal_dm(_sig(snap=trail), 1)
    assert "Трейлинг" not in templates.render_signal_dm(_sig(), 1)


def test_trail_only_exit_line_states_tp_is_reference():
    """В режиме «выход только по трейлингу» пост обязан сказать, что цель — ОРИЕНТИР:
    иначе подписчик закроется по тейку, а журнал оценит сделку по трейлингу."""
    from app.core.signal_engine import Side, Signal
    from app.delivery import templates
    sig = Signal(symbol="BTC/USDT:USDT", side=Side.LONG, timeframe="1h",
                 entry=100.0, stop=90.0, take_profits=[130.0], risk_reward=3.0,
                 confidence=0.7, reasons=["тест"], created_at="2026-01-01T00:00:00+00:00",
                 indicators_snapshot={"setup": "6226", "trail": {
                     "activate_r": 1.5, "distance_r": 0.3,
                     "tp_exit": False, "max_hold_hours": 168.0}})
    line = templates.trail_line(sig)
    assert "ориентир" in line and "НЕ закрываем" in line and "7 дн" in line
    # классический режим — строки про ориентир нет
    sig.indicators_snapshot["trail"]["tp_exit"] = True
    assert "ориентир" not in templates.trail_line(sig)


def test_weekly_digest_usd_line_for_user_deposit():
    wk = {"trades": 10, "wins": 4, "losses": 6, "winrate": 0.4, "total_r": 5.0}
    st = {"trades": 417, "open": 3, "expectancy_r": 0.12, "total_r": 48.8}
    txt = templates.render_weekly_digest("20.10–26.10", wk, st, depo=(1000.0, 1.0))
    assert "На ваш депозит $1 000 при риске 1%" in txt and "+50$" in txt
    neg = templates.render_weekly_digest("20.10–26.10", {**wk, "total_r": -3.0}, st,
                                         depo=(500.0, 2.0))
    assert "−" in neg or "-30$" in neg                    # минусовая неделя — минусом
    ask = templates.render_weekly_digest("20.10–26.10", wk, st, ask_depo=True)
    assert "/depo 500" in ask
    assert "/depo" not in templates.render_weekly_digest("20.10–26.10", wk, st)
    quiet = templates.render_weekly_digest("20.10–26.10", {**wk, "trades": 0}, st,
                                           depo=(1000.0, 1.0))
    assert "На ваш депозит" not in quiet                  # пустая неделя — без строки
