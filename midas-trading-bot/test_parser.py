from app.services.signal_parser import parse_signal

test_signal = """
**#SAND**** SHORT **🔽**

Вход : Рынок ( 0.11411 **) 
**
**✔️**Тейк - 0.11296
**✔️**Тейк - 0.11144
**✔️**Тейк - 0.10692

****❌****Cтоп: 0.12039
"""

def test():
    print("Testing Signal Parsing...")
    result = parse_signal(test_signal)
    if result:
        print("✅ SUCCESS!")
        print(f"Symbol: {result['symbol']}")
        print(f"Side: {result['side']}")
        print(f"Entry: {result['entry_price']}")
        print(f"TP1: {result['tp1']}")
        print(f"SL: {result['sl']}")
    else:
        print("❌ FAILED!")

if __name__ == "__main__":
    test()
