from unittest.mock import Mock

import pytest

from app.app_utils.retry import retry, retry_call


def test_retry_decorator_success():
    call_count = 0

    @retry(tries=3, delay=0.01)
    def dummy_func():
        nonlocal call_count
        call_count += 1
        return "success"

    res = dummy_func()
    assert res == "success"
    assert call_count == 1

def test_retry_decorator_fail_then_success():
    call_count = 0

    @retry(tries=3, delay=0.01)
    def dummy_func():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ValueError("temporary error")
        return "success"

    res = dummy_func()
    assert res == "success"
    assert call_count == 2

def test_retry_decorator_all_fail():
    call_count = 0

    @retry(tries=3, delay=0.01)
    def dummy_func():
        nonlocal call_count
        call_count += 1
        raise ValueError("permanent error")

    with pytest.raises(ValueError, match="permanent error"):
        dummy_func()
    assert call_count == 3

def test_retry_call_success():
    func = Mock(return_value="ok")
    res = retry_call(func, tries=2, delay=0.01)
    assert res == "ok"
    assert func.call_count == 1

def test_retry_call_fail():
    func = Mock(side_effect=RuntimeError("fail"))
    with pytest.raises(RuntimeError, match="fail"):
        retry_call(func, tries=3, delay=0.01)
    assert func.call_count == 3
