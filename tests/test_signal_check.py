import pandas as pd
import pytest

from app.delivery import signal_check as sc

T0 = pd.Timestamp("2026-10-20 12:00", tz="UTC")


def _bars(rows, start=T0, step="15min"):
    """rows: [(open, high, low, close), ...] — бары подряд, первый открывается в start."""
    dt = pd.date_range(start, periods=len(rows), freq=step)
    return pd.DataFrame([dict(dt=d, open=o, high=h, low=lo, close=c)
                         for d, (o, h, lo, c) in zip(dt, rows)])


# --- разбор текста ---

def test_parse_typical_channel_post():
    txt = """🔥 #BTC/USDT LONG
Плечо: x10 (cross)
Вход: 64 200 - 64 500
Стоп: 63 500
🎯 TP1: 65 300
🎯 TP2: 66 000
🎯 TP3: 67 000"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("BTC", "LONG", 64200, 64500, 63500)
    assert s.targets == [65300, 66000, 67000]
    assert s.symbol == "BTC/USDT:USDT"


def test_parse_english_short_with_list_targets():
    txt = """SOLUSDT SHORT
Entry zone: 182.5-184
Targets: 178, 175.5, 170
Stop loss: 187.2
Leverage 20x"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("SOL", "SHORT", 182.5, 184, 187.2)
    assert s.targets == [178, 175.5, 170]


def test_parse_comma_decimals_and_keyword_on_own_line():
    txt = """#WLD шорт
Вход:
0,4520
Стоп:
0,4700
Цель: 0,4300"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.stop, s.targets) == ("WLD", "SHORT", 0.452, 0.47, [0.43])


def test_parse_market_entry():
    s = sc.parse_signal("ETH/USDT лонг по рынку\nSL 3100\nTP 3400")
    assert s.entry_lo is None and s.stop == 3100 and s.targets == [3400]


def test_breakeven_phrase_does_not_steal_next_line():
    txt = "#ADA LONG\nВход 0.50\nСтоп 0.48\nстоп в безубыток после TP1\nTP1 0.53\nTP2 0.56"
    s = sc.parse_signal(txt)
    assert s.stop == 0.48 and s.targets == [0.53, 0.56]


def test_tp_index_not_glued_to_price():
    s = sc.parse_signal("#DOGE LONG\nEntry 100\nSL 90\nTP1 110\nTP2 120")
    assert s.targets == [110, 120]


def test_parse_one_letter_ticker():
    s = sc.parse_signal("🟢 LONG S/USDT:USDT\nВход: 0.05555\nСтоп: 0.05404")
    assert s.coin == "S" and s.entry_lo == 0.05555


def test_parse_cornix_lists():
    txt = """#BTC/USDT
Exchange: Binance Futures
Signal Type: Regular (Long)
Leverage: Cross (20.0X)
Entry Targets:
1) 64000
2) 63500
Take-Profit Targets:
1) 65000
2) 66000
3) 67500
Stop Targets:
1) 62000"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("BTC", "LONG", 63500, 64000, 62000)
    assert s.targets == [65000, 66000, 67500]


def test_parse_numbered_targets_and_coin_label():
    txt = """📍Монета: ARB
Направление: Long 🟢
Плечо: 10x-20x
Точка входа: 0.3150 - 0.3100
🎯 Target 1: 0.3200
🎯 Target 2: 0.3260
❌ StopLoss: 0.3040"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("ARB", "LONG", 0.31, 0.315, 0.304)
    assert s.targets == [0.32, 0.326]


def test_parse_our_own_dm_format():
    txt = """🔔 СИГНАЛ #715 | 🔴 SHORT   NEAR/USDT:USDT   ·   1h
💹 Цена сейчас:  4.507
📍 Вход:  4.5915  (по касанию)
🛑 Стоп:  4.664  (ход до стопа 1.6%)
🔁 Трейлинг-стоп ордером на бирже: активация 4.48275 (+1.5R), отступ 0.0145 (0.2R ≈ 0.32%)
🎯 Тейки:
   • TP1: 4.374  (3.0R)
