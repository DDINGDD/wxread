# main.py 主逻辑：包括字段拼接、模拟请求
import hashlib
import json
import logging
import random
import time
import urllib.parse

import requests

from config import PUSH_METHOD, READ_NUM, book, chapter, cookies, data, headers
from log_utils import setup_logging
from push import push


# 加密盐及其它默认值
KEY = "3c5c8717f3daf09iop3423zafeqoi"
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"
COOKIE_DATA_VARIANTS = [
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
    {"rq": "%2Fweb%2Fbook%2Fread"},
]

REQUEST_TIMEOUT = (10, 30)
READ_REQUEST_ATTEMPTS = 5
READ_RETRY_DELAYS = (2, 4, 8, 16)

refresh_print = setup_logging()


class ReadRequestError(RuntimeError):
    """阅读接口在多次重试后仍无法返回可解析的 JSON。"""


def encode_data(payload):
    """数据编码。"""
    return "&".join(
        f"{key}={urllib.parse.quote(str(payload[key]), safe='')}"
        for key in sorted(payload.keys())
    )


def cal_hash(input_string):
    """计算哈希值。"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7FFFFFFF & (
            _7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30
        )
        _cc1055 = 0x7FFFFFFF & (
            _cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30
        )
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()


def get_wr_skey():
    """刷新 cookie 密钥。"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = requests.post(
                RENEW_URL,
                headers=headers,
                cookies=cookies,
                data=json.dumps(cookie_data, separators=(",", ":")),
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            if "wr_skey" in response.cookies:
                return response.cookies["wr_skey"][:8]
        except requests.RequestException as exc:
            logging.warning(
                "refresh_cookie 请求失败，payload=%s，原因：%s",
                cookie_data,
                exc,
            )

    return None


def refresh_cookie():
    """刷新当前会话 cookie，失败时抛出异常交由顶层统一通知。"""
    logging.info("刷新 cookie")
    new_skey = get_wr_skey()
    if not new_skey:
        raise RuntimeError("无法获取新密钥或者 WXREAD_CURL_BASH 配置有误，终止运行。")

    cookies["wr_skey"] = new_skey
    logging.info("密钥刷新成功，新密钥：%s***", new_skey[:2])
    logging.info("重新本次阅读。")


