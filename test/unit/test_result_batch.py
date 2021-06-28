#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2012-2021 Snowflake Computing Inc. All right reserved.
#

import importlib
from collections import namedtuple
from http import HTTPStatus
from unittest import mock

import pytest
from mock import Mock

from snowflake.connector import DatabaseError, InterfaceError
from snowflake.connector.compat import (
    BAD_GATEWAY,
    BAD_REQUEST,
    FORBIDDEN,
    GATEWAY_TIMEOUT,
    INTERNAL_SERVER_ERROR,
    METHOD_NOT_ALLOWED,
    OK,
    REQUEST_TIMEOUT,
    SERVICE_UNAVAILABLE,
    UNAUTHORIZED,
)
from snowflake.connector.errorcode import (
    ER_FAILED_TO_CONNECT_TO_DB,
    ER_FAILED_TO_REQUEST,
)
from snowflake.connector.network import RetryRequest
from snowflake.connector.result_batch import MAX_DOWNLOAD_RETRY, JSONResultBatch
from snowflake.connector.sqlstate import (
    SQLSTATE_CONNECTION_REJECTED,
    SQLSTATE_CONNECTION_WAS_NOT_ESTABLISHED,
)

REQUEST_MODULE_PATH = (
    "snowflake.connector.vendored.requests"
    if importlib.util.find_spec("snowflake.connector.vendored.requests")
    else "requests"
)

MockRemoteChunkInfo = namedtuple("MockRemoteChunkInfo", "url")


def create_mock_response(status_code):
    mock_resp = Mock()
    mock_resp.status_code = status_code
    mock_resp.raw = "success" if status_code == OK else "fail"
    return mock_resp


@mock.patch(REQUEST_MODULE_PATH + ".get")
def test_ok_response_download(mock_get):
    mock_get.return_value = create_mock_response(200)
    chunk_info = MockRemoteChunkInfo("http://www.chunk-url.com")

    result_batch = JSONResultBatch(100, None, chunk_info, [], [], True)
    response = result_batch._download()

    # successful on first try
    assert mock_get.call_count == 1
    assert response.status_code == 200


@pytest.mark.parametrize(
    "errcode",
    [
        BAD_REQUEST,  # 400
        FORBIDDEN,  # 403
        METHOD_NOT_ALLOWED,  # 405
        REQUEST_TIMEOUT,  # 408
        INTERNAL_SERVER_ERROR,  # 500
        BAD_GATEWAY,  # 502
        SERVICE_UNAVAILABLE,  # 503
        GATEWAY_TIMEOUT,  # 504
        555,  # random 5xx error
    ],
)
def test_retryable_response_download(errcode):
    # retryable exceptions
    with mock.patch(REQUEST_MODULE_PATH + ".get") as mock_get:
        mock_get.return_value = create_mock_response(errcode)

        chunk_info = MockRemoteChunkInfo("http://www.chunk-url.com")
        result_batch = JSONResultBatch(100, None, chunk_info, [], [], True)
        with mock.patch("time.sleep", return_value=None):
            with pytest.raises(RetryRequest) as ex:
                _ = result_batch._download()
            err_msg = ex.value.args[0].msg
            if isinstance(errcode, HTTPStatus):
                assert str(errcode.value) in err_msg
            else:
                assert str(errcode) in err_msg
        assert mock_get.call_count == MAX_DOWNLOAD_RETRY


def test_unauthorized_response_download():
    # unauthorized = 401
    with mock.patch(REQUEST_MODULE_PATH + ".get") as mock_get:
        mock_get.return_value = create_mock_response(UNAUTHORIZED)

        chunk_info = MockRemoteChunkInfo("http://www.chunk-url.com")
        result_batch = JSONResultBatch(100, None, chunk_info, [], [], True)
        with mock.patch("time.sleep", return_value=None):
            with pytest.raises(DatabaseError) as ex:
                _ = result_batch._download()
            error = ex.value
            assert error.errno == ER_FAILED_TO_CONNECT_TO_DB
            assert error.sqlstate == SQLSTATE_CONNECTION_REJECTED
            assert "401" in error.msg
        assert mock_get.call_count == MAX_DOWNLOAD_RETRY


# retry on success codes that are not 200
@pytest.mark.parametrize("status_code", [201, 302])
def test_non_200_response_download(status_code):
    with mock.patch(REQUEST_MODULE_PATH + ".get") as mock_get:
        mock_get.return_value = create_mock_response(status_code)

        chunk_info = MockRemoteChunkInfo("http://www.chunk-url.com")
        result_batch = JSONResultBatch(100, None, chunk_info, [], [], True)
        with mock.patch("time.sleep", return_value=None):
            with pytest.raises(InterfaceError) as ex:
                _ = result_batch._download()
            error = ex.value
            assert error.errno == ER_FAILED_TO_REQUEST
            assert error.sqlstate == SQLSTATE_CONNECTION_WAS_NOT_ESTABLISHED
        assert mock_get.call_count == MAX_DOWNLOAD_RETRY


def test_retries_until_success():
    with mock.patch(REQUEST_MODULE_PATH + ".get") as mock_get:
        error_codes = [BAD_REQUEST, UNAUTHORIZED, 201]
        mock_responses = [create_mock_response(code) for code in error_codes + [OK]]
        mock_get.side_effect = mock_responses

        chunk_info = MockRemoteChunkInfo("http://www.chunk-url.com")
        result_batch = JSONResultBatch(100, None, chunk_info, [], [], True)
        with mock.patch("time.sleep", return_value=None):
            res = result_batch._download()
            assert res.raw == "success"
        # call `get` once for each error and one last time when it succeeds
        assert mock_get.call_count == len(error_codes) + 1