⚖️ R/R до TP1: 1:3.0
🔎 Уверенность: 51%"""
    s = sc.parse_signal(txt)
    assert (s.coin, s.side, s.entry_lo, s.stop, s.targets) == ("NEAR", "SHORT", 4.5915, 4.664,
                                                              [4.374])


# --- форматы цен VIP-каналов (30.10.2026: «81,500» читалось как 81.5) ---

@pytest.mark.parametrize("txt,entry,stop,targets", [
    ("#BTC/USDT LONG\nВход: 81 500\nСтоп: 80 900\nTP: 83 000",           # пробел — тысячи
     81500, 80900, [83000]),
    ("#BTC/USDT LONG\nВход: 81 500\nСтоп: 80 900\nTP: 83 000",   # неразрывный
     81500, 80900, [83000]),
    ("#ARB SHORT\nВход 0,0345\nСтоп 0,0360\nЦель 0,0320",                 # запятая — дробь
     0.0345, 0.036, [0.032]),
    ("#BTC LONG\nEntry 81.5k\nSL 80.9k\nTP 83k", 81500, 80900, [83000]),  # «k» — тысячи
    ("#BTC LONG\nВход 81,5к\nСтоп 80,9к\nTP 83К", 81500, 80900, [83000]),
    ("BTC/USDT LONG\nEntry $81,500.5\nSL $80,900\nTP $83,000",           # запятая + точка
     81500.5, 80900, [83000]),
    ("#BTC LONG\nВход 81.500,5\nСтоп 80.900\nЦель 83.000",               # европейская запись
     81500.5, 80900, [83000]),
    ("#BTC LONG\nВход 63000\nСтоп 62000\nЦели 64000,65000",              # склеенный список
     63000, 62000, [64000, 65000]),
    ("#XRP LONG\nВход 1.15\nСтоп 1.10\nЦели 1.2,1.3,1.4", 1.15, 1.1, [1.2, 1.3, 1.4]),
])
def test_parse_price_formats(txt, entry, stop, targets):
    s = sc.parse_signal(txt)
    assert (s.entry_lo, s.stop, s.targets) == (entry, stop, targets)
    assert not s.unsure


def test_ambiguous_comma_needs_market_to_decide():
    """«81,500» без других подсказок в посте: и 81 500, и 81.5 — оба прочтения сохраняем,
    выбирает цена рынка в момент поста; не подсказала и она — не угадываем."""
    s = sc.parse_signal("#BTC/USDT LONG\nВход: 81,500\nСтоп: 80,900\nTP: 83,000")
    assert s.unsure and (s.entry_lo, s.stop) == (81500, 80900)
    assert [(a.entry_lo, a.stop, a.targets) for a in s.alternatives] == [(81.5, 80.9, [83.0])]
    btc = sc.resolve_reading(s, _bars([(81600, 81700, 81400, 81500)]), T0)
    alt = sc.resolve_reading(s, _bars([(81.6, 81.7, 81.4, 81.5)]), T0)
    assert (btc.entry_lo, btc.targets) == (81500, [83000])
    assert (alt.entry_lo, alt.targets) == (81.5, [83.0])
    assert sc.resolve_reading(s, _bars([(5.0, 5.1, 4.9, 5.0)]), T0) is None    # ни одно
    assert sc.resolve_reading(s, _bars([]), T0) is None                         # нет свечей
    msg = sc.render_unsure(s)
    assert "81500" in msg and "81.5" in msg and "BTC" in msg


def test_post_style_decides_without_market():
    """Стиль поста — по однозначным числам: «1.5%» за точку -> «81,500» — тысячи; «0,5%» за
    запятую -> «2,345» — дробь. Тогда без свечей возвращаем основное прочтение."""
    us = sc.parse_signal("#BTC/USDT LONG\nВход: 81,500\nСтоп: 80,900\nРиск 1.5%")
    assert (us.entry_lo, us.unsure) == (81500, False)
    assert sc.resolve_reading(us, _bars([]), T0) is us
    ru = sc.parse_signal("#XRP/USDT LONG\nВход: 2,345\nСтоп: 2,210\nРиск 0,5%")
    assert (ru.entry_lo, ru.stop, ru.unsure) == (2.345, 2.21, False)


def test_dot_thousands_switched_by_market_only():
    """«81.500» по умолчанию — дробь (так пишет большинство каналов), но если рынок явно
    за тысячи, берём тысячи; без свечей — дробь, без отказа."""
    s = sc.parse_signal("#BTC LONG\nВход 81.500\nСтоп 80.900\nTP 83.000")
    assert (s.entry_lo, s.unsure) == (81.5, False)
    assert sc.resolve_reading(s, _bars([(81600, 81700, 81400, 81500)]), T0).entry_lo == 81500
    assert sc.resolve_reading(s, _bars([]), T0).entry_lo == 81.5
    xrp = sc.parse_signal("#XRP LONG\nВход 2.345\nСтоп 2.210\nTP 2.600")
    assert sc.resolve_reading(xrp, _bars([(2.35, 2.36, 2.33, 2.34)]), T0).entry_lo == 2.345


def test_resolve_without_alternatives_is_identity():
    s = sc.parse_signal("#DOGE LONG\nEntry 100\nSL 90\nTP1 110")
    assert not s.alternatives and sc.resolve_reading(s, _bars([]), T0) is s


@pytest.mark.parametrize("txt", [
    "",
    "Биткоин сегодня интересно себя ведёт",
    "#BTC LONG вход 64000",                      # нет стопа
    "#BTC LONG\nВход 64000\nСтоп 65000",         # стоп над входом лонга
    "BTC вход 64000 стоп 63000",                 # нет стороны
])
def test_parse_rejects(txt):
    assert sc.parse_signal(txt) is None


def test_wrong_side_targets_dropped():
    s = sc.parse_signal("#BTC LONG\nВход 100\nСтоп 90\nTP 95\nTP 110")
    assert s.targets == [110]


# --- англоязычные VIP-посты (30.10.2026): тысячи при точечных дробях, цели из абзаца разбора,
# «coin» внутри слова. Тексты синтетические — в форматах каналов, не копии постов ---

_VIP_SHORT = """🔴 AVAX/USDT SHORT
Entry: $31.40 - $31.90
🎯 TP1: $29.60 (R:R 1.6:1)
🎯 TP2: $28.10
🛑 SL: $33.20

