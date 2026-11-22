from xian_wallet_cli.telegram import callback_decision


def test_messaging_decision_binds_user_chat_and_exact_message():
    challenge = "request-challenge"
    callback = {
        "from": {"id": 42},
        "message": {"chat": {"id": 42, "type": "private"}, "message_id": 7},
        "data": f"approve:{challenge}",
    }
    assert callback_decision(callback, 42, 7, challenge) is True
    assert callback_decision(callback, 42, 7, "other-proposal") is None
    assert callback_decision(callback, 42, 8, challenge) is None
    assert callback_decision(callback, 1, 7, challenge) is None
    callback["data"] = f"decline:{challenge}"
    assert callback_decision(callback, 42, 7, challenge) is False
    callback["message"]["chat"]["type"] = "group"
    assert callback_decision(callback, 42, 7, challenge) is None
