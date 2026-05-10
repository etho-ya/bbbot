"""Quick test for compound_growth.py"""
from compound_growth import CompoundGrowthEngine

cg = CompoundGrowthEngine(risk_pct=0.02, dd_threshold=0.10, min_position_usd=0.1)

# T1: Ramp-up mode (first 10 trades → half risk)
r = cg.calculate_position_size(26.0)
assert r["mode"] == "ramp_up", f'Expected ramp_up, got {r["mode"]}'
assert r["size_usd"] == 0.26, f'Expected 0.26, got {r["size_usd"]}'  # 26 * 0.02 * 0.5
print(f'T1 PASS: ramp_up $26 -> ${r["size_usd"]}')

# Graduate from ramp-up
for _ in range(10):
    cg.record_trade()

# T2: Normal mode
r = cg.calculate_position_size(26.0)
assert r["mode"] == "normal", f'Expected normal, got {r["mode"]}'
assert r["size_usd"] == 0.52, f'Expected 0.52, got {r["size_usd"]}'  # 26 * 0.02
print(f'T2 PASS: normal $26 -> ${r["size_usd"]}')

# T3: Balance grew to $100 → compound effect  
r = cg.calculate_position_size(100.0)
assert r["size_usd"] == 2.0, f'Expected 2.0, got {r["size_usd"]}'  # 100 * 0.02
print(f'T3 PASS: compound $100 -> ${r["size_usd"]}')

# T4: Drawdown! 80 from peak 100 = 20% DD → protective mode
r = cg.calculate_position_size(80.0)
assert r["mode"] == "protective", f'Expected protective, got {r["mode"]}'
assert r["drawdown_pct"] == 0.2, f'Expected 0.2 DD, got {r["drawdown_pct"]}'
assert r["size_usd"] == 0.4, f'Expected 0.4, got {r["size_usd"]}'  # 80 * 0.02 * 0.25
print(f'T4 PASS: DD=20% $80 -> ${r["size_usd"]}, mode={r["mode"]}')

# T5: Verify hard cap works (huge balance)
r = cg.calculate_position_size(10000.0)
assert r["size_usd"] == 200.0, f'Expected 200 (2% of 10K), got {r["size_usd"]}'  # 10K * 0.02
print(f'T5 PASS: $10K -> ${r["size_usd"]}')

# T6: Hard cap trigger (risk > max_position_pct would never happen at 2%, but verify cap exists)
cg2 = CompoundGrowthEngine(risk_pct=0.10, max_position_pct=0.05, min_position_usd=0.1)
for _ in range(10): cg2.record_trade()
r = cg2.calculate_position_size(100.0)
assert r["size_usd"] == 5.0, f'Expected 5.0 (cap at 5%), got {r["size_usd"]}'  # min(10, 5) = 5
print(f'T6 PASS: hard cap 5% of $100 -> ${r["size_usd"]}')

# T7: Shadow report
shadow = cg.get_shadow_report(26.0, 5.0)
assert "COMPOUND SHADOW" in shadow
print(f'T7 PASS: shadow report')

print('\nAll 7 tests passed!')