📊 Why:
• Last week AVAX hit our TP2 $31 target and now retests that level from below.
• R:R by target → TP1: 1.6:1 · TP2: 2.4:1"""

_VIP_LONG = """🟢 DOT/USDT LONG
Entry: $4.30 - $4.38
TP1: $4.55
TP 2: $4.80
SL: $4.12

Recap: DOT LONG #41207 hit TP2 $4.62 yesterday — DOT was $4.48–$4.66 at the time."""


def test_vip_thousands_with_dot_decimals():
    """«62,800» при «+8.4%» в посте — тысячи (до 352b47a: 62 и 800, вход 62–800, стоп 59);
    запятая-дробь в посте без точек — по-прежнему дробь."""
    s = sc.parse_signal("#BTC/USDT LONG\nEntry Zone: 62,800 - 64,100\nTP1: 68,900 (+8.4%)\n"
                        "TP2: 72,300 (+13.9%)\nSL: 59,700")
    assert (s.entry_lo, s.entry_hi, s.stop, s.targets) == (62800, 64100, 59700, [68900, 72300])
    assert not s.unsure and sc.resolve_reading(s, _bars([]), T0) is s
    ru = sc.parse_signal("#WLD SHORT\nВход 0,4512\nСтоп 0,4690\nЦель 0,4310")
    assert (ru.entry_lo, ru.stop, ru.targets) == (0.4512, 0.469, [0.431])


