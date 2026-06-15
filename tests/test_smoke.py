from chrome_wrapper_plugin.server import ping


def test_ping():
    assert ping() == "pong"
