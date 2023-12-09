import time
import random
import pexpect
import logging
from functools import wraps

from datetime import datetime, timedelta

qm_monitor_defer_close = True
deferred_closing = []

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

        def invalidate_cache(*args, **kwargs):
            key = str(args) + str(kwargs)
            if key in cache:
                del cache[key]

        # Attach the invalidation function to the wrapper
        wrapper.invalidate_cache = invalidate_cache

        return wrapper
    return decorator

@ttl_cache_with_randomness(qm_max_ttl, qm_rand)
def qm_term_cmd(vm_id, cmd, timeout=global_qm_timeout): # TODO: ignore cmd timeout in cache key
    global deferred_closing
    child = pexpect.spawn(f'qm monitor {vm_id}')
    try:
        child.expect('qm>', timeout=timeout)
        child.sendline(cmd)
        child.expect('qm>', timeout=timeout)
        raw_output = child.before.decode('utf-8').strip()
    finally:
        try:
            child.close()
        except pexpect.exceptions.ExceptionPexpect:
            if qm_monitor_defer_close:
                logging.warn(f"Failed to close {vm_id=}, {cmd=}; deferring")
                deferred_closing.append((child, datetime.now()))

    if qm_monitor_defer_close:
        # Reattempt closing deferred child processes
        still_deferred = []
        for child, timestamp in deferred_closing:
            if datetime.now() - timestamp > timedelta(seconds=10):
                try:
                    child.close()
                except pexpect.exceptions.ExceptionPexpect:
                    still_deferred.append((child, timestamp))
            else:
                still_deferred.append((child, timestamp))

        deferred_closing = still_deferred

        if deferred_closing:
            raise Exception("Could not terminate some child processes after 10 seconds.")

    return raw_output
