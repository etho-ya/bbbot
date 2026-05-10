import cupy as cp
import time
import sys

def gpu_stress_test(duration_sec=30):
    print(f"Starting GPU stress test on Titan V for {duration_sec} seconds...")
    print("This will simulate heavy FP64 Monte Carlo workloads.")
    
    # 100k scenarios, 100 assets - large matrix for FP64
    n_scenarios = 200_000
    n_assets = 200
    
    start_time = time.time()
    count = 0
    
    try:
        while time.time() - start_time < duration_sec:
            # Generate large random matrix (FP64)
            A = cp.random.randn(n_scenarios, n_assets, dtype=cp.float64)
            B = cp.random.randn(n_assets, n_assets, dtype=cp.float64)
            
            # Heavy matrix multiplication
            C = cp.matmul(A, B)
            
            # Simple reduction to ensure work isn't optimized away
            res = cp.sum(C)
            cp.cuda.Stream.null.synchronize()
            
            count += 1
            if count % 10 == 0:
                elapsed = time.time() - start_time
                print(f"[{elapsed:.1f}s] Iteration {count} complete...")
                
    except Exception as e:
        print(f"Error during stress test: {e}")
    
    print(f"Stress test complete. Total iterations: {count}")

if __name__ == "__main__":
    dur = 30
    if len(sys.argv) > 1:
        dur = int(sys.argv[1])
    gpu_stress_test(dur)
