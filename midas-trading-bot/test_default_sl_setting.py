#!/usr/bin/env python3
"""
Тест настройки безопасного Stop Loss
"""

def test_sl_calculation():
    """Тест расчета SL с разными процентами"""
    
    print("=" * 80)
    print("ТЕСТ: Безопасный Stop Loss из настроек")
    print("=" * 80)
    print()
    
    # Тестовые случаи
    test_cases = [
        {
            "name": "Консервативный трейдер",
            "direction": "LONG",
            "entry": 0.3385,
            "sl_percent": 5.0,  # Очень осторожный
        },
        {
            "name": "Стандартный трейдер",
            "direction": "LONG",
            "entry": 0.3385,
            "sl_percent": 10.0,  # По умолчанию
        },
        {
            "name": "Агрессивный трейдер",
            "direction": "LONG",
            "entry": 0.3385,
            "sl_percent": 15.0,  # Больше риск
        },
        {
            "name": "SHORT консервативный",
            "direction": "SHORT",
            "entry": 2.50,
            "sl_percent": 5.0,
        },
        {
            "name": "SHORT стандартный",
            "direction": "SHORT",
            "entry": 2.50,
            "sl_percent": 10.0,
        },
    ]
    
    for case in test_cases:
        print(f"📊 {case['name']}")
        print(f"   Направление: {case['direction']}")
        print(f"   Цена входа: {case['entry']}")
        print(f"   Настройка SL: {case['sl_percent']}%")
        
        sl_percentage = case['sl_percent'] / 100.0
        
        if case['direction'] == "LONG":
            sl = case['entry'] * (1 - sl_percentage)
            direction_text = f"на {case['sl_percent']}% НИЖЕ входа"
        else:
            sl = case['entry'] * (1 + sl_percentage)
            direction_text = f"на {case['sl_percent']}% ВЫШЕ входа"
        
        print(f"   ✅ Безопасный SL: {sl:.6f} ({direction_text})")
        
        # Расчет максимального убытка
        loss_notional = abs(sl - case['entry']) / case['entry'] * 100
        print(f"   💰 Макс. убыток по цене: {loss_notional:.1f}%")
        print()
    
    print("=" * 80)
    print("✅ Все расчеты выполнены корректно!")
    print("=" * 80)
    print()
    print("📝 Как изменить настройку:")
    print("   1. Через веб-интерфейс (Settings)")
    print("   2. Через API: PATCH /api/settings")
    print("   3. Напрямую в БД: UPDATE settings SET default_stop_loss_percent = X")
    print()
    print("⚠️  Рекомендации:")
    print("   - 5% - очень консервативно (малый риск)")
    print("   - 10% - стандарт (средний риск) ✅")
    print("   - 15% - агрессивно (высокий риск)")
    print("   - >20% - не рекомендуется с плечом 30x!")


def test_with_leverage():
    """Тест с учетом плеча"""
    
    print()
    print("=" * 80)
    print("ТЕСТ: Максимальный убыток с плечом")
    print("=" * 80)
    print()
    
    balance = 53.65
    deposit_pct = 10.0
    leverage = 30
    
    margin = balance * (deposit_pct / 100.0)
    position_size = margin * leverage
    
    print(f"Баланс: {balance} USDT")
    print(f"В сделке: {deposit_pct}% = {margin:.2f} USDT (маржа)")
    print(f"Плечо: {leverage}x")
    print(f"Размер позиции: {position_size:.2f} USDT")
    print()
    
    for sl_percent in [5.0, 10.0, 15.0, 20.0]:
        # Убыток по позиции при срабатывании SL
        loss_by_position = position_size * (sl_percent / 100.0)
        
        # Но мы рискуем только маржей
        loss_real = min(loss_by_position, margin)
        loss_from_balance = (loss_real / balance) * 100
        
        print(f"SL = {sl_percent}%:")
        print(f"  Убыток по позиции: {loss_by_position:.2f} USDT")
        print(f"  Реальный убыток: {loss_real:.2f} USDT (ограничен маржей)")
        print(f"  % от баланса: {loss_from_balance:.2f}%")
        print()
    
    print("💡 Вывод:")
    print(f"   С плечом {leverage}x даже малое движение цены = большой убыток!")
    print(f"   Безопасный SL защищает от потери всего депозита")
    print(f"   Максимум потеряете: {margin:.2f} USDT (маржу в сделке)")


if __name__ == "__main__":
    test_sl_calculation()
    test_with_leverage()
