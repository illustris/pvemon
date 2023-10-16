import pexpect

global_qm_timeout = 10

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
