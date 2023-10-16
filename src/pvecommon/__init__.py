import time
import random
import pexpect
from functools import wraps

global_qm_timeout = 10
qm_max_ttl = 600
qm_rand = 60

def ttl_cache_with_randomness(max_ttl, randomness_factor):
    cache = {}
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Create a key based on the args and kwargs
            key = str(args) + str(kwargs)
            # Check if the key is in the cache and not expired
            if key in cache:
                result, timestamp = cache[key]
                elapsed_time = time.time() - timestamp
                if elapsed_time < max_ttl + random.uniform(-randomness_factor, randomness_factor):
                    return result
            # Call the actual function and store the result in cache
            result = func(*args, **kwargs)
            cache[key] = (result, time.time())
            return result
        return wrapper
    return decorator

@ttl_cache_with_randomness(qm_max_ttl, qm_rand)
def qm_term_cmd(vm_id, cmd, timeout=global_qm_timeout):
    child = pexpect.spawn(f'qm monitor {vm_id}')
    try:
        child.expect('qm>', timeout=timeout)
        child.sendline(cmd)
        child.expect('qm>', timeout=timeout)
        raw_output = child.before.decode('utf-8').strip()
    finally:
        child.close()

    return raw_output
