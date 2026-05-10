
import requests
import socket
import sys

def test_url(url, family):
    # Overriding getaddrinfo to force IPv4 or IPv6
    orig_getaddrinfo = socket.getaddrinfo
    def filtered_getaddrinfo(*args, **kwargs):
        res = orig_getaddrinfo(*args, **kwargs)
        return [r for r in res if r[0] == family]
    
    socket.getaddrinfo = filtered_getaddrinfo
    
    try:
        response = requests.get(url, timeout=10)
        print(f"Family {family}: Status {response.status_code}, Reason: {response.reason}")
        if response.status_code == 403:
            print(f"Body: {response.text}")
        return response.status_code
    except Exception as e:
        print(f"Family {family}: Failed with {type(e).__name__}: {e}")
        return None
    finally:
        socket.getaddrinfo = orig_getaddrinfo

url = "https://api-testnet.bybit.com/v5/public/time"
print(f"Testing {url}...")
print("\n--- Testing IPv4 ---")
test_url(url, socket.AF_INET)
print("\n--- Testing IPv6 ---")
test_url(url, socket.AF_INET6)
