from logscope.core.fingerprint import fingerprint, template


def test_collapses_variable_parts():
    a = "Unable to get disk metrics for /run/docker/netns/7d7423c17612: permission denied"
    b = "Unable to get disk metrics for /run/user/1000/gvfs: permission denied"
    assert fingerprint(a) == fingerprint(b)


def test_collapses_ips_ports_numbers():
    a = "connection to 10.0.0.1:8126 failed after 3 retries in 1.5s"
    b = "connection to 192.168.4.77:9999 failed after 12 retries in 300ms"
    assert fingerprint(a) == fingerprint(b)


def test_collapses_quoted_strings_and_ids():
    a = 'Post "https://process.datadoghq.com./api/v1": timeout for 508142aba25c'
    b = 'Post "https://other.example.com/x": timeout for deadbeef0123'
    assert fingerprint(a) == fingerprint(b)


def test_distinct_messages_differ():
    assert fingerprint("Error while processing transaction") != \
        fingerprint("failed to flush agent telemetry session")


def test_template_readable():
    t = template("retry 5 of 10 to 10.0.0.1:80 took 1.5s")
    assert t == "retry <N> of <N> to <IP> took <DUR>"
