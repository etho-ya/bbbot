import cupy as cp
import time
import numpy as np

def benchmark_fp64():
    print("--- GPU FP64 Benchmark (Titan V) ---")
    cp.cuda.Device(0).use()
    props = cp.cuda.runtime.getDeviceProperties(0)
    print(f"Device: {props['name'].decode()}")
    
    size = 4096
    print(f"Matrix size: {size}x{size} (Float64)")
    
    a = cp.random.randn(size, size, dtype=cp.float64)
    b = cp.random.randn(size, size, dtype=cp.float64)
    
    # Warmup
    _ = a @ b
    cp.cuda.stream.get_current_stream().synchronize()
    
    # Benchmark
    t0 = time.time()
    iterations = 5
    for _ in range(iterations):
        c = a @ b
    cp.cuda.stream.get_current_stream().synchronize()
    t1 = time.time()
    
    avg_time = (t1 - t0) / iterations
    gflops = (2 * size**3) / (avg_time * 1e9)
    print(f"Average time: {avg_time:.4f}s")
    print(f"Performance: {gflops:.2f} GFLOPS (FP64)")

if __name__ == "__main__":
    benchmark_fp64()