def fix_no_synckey():
    """尝试修复缺失 synckey 的情况；失败时返回 False。"""
    try:
        response = requests.post(
            FIX_SYNCKEY_URL,
            headers=headers,
            cookies=cookies,
            data=json.dumps({"bookIds": ["3300060341"]}, separators=(",", ":")),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logging.warning("修复 synckey 请求失败：%s", exc)
        return False


def post_json_with_retry(url, **kwargs):
    """POST 并解析 JSON；网络异常、空响应和非 JSON 响应都会指数退避重试。"""
    last_error = None

    for attempt in range(1, READ_REQUEST_ATTEMPTS + 1):
        response = None
        try:
            response = requests.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
            response.raise_for_status()

            if not response.content or not response.text.strip():
                raise ValueError(
                    f"服务器返回空响应，HTTP {response.status_code}"
                )

            try:
                return response.json()
            except requests.exceptions.JSONDecodeError as exc:
                content_type = response.headers.get("content-type", "unknown")
                raise ValueError(
                    "服务器返回非 JSON 数据，"
                    f"HTTP {response.status_code}，Content-Type={content_type}，"
                    f"响应长度={len(response.content)} bytes"
                ) from exc

        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logging.warning(
                "阅读请求失败，第 %d/%d 次：%s",
                attempt,
                READ_REQUEST_ATTEMPTS,
                exc,
            )

            if response is not None:
                logging.debug(
                    "异常响应：status=%s, content-type=%s, length=%d",
                    response.status_code,
                    response.headers.get("content-type", "unknown"),
                    len(response.content),
                )

            if attempt < READ_REQUEST_ATTEMPTS:
                delay_index = min(attempt - 1, len(READ_RETRY_DELAYS) - 1)
                delay = READ_RETRY_DELAYS[delay_index]
                logging.info("%d 秒后重试阅读请求。", delay)
                time.sleep(delay)

    raise ReadRequestError(
        f"阅读接口连续 {READ_REQUEST_ATTEMPTS} 次未返回有效 JSON"
    ) from last_error


def read_once():
    """发送一次阅读请求；首次重试耗尽时刷新 cookie 后再给一次恢复机会。"""
    request_kwargs = {
        "headers": headers,
        "cookies": cookies,
        "data": json.dumps(data, separators=(",", ":")),
    }

    try:
        return post_json_with_retry(READ_URL, **request_kwargs)
    except ReadRequestError as first_error:
        logging.warning("阅读接口持续异常，尝试刷新 cookie 后恢复当前阅读请求。")
        refresh_cookie()
        try:
            return post_json_with_retry(READ_URL, **request_kwargs)
        except ReadRequestError as second_error:
            raise ReadRequestError(
                "刷新 cookie 后阅读接口仍持续返回无效响应"
            ) from second_error


def run():
    """执行完整阅读流程。"""
    refresh_cookie()
    index = 1
    last_time = int(time.time()) - 30
    logging.info("一共需要阅读 %d 次。", READ_NUM)

    while index <= READ_NUM:
        data.pop("s", None)
        data["b"] = random.choice(book)
        data["c"] = random.choice(chapter)
        this_time = int(time.time())
        data["ct"] = this_time
        data["rt"] = this_time - last_time
        data["ts"] = int(this_time * 1000) + random.randint(0, 1000)
        data["rn"] = random.randint(0, 1000)
        data["sg"] = hashlib.sha256(
            f"{data['ts']}{data['rn']}{KEY}".encode()
        ).hexdigest()
        data["s"] = cal_hash(encode_data(data))

        refresh_print(
            f"阅读进度: 第 {index}/{READ_NUM} 次，"
            f"已完成 {(index - 1) * 0.5:.1f} 分钟"
        )
        logging.debug("data: %s", data)

        res_data = read_once()
        logging.debug("response: %s", res_data)

        if "succ" in res_data:
            if "synckey" in res_data:
                last_time = this_time
                index += 1

                refresh_print(
                    f"阅读进度: 第 {min(index, READ_NUM + 1) - 1}/{READ_NUM} 次，"
                    f"已完成 {(index - 1) * 0.5:.1f} 分钟"
                )

                # 最后一次完成后无需再等待 30 秒，给 GitHub Actions 留出退出余量。
                if index <= READ_NUM:
                    time.sleep(30)
            else:
                logging.warning("无 synckey，尝试修复...")
                if not fix_no_synckey():
                    logging.warning("synckey 修复失败，刷新 cookie 后重试当前阅读。")
                    refresh_cookie()
        else:
            logging.warning("cookie 已过期或接口返回失败状态，尝试刷新...")
            refresh_cookie()

    logging.info("阅读脚本已完成。")

    if PUSH_METHOD not in (None, ""):
        logging.info("开始推送...")
        push(
            f"微信读书自动阅读完成。\n阅读时长：{(index - 1) * 0.5} 分钟。",
            PUSH_METHOD,
            is_success=True,
        )
    else:
        logging.info("未配置推送渠道，跳过推送。")


def notify_failure(exc):
    """发生未恢复异常时发送失败通知，但不让通知异常覆盖原始错误。"""
    if PUSH_METHOD in (None, ""):
        return

    try:
        push(
            f"微信读书自动阅读失败。\n原因：{type(exc).__name__}: {exc}",
            PUSH_METHOD,
            is_success=False,
        )
    except Exception:
        logging.exception("发送失败通知时再次出现异常。")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        logging.exception("阅读脚本异常终止：%s", exc)
        notify_failure(exc)
        raise