def test_targets_only_from_numbered_tp_lines():
    """Есть «TP1: …» — цели только из таких строк: «hit our TP2 $31 target» и «DOT was
    $4.48–$4.66» из абзаца разбора — не цели, «R:R 1.6:1» — тоже."""
    s = sc.parse_signal(_VIP_SHORT)
    assert (s.coin, s.side, s.entry_lo, s.entry_hi, s.stop) == ("AVAX", "SHORT", 31.4, 31.9, 33.2)
    assert s.targets == [29.6, 28.1]                 # было [31, 29.6, 28.1, 1.0, …]
    assert sc.parse_signal(_VIP_LONG).targets == [4.55, 4.8]     # было [4.48, 4.55, 4.62, …]


def test_vip_short_check_does_not_count_analysis_target():
    """Итог проверки: TP1 = 31 из абзаца разбора засчитывался на первом откате — «взята цель,
    затем стоп»; до настоящей первой цели (29.60) цена не дошла."""
    bars = _bars([(31.5, 31.6, 31.3, 31.4), (31.4, 31.5, 30.8, 31.0), (31.0, 33.4, 30.9, 33.3)])
    r = sc.evaluate(sc.parse_signal(_VIP_SHORT), bars, T0)
    assert r.status == "stop" and r.hit == 0


def test_numbered_target_values():
    # значение — число сразу после двоеточия; соотношение, процент и R — не цель
    assert sc._numbered_targets(["TP1: 107 | TP2: 101", "TP3: 1.6:1", "TP4: 8.5%",
                                 "TP5: 3R", "Final tp: 95"]) == [107, 101, 95]
    assert sc._numbered_targets(["Final tp: 95", "TP: 90", "hit our TP2 $115"]) == []
    # строк «TP<n>:» нет — прежний разбор: список целей в строке «TP:» целиком
    s = sc.parse_signal("#ADA LONG\nВход 0.50\nСтоп 0.48\nTP: 0.53, 0.56, 0.60")
    assert s.targets == [0.53, 0.56, 0.6]


def test_final_tp_label_and_price_order():
    """«Final tp:» без номера рядом с «Tp 1:» — тоже цель; нумерация канала с опечаткой
    («TP2» дальше «TP3») — цели всё равно по цене: evaluate считает их подряд."""
    s = sc.parse_signal("#ETH/USDT SHORT\nEntry: 2450\nTp 1: 2380\nTp 2: 2300\nFinal tp: 2150\n"
                        "Sl: 2520")
    assert s.targets == [2380, 2300, 2150]
    s = sc.parse_signal("#OP LONG\nEntry: 1.20\nSL: 1.10\nTP1: 1.30\nTP2: 1.55\nTP3: 1.40")
    assert s.targets == [1.3, 1.4, 1.55]


def test_rr_ratio_is_not_a_price():
    assert sc._nums("TP 29.60 (R:R 1.6:1)") == [29.6]        # было [29.6, 1.0]
    assert sc._nums("TP 29.60 R:R 1.6:1") == [29.6]          # цена перед R:R — не «29.60 R»
    assert sc._nums("+1.5R 3R 1:3") == []


@pytest.mark.parametrize("txt,coin", [
    ("#SOL SHORT\nEntry 182.5 - 184\nSL 187.2\nTP1 178\nPrice source: CoinGecko", "SOL"),
    ("#ETH LONG\nEntry 3100\nSL 3000\nTP1 3300\nCoinbase premium flipped positive", "ETH"),
])
def test_coin_word_inside_name_is_not_a_label(txt, coin):
    assert sc.parse_signal(txt).coin == coin          # было GECKO и BASE


def test_coin_label_needs_word_and_separator():
    assert sc._coin("Bitcoin crashed overnight, shorts below 93000") is None    # было CRASHED
    assert sc._coin("pair trading is back") is None
    assert sc._coin("Coin: sol") == "SOL"             # после «:» — любой регистр
    assert sc._coin("Trading pair ARB") == "ARB"      # после пробела — тикер заглавными
    assert sc._coin("Пара - TON") == "TON"


