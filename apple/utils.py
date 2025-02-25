import argparse
import os
import re
import shlex
from collections import OrderedDict, namedtuple
from http.cookies import SimpleCookie
from typing import Dict, Union

import gevent
import requests
from requests import Timeout

from base.style import str_json, now, to_form_url, Assert, Log, json_str, Fail, str_json_i, Block, ExFalse
from base.utils import base64, base64decode
from frameworks.db import db_session, message_from_topic
# noinspection PyProtectedMember
from frameworks.utils import DbLock
from .models import IosAccountInfo


def _cache(url: str, data: Dict):
    return db_session.get("http:cache:%s:%s" % (url, to_form_url(data)))


def _set_cache(url: str, data: Dict, content: str, expire: int):
    db_session.set("http:cache:%s:%s" % (url, to_form_url(data)), content, ex=expire // 1000)


__pub = db_session.pubsub()

# noinspection PyBroadException
try:
    db_session.info()
    __pub.subscribe("account:security:code")
except:
    if os.environ.get("REDIS_HOST"):
        Fail("redis[%s:%s]没准备好" % (os.environ.get("REDIS_HOST"), os.environ.get("REDIS_PORT")))
    else:
        pass


def publish_security_code(account: str, code: str, ts: int):
    db_session.publish("account:security:code", json_str({
        "account": account,
        "code": code,
        "ts": ts,
    }))


def get_capability(cate: str):
    if cate == "GAME_CENTER":
        return {
            "type": "bundleIdCapabilities",
            "attributes": {
                "enabled": True,
                "settings": [],
            },
            "relationships": {
                "capability": {
                    "data": {
                        "type": "capabilities",
                        "id": "GAME_CENTER",
                    },
                },
            },
        }
    elif cate == "IN_APP_PURCHASE":
        return {
            "type": "bundleIdCapabilities",
            "attributes": {
                "enabled": True,
                "settings": [],
            },
            "relationships": {
                "capability": {
                    "data": {
                        "type": "capabilities",
                        "id": "IN_APP_PURCHASE",
                    },
                },
            },
        }
    else:
        raise Fail("不支持的capability[%s]" % cate)


def _wait_code(info: IosAccountInfo, session: requests.Session, ts, save_now=False):
    """
    等待二次提交的需要
    """
    Log("等待[%s]二次验证" % info.account)
    # last = info.security_code
    expire = now() + 1200 * 1000
    while now() < expire:
        gevent.sleep(1)
        for data in message_from_topic(__pub, is_json=True, limit=1):
            if data.get("ts") > ts:
                if data.get("code") and data.get("account") in {info.account, "*"}:
                    rsp = session.post("https://idmsa.apple.com/appleauth/auth/verify/phone/securitycode", json={
                        "securityCode": {
                            "code": str(data.get("code")),
                        },
                        "phoneNumber": {
                            "id": 1,
                        },
                        "mode": "sms",
                    })
                    # Log("[%s] %s" % (rsp.status_code, rsp.json()))
                    # rsp = session.post("https://idmsa.apple.com/appleauth/auth/verify/trusteddevice/securitycode", json={
                    #     "securityCode": {"code": data["code"]},
                    # })
                    if rsp.status_code != 200:
                        Log("账号[%s]验证校验码[%s]失败[%s]" % (info.account, data.get("code"), rsp.status_code))
                    # if rsp.status_code != 204:
                    #     Log("账号[%s]验证校验码[%s]失败[%s]" % (info.account, data.get("code"), rsp.status_code))
                    #     continue
                    rsp = session.get("https://idmsa.apple.com/appleauth/auth/2sv/trust")
                    if rsp.status_code == 204:
                        session.cookies.update({
                            "__expire": str(now() + 6 * 3600 * 1000)
                        })
                        if save_now:
                            info.cookie = json_str(session.cookies)
                            info.save()
                        Log("账号[%s]登录成功" % info.account)
                        return session
    raise Fail("登录二次验证超时")


class IosAccountHelper:
    def __init__(self, info: IosAccountInfo):
        self.info = info
        self.account = info.account
        self.password = info.password
        self.teams = str_json_i(info.teams, default=[])
        self.team_id = info.team_id
        self.headers = str_json(info.headers)
        self.cookie = str_json(info.cookie)  # type: Dict[str,str]
        self.csrf = info.csrf
        self.csrf_ts = info.csrf_ts
        self.session = requests.session()
        # self.session.headers.update(self.headers)
        # self.session.cookies.update(self.cookie)

    def post(self, title: str, url: str, data: Union[Dict, str] = None, is_json=True, log=True, cache: Union[bool, int] = False, csrf=False,
             json_api=True, method="POST", is_binary=False, ex_headers=None, status=200):
        if cache is True:
            expire = 3600 * 1000
        else:
            expire = cache

        if not self.is_login:
            self.__login()
        start = now()
        rsp_str = "#NODATA#"
        try:
            if "teamId=" in url:
                if "teamId=%s" % self.team_id not in url:
                    url = url.replace("teamId=", "teamId=%s" % self.team_id)
            if cache:
                rsp_str = _cache(url, data) or rsp_str
            headers = {
                'cookie': to_form_url({
                    "myacinfo": self.cookie["myacinfo"]
                }, split=';'),
            }
            if csrf:
                headers.update({
                    'csrf': self.csrf,
                    'csrf_ts': str(self.csrf_ts),
                })
            if ex_headers:
                headers.update(ex_headers)
            if rsp_str == "#NODATA#":
                cnt = 3
                rsp = None
                while cnt >= 0:
                    cnt -= 1
                    try:
                        if method.upper() == "GET":
                            rsp = requests.get(url, params=data, headers=headers, timeout=3, verify=False)
                        else:
                            rsp = requests.post(url, data=data, headers=headers, timeout=3, verify=False)
                        if rsp.headers.get("csrf"):
                            self.csrf = rsp.headers["csrf"]
                            self.csrf_ts = int(rsp.headers["csrf_ts"])
                        rsp_str = rsp.text
                        Assert(rsp.status_code == status, "请求[%s]异常[%s]" % (title, rsp.status_code))
                        break
                    except Timeout:
                        Log("apple请求[%s][%s]发送[%r]超时剩余尝试[%s]" % (title, now() - start, data, cnt))
                Assert(cnt > 0 and rsp is not None, "apple请求[%s][%s]发送[%r]超时多次失败" % (title, now() - start, data))
                if json_api:
                    _data = str_json_i(rsp_str) or {}
                    if _data.get("resultCode") == 1100:
                        self.__logout()
                        raise Fail("登录[%s]过期了[%s][%s]" % (self.account, _data.get("resultString"), _data.get("userString")))
                    Assert(_data.get("resultCode") == 0, "请求业务[%s]失败[%s][%s]" % (title, _data.get("resultString"), _data.get("userString")))
                if log:
                    Log("apple请求[%s][%s]发送[%r]成功[%r]" % (title, now() - start, data, rsp_str))
                if is_binary:
                    rsp_str = base64(rsp.content)
                if cache:
                    _set_cache(url, data, rsp_str, expire=expire)
            if is_json:
                return str_json(rsp_str)
            elif is_binary:
                return base64decode(rsp_str)
            else:
                return rsp_str
        except Exception as e:
            if log:
                Log("apple请求[%s][%s]发送[%r]失败[%r]" % (title, now() - start, data, rsp_str))
            raise e

    @property
    def is_login(self) -> ExFalse:
        if "myacinfo" not in self.cookie:
            return ExFalse("未登录")
        if int(self.cookie.get("__expire", 0)) < now():
            # 登录信息已经过期需要刷新
            return ExFalse("登录过期")
        if not self.team_id:
            return ExFalse("team_id缺失")
        return ExFalse.TRUE

    def __save_cookie(self, cookie: Dict):
        _orig = self.info.cookie
        _new = json_str(cookie)
        if _orig == _new:
            return
        self.info.cookie = _new
        self.info.save()

    def __logout(self):
        Log("登出账号[%s]" % self.account)
        _key = "apple:developer:cookie"
        self.cookie.clear()
        self.info.cookie = "{}"
        self.info.save()

    def touch(self, force=False):
        self.__login(force=force)

    def __login(self, force=False):
        if not force and self.csrf_ts > now():
            return
        with Block("账号登录", lock=DbLock("登录%s" % self.account)):
            ret = requests.post(
                "https://developer.apple.com/services-account/QH65B2/account/getTeams",
                json={
                    "includeInMigrationTeams": 1,
                },
                headers={
                    'cookie': to_form_url(self.cookie, split=';'),
                }, timeout=3, verify=False).json() if not self.team_id else {}
            if ret.get("resultCode") == 0:
                self.teams = list(map(lambda x: x["teamId"], ret["teams"]))
                self.info.team_id = self.team_id = self.teams[0]
                self.info.team_member_id = ret["teams"][0]["teamMemberId"]
                if len(self.teams) > 1:
                    for team in ret["teams"]:
                        if "Account Holder" in team["userRoles"]:
                            # 优先选择自己的team
                            self.info.team_id = self.team_id = team["teamId"]
                            self.info.team_member_id = team["teamMemberId"]
                self.info.teams = json_str(self.teams)
                self.info.save()
            else:
                # 重新登录
                self.session = requests.session()
                self.session.headers["User-Agent"] = "Spaceship 2.117.1"

                rsp = self.session.get("https://olympus.itunes.apple.com/v1/app/config?hostname=itunesconnect.apple.com").json()
                self.session.headers["X-Apple-Widget-Key"] = rsp["authServiceKey"]
                # self.session.headers["X-Apple-Widget-Key"] = "16452abf721961a1728885bef033f28e"
                self.session.headers["Accept"] = "application/json"
                rsp = self.session.post("https://idmsa.apple.com/appleauth/auth/signin", json={
                    "accountName": self.account,
                    "password": self.password,
                    "rememberMe": True,
                }, timeout=3)
                self.session.headers["x-apple-id-session-id"] = rsp.headers["x-apple-id-session-id"]
                self.session.headers["scnt"] = rsp.headers["scnt"]
                if rsp.status_code == 409:
                    # 二次验证
                    # noinspection PyUnusedLocal
                    rsp = self.session.post("https://idmsa.apple.com/appleauth/auth")
                    # Log("===> https://idmsa.apple.com/appleauth/auth [%s] %s" % (rsp.status_code, rsp.json()))

                    # 切手机验证码
                    rsp = self.session.put("https://idmsa.apple.com/appleauth/auth/verify/phone", json={
                        "phoneNumber": {
                            "id": 1
                        },
                        "mode": "sms",
                    })
                    Assert(rsp.status_code == 200, "[%s]短信发送失败" % self.account)
                    # Log("===> https://idmsa.apple.com/appleauth/auth/verify/phone [%s] %s" % (rsp.status_code, rsp.json()))
                    _wait_code(self.info, self.session, now())
                else:
                    self.session.cookies.update({
                        "__expire": str(now() + 6 * 3600 * 1000)
                    })

                self.cookie.update(self.session.cookies)
                self.__save_cookie(self.cookie)
        if not self.team_id:
            ret = requests.post(
                "https://developer.apple.com/services-account/QH65B2/account/getTeams",
                json={
                    "includeInMigrationTeams": 1,
                },
                headers={
                    'cookie': to_form_url(self.cookie, split=';'),
                }, timeout=3, verify=False).json()
            if ret["resultCode"] == 0:
                self.teams = list(map(lambda x: x["teamId"], ret["teams"]))
                self.info.team_id = self.team_id = self.teams[0]
                self.info.teams = json_str(self.teams)
                self.info.save()
            else:
                Log("[%s]获取team失败登出了" % self.account)
                self.__logout()
        Log("apple账号[%s:%s]登录完成了" % (self.account, self.team_id))


parser = argparse.ArgumentParser()
parser.add_argument('command')
parser.add_argument('url')
parser.add_argument('-d', '--data')
parser.add_argument('-b', '--data-binary', default=None)
parser.add_argument('-X', default='')
parser.add_argument('-H', '--header', action='append', default=[])
parser.add_argument('--compressed', action='store_true')
parser.add_argument('--insecure', action='store_true')
ParsedContext = namedtuple('ParsedContext', ['method', 'url', 'data', 'headers', 'cookies', 'verify'])


def curl_parse_context(curl_command):
    method = "get"

    tokens = shlex.split(curl_command)

    # noinspection PyUnusedLocal
    def no_exit(*args, **kwargs):
        pass

    parser.exit = no_exit
    parsed_args = parser.parse_args(tokens)

    post_data = parsed_args.data or parsed_args.data_binary
    if post_data:
        method = 'post'

    if parsed_args.X:
        method = parsed_args.X.lower()

    cookie_dict = OrderedDict()
    quoted_headers = OrderedDict()

    for curl_header in parsed_args.header:
        if curl_header.startswith(':'):
            occurrence = [m.start() for m in re.finditer(':', curl_header)]
            header_key, header_value = curl_header[:occurrence[1]], curl_header[occurrence[1] + 1:]
        else:
            header_key, header_value = curl_header.split(":", 1)

        if header_key.lower() == 'cookie':
            cookie = SimpleCookie(header_value)
            for key in cookie:
                cookie_dict[key] = cookie[key].value
        else:
            quoted_headers[header_key] = header_value.strip()

    return ParsedContext(
        method=method,
        url=parsed_args.url,
        data=post_data,
        headers=quoted_headers,
        cookies=cookie_dict,
        verify=parsed_args.insecure
    )
