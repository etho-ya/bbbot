#!/usr/bin/env python3
"""
Тест парсинга 4 форматов сигналов
"""
import sys
sys.path.insert(0, '/opt/trading-bot/trading-bot')

from app.services.signal_parser import parse_signal

# 4 реальных сигнала из Telegram
signals = [
    # Формат 1: WIF
    """Монета: WIF LONG Х25 

🔵Цена входа: 0.3385

✅Тэйки: 0.3417 0.3453 0.3693

🛑Стоп: 0.3229

Входим на 35$
🏦Банк: 354.8$""",
    
    # Формат 2: ATOM
    """ATOM/USDT long на 900$ в рамках марафона 📈

Остаток депозита: 24536$

Вход: по рынку, Добор: ниже 2.2653

Тейки: 2.3365 2.3595 2.5470""",
    
    # Формат 3: ONDO
    """ONDO LONG 🔼

➡️Твх - 0.3428

✔️Тейк 1 - 0.3445
✔️Тейк 2 - 0.3464
✔️Тейк 3 - 0.3481

🚩Стоп-лос ставим соблюдая ваш риск-менеджмент.""",
    
    # Формат 4: MAGIC
    """#MAGIC LONG 🔼

Вход : Рынок ( 0.0925 ) 

✔️Тейк - 0.0935
✔️Тейк - 0.0948
✔️Тейк - 0.0986

❌Cтоп: 0.0873

Маржа: 400$
Банк: 4 961.73$"""
]

signal_names = ["WIF (Формат 1)", "ATOM (Формат 2)", "ONDO (Формат 3)", "MAGIC (Формат 4)"]

print("=" * 80)
print("ТЕСТ ПАРСИНГА 4 ФОРМАТОВ СИГНАЛОВ")
print("=" * 80)

for i, (name, signal_text) in enumerate(zip(signal_names, signals), 1):
    print(f"\n{'='*80}")
    print(f"📊 СИГНАЛ {i}: {name}")
    print(f"{'='*80}")
    print(f"Текст:\n{signal_text[:100]}...")
    print(f"\n{'─'*80}")
    
    result = parse_signal(signal_text)
    
    if result:
        print(f"✅ УСПЕШНО РАСПАРСЕН")
        print(f"\n📋 Результат:")
        print(f"  Символ:     {result['symbol']}")
        print(f"  Направление: {result['direction']} ({result['side']})")
        print(f"  Цена входа:  {result['entry_price']}")
        print(f"  TP1:         {result['tp1']}")
        print(f"  TP2:         {result['tp2']}")
        print(f"  TP3:         {result['tp3']}")
        print(f"  SL:          {result['sl']}")
        print(f"  Плечо:       {result['leverage']}x")
    else:
        print(f"❌ НЕ УДАЛОСЬ РАСПАРСИТЬ")
        print(f"\n⚠️  ПРОБЛЕМА: Парсер не смог извлечь данные из этого формата!")

print(f"\n{'='*80}")
print("📊 ИТОГИ:")
print(f"{'='*80}")

success_count = sum(1 for signal in signals if parse_signal(signal) is not None)
total_count = len(signals)

print(f"\n✅ Успешно: {success_count}/{total_count}")
print(f"❌ Ошибок:  {total_count - success_count}/{total_count}")

if success_count == total_count:
    print(f"\n🎉 ВСЕ {total_count} ФОРМАТА ПАРСЯТСЯ КОРРЕКТНО!")
else:
    print(f"\n⚠️  {total_count - success_count} формат(ов) НЕ РАБОТАЮТ - требуется доработка парсера!")

print(f"\n{'='*80}")