# --- ReDoS: разбор чужого текста обязан быть линейным (аудит безопасности 30.10.2026) ---
# Каждый вход — ~4 КБ (потолок текста Telegram). ДО фикса: 0.35–5.5 с на ОДНО сообщение,
# причём синхронно на event loop бота (весь бот стоял); после — единицы миллисекунд.
_N = 4096
_ADVERSARIAL = {
    "число-простыня в строке стопа": "BTC/USDT LONG\nstop " + "1" * (_N - 30),
    "число-простыня строкой-значением": "BTC/USDT LONG\nstop 1\n" + "1" * (_N - 30),
    "число-простыня во входе": "BTC/USDT LONG\nentry " + "9" * (_N - 30) + "\nstop 1",
    "пробелы после числа (value-строка)": "BTC/USDT LONG\nstop 1\n1" + " " * (_N - 40) + "x",
    "пробелы после тикера (пара)": "AA" + " " * (_N - 10) + "x long",
    "пробелы после ключа (номер позиции)": "BTC/USDT LONG\nstop" + " " * (_N - 30) + "x",
    "пробелы после coin:": "SHORT coin" + " " * (_N - 20) + "!",
    "сверхдлинный текст": "BTC/USDT LONG\nentry 100\nstop 90\n" + "1" * (3 * _N),
    "запятые через цифру": "BTC/USDT LONG\nentry 100\nstop " + "1," * (_N // 2 - 20),
    "точки до «k»": "BTC/USDT LONG\nentry " + "1." * (_N // 2 - 20) + "5k\nstop 1",
    "группы тысяч": "BTC/USDT LONG\nstop 1\nentry " + "1,234" * (_N // 5 - 10) + ".5",
    "пробелы между TP и номером": "BTC/USDT LONG\nentry 100\nstop 90\nTP" + " " * (_N // 2 - 30)
                                  + "1" + " " * (_N // 2 - 30) + "x",
    "метки TP1: подряд": "BTC/USDT LONG\nentry 100\nstop 90\n" + "TP1: 1:1 " * (_N // 9 - 5),
    "соотношения с дробью": "BTC/USDT LONG\nentry 100\nstop 90\nTP " + "1.5:" * (_N // 4 - 10),
    "пробелы вокруг «-» после coin": "SHORT coin" + " " * (_N // 2 - 20) + "-"
                                     + " " * (_N // 2 - 20) + "!",
    "цифры перед R:R": "BTC/USDT LONG\nentry 100\nstop 90\nTP " + "1" * (_N - 50) + " R:R",
}


@pytest.mark.parametrize("name", list(_ADVERSARIAL))
def test_parse_is_fast_on_adversarial_input(name):
    import time
    txt = _ADVERSARIAL[name]
    best = float("inf")
    for _ in range(3):                      # минимум из трёх — без шума GC/загрузки машины
        t0 = time.perf_counter()
        sc.parse_signal(txt)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.2, f"{name}: {best * 1000:.0f} мс — квадратичный regex вернулся?"


def test_regex_rewrite_keeps_semantics():
    """Линейные шаблоны распознают ровно то же, что прежние (дифференциальный фаззинг
    90 тыс. текстов при фиксе — 0 расхождений); здесь — закреплённые крайние случаи."""
    # проценты, R-мультипликаторы, соотношения и плечо — не цены
    assert sc._nums("TP 65300 (50%) +1.5R 1:3.5 x10 20x 64000") == [65300.0, 64000.0]
    assert sc._nums("5-3R 7") == [5.0, 7.0]
    assert sc._nums("1.2.3% 4") == [1.0, 4.0]
    # тикер: через дефис с пробелами, слитно, через слэш, по ключевому слову
    assert sc._coin("ETH - USDT long") == "ETH"
    assert sc._coin("SOLUSDT short") == "SOL"
    assert sc._coin("S / USDT long") == "S"
    assert sc._coin("монета - ADA шорт") == "ADA"
    # номер позиции после ключевого слова и строка-значение со скобкой
    assert sc._INDEX.sub(" ", " № 2) 1.55") == "  1.55"
    assert sc._VALUE_LINE.fullmatch("• 0.3150 - 0.3100 (3.0R)  ")
    assert not sc._VALUE_LINE.fullmatch("1 x")


# --- прогон по барам ---

LONG = sc.ParsedSignal("BTC", "LONG", 100.0, 100.0, 90.0, [110.0, 120.0])


def test_pullback_fill_then_all_targets():
    bars = _bars([(105, 106, 99, 101), (101, 111, 100, 110), (110, 121, 109, 120)])
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "all_tp" and r.hit == 2 and r.fill_price == 100.0
    assert r.mfe_r == pytest.approx(2.1)


def test_tp1_then_stop_counts_both():
    bars = _bars([(101, 101, 99, 100), (100, 112, 100, 111), (111, 111, 89, 90)])
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "stop" and r.hit == 1 and not r.ambiguous


def test_stop_and_target_same_bar_is_stop_first():
    bars = _bars([(101, 101, 99, 100), (100, 115, 85, 100)])
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "stop" and r.hit == 0 and r.ambiguous


def test_fill_bar_only_counts_stop_not_targets():
    bars = _bars([(101, 125, 99, 120)])          # в баре филла задета и цель — не засчитана
    r = sc.evaluate(LONG, bars, T0)
    assert r.status == "open" and r.hit == 0


def test_no_fill_when_price_never_comes():
    bars = _bars([(105, 108, 102, 107), (107, 115, 104, 114)])
    assert sc.evaluate(LONG, bars, T0).status == "no_fill"


def test_bars_before_post_ignored():
    bars = _bars([(101, 101, 85, 95), (105, 106, 101, 105)], start=T0 - pd.Timedelta("15min"))
    assert sc.evaluate(LONG, bars, T0).status == "no_fill"   # стоп-бар был ДО поста


def test_breakout_long_from_below():
    s = sc.ParsedSignal("BTC", "LONG", 100.0, 100.0, 95.0, [110.0])
    bars = _bars([(97, 99, 96, 98), (98, 101, 97, 100.5), (100.5, 111, 100, 110)])
    r = sc.evaluate(s, bars, T0)
    assert r.fill_price == 100.0 and r.status == "all_tp"


def test_breakout_long_invalidated_by_stop_first():
    s = sc.ParsedSignal("BTC", "LONG", 100.0, 100.0, 95.0, [110.0])
    bars = _bars([(97, 98, 94, 95), (95, 111, 95, 110)])
    assert sc.evaluate(s, bars, T0).status == "no_fill"


def test_short_limit_from_below_and_stop():
    s = sc.ParsedSignal("SOL", "SHORT", 182.5, 184.0, 187.2, [178.0])
    bars = _bars([(180, 183, 179, 182), (182, 188, 181, 187.5)])
    r = sc.evaluate(s, bars, T0)
    assert r.fill_price == 182.5 and r.status == "stop"


def test_market_entry_fills_first_open():
    s = sc.ParsedSignal("ETH", "LONG", None, None, 3100.0, [3400.0])
    bars = _bars([(3200, 3210, 3190, 3205), (3205, 3401, 3200, 3390)])
    r = sc.evaluate(s, bars, T0)
    assert r.fill_price == 3200 and r.status == "all_tp"


def test_pick_timeframe():
    assert sc.pick_timeframe(3600) == "15m"
    assert sc.pick_timeframe(20 * 86400) == "1h"
    assert sc.pick_timeframe(100 * 86400) == "4h"
    assert sc.pick_timeframe(200 * 86400) is None


def test_render_contains_verdict_and_cta():
    bars = _bars([(101, 101, 99, 100), (100, 112, 100, 111), (111, 111, 89, 90)])
    r = sc.evaluate(LONG, bars, T0)
    txt = sc.render_check(LONG, r, T0, "15m")
    assert "TP1" in txt and "✅" in txt and "в стоп" in txt
    assert "/trackrecord" in txt
    nf = sc.render_check(LONG, sc.CheckResult("no_fill"), T0, "15m")
    assert "не исполнился" in nf
