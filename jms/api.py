#!/usr/bin/env python
# -*- coding: utf-8 -*-
#

from __future__ import unicode_literals, absolute_import

import json
import base64
import logging

try:
    import cStringIO as StringIO
except ImportError:
    import StringIO
try:
    from collections import OrderedDotMap
except ImportError:
    OrderedDotMap = dict

import paramiko
import requests
from requests.structures import CaseInsensitiveDict
from dotmap import DotMap

from .authentication import Auth
from .utils import sort_assets, PKey, dict_to_dotmap, timestamp_to_datetime_str
from .exceptions import RequestError
from .config import API_URL_MAPPING


_USER_AGENT = 'jms-sdk-py'


class Request(object):
    func_mapping = {
        'get': requests.get,
        'post': requests.post,
        'patch': requests.patch,
        'put': requests.put,
    }

    def __init__(self, url, method='get', data=None, params=None, headers=None,
                 content_type='application/json', app_name=''):
        self.url = url
        self.method = method
        self.params = params or {}
        self.result = None

        if not isinstance(headers, dict):
            headers = {}
        self.headers = CaseInsensitiveDict(headers)

        self.headers['Content-Type'] = content_type
        if not isinstance(data, dict):
            data = {}
        if isinstance(data, DotMap):
            data = data.toDict()
        self.data = json.dumps(dict(data))

        if 'User-Agent' not in self.headers:
            if app_name:
                self.headers['User-Agent'] = _USER_AGENT + '/' + app_name
            else:
                self.headers['User-Agent'] = _USER_AGENT

    def request(self):
        self.result = self.func_mapping.get(self.method)(
            url=self.url, headers=self.headers,
            data=self.data,
            params=self.params)
        return self.result


class ApiRequest(object):
    api_url_mapping = API_URL_MAPPING

    def __init__(self, app_name, endpoint, auth=None):
        self.app_name = app_name
        self._auth = auth
        self.req = None
        self.endpoint = endpoint

    @staticmethod
    def parse_result(result):
        try:
            content = result.json()
        except ValueError:
            content = {'error': 'We only support json response'}
            logging.warning(result.content)
            logging.warning(content)
        return result, DotMap({'content': content}).content

    def request(self, api_name=None, pk=None, method='get', use_auth=True,
                data=None, params=None, content_type='application/json'):

        if api_name in self.api_url_mapping:
            path = self.api_url_mapping.get(api_name)
            if pk and '%s' in path:
                path = path % pk
        else:
            path = '/'

        url = self.endpoint.rstrip('/') + path
        self.req = req = Request(url, method=method, data=data,
                                 params=params, content_type=content_type,
                                 app_name=self.app_name)
        if use_auth:
            if not self._auth:
                raise RequestError('Authentication required')
            else:
                self._auth.sign_request(req)
        try:
            result = req.request()
        except (requests.ConnectionError, requests.ConnectTimeout):
            logging.warning('Connect endpoint: {} error'.format(self.endpoint))
            result = {}
        if result.status_code > 500:
            result = {}
            logging.warning('Server internal error')
        return self.parse_result(result)

    def get(self, *args, **kwargs):
        kwargs['method'] = 'get'
        return self.request(*args, **kwargs)

    def post(self, *args, **kwargs):
        kwargs['method'] = 'post'
        return self.request(*args, **kwargs)

    def put(self, *args, **kwargs):
        kwargs['method'] = 'put'
        return self.request(*args, **kwargs)

    def patch(self, *args, **kwargs):
        kwargs['method'] = 'patch'
        return self.request(*args, **kwargs)


class AppService(ApiRequest):
    """使用该类和Jumpserver api进行通信,将terminal用到的常用api进行了封装,
    直接调用方法即可.
        from jms import AppService

        service = AppService(app_name='coco', endpoint='http://localhost:8080')

        # 如果app是第一次启动, 注册一下,并得到 access key, 然后认真
        service.register()
        service.auth()  # 直接使用注册得到的access key进行认证

        # 如果已经启动过, 需要使用access key进行认证
        service.auth(access_key_id, access_key_secret)

        service.check_auth()  # 检测一下是否认证有效
        data = {
            "username": "ibuler",
            "name": "Guanghongwei",
            "hostname": "localhost",
            "ip": "127.0.0.1",
            "system_user": "web",
            "login_type": "ST",
            "was_failed": False,
            "date_start": 1484206685,
        }
        service.send_proxy_log(data)

    """

    def __init__(self, app_name, endpoint, auth=None):
        super(AppService, self).__init__(app_name, endpoint, auth=auth)
        self.access_key_id = None
        self.access_key_secret = None

    def auth(self, access_key_id=None, access_key_secret=None):
        """App认证, 请求api需要签名header
        :param access_key_id: 注册时或新建app用户生成access key id
        :param access_key_secret: 同上access key secret
        """

        if None not in (access_key_id, access_key_secret):
            self.access_key_id = access_key_id
            self.access_key_secret = access_key_secret
        self._auth = Auth(access_key_id=self.access_key_id,
                          access_key_secret=self.access_key_secret)

    def register_terminal(self):
        """注册Terminal, 通常第一次启动需要向Jumpserver注册"""
        r, content = self.post('terminal-register',
                               data={'name': self.app_name},
                               use_auth=False)
        if r.status_code == 201:
            self.access_key_id = content.access_key_id
            self.access_key_secret = content.access_key_secret
            is_success = True
        else:
            is_success = False
        return is_success, content

    def terminal_heatbeat(self):
        """和Jumpserver维持心跳, 当Terminal断线后,jumpserver可以知晓

        Todo: Jumpserver发送的任务也随heatbeat返回, 并执行,如 断开某用户
        """
        r, content = self.post('terminal-heatbeat', use_auth=True)
        if r.status_code == 201:
            return True
        else:
            return False

    def check_auth(self):
        """执行auth后只是构造了请求头, 可以使用该方法连接Jumpserver测试认证"""
        result = self.terminal_heatbeat()
        return result

    def get_system_user_auth_info(self, system_user):
        """获取系统用户的认证信息: 密码, ssh私钥"""
        if isinstance(system_user, dict) and \
                not isinstance(system_user, DotMap):
            system_user = DotMap(system_user)
        assert isinstance(system_user, DotMap)

        r, content = self.get('system-user-auth-info', pk=system_user.id)
        if r.status_code == 200:
            password = content.password
            private_key_string = content.private_key

            if private_key_string and private_key_string.find('PRIVATE KEY'):
                private_key = PKey.from_string(private_key_string)
            else:
                private_key = None

            if isinstance(private_key, paramiko.PKey) \
                    and len(private_key_string.split('\n')) > 2:
                private_key_log_msg = private_key_string.split('\n')[1]
            else:
                private_key_log_msg = 'None'

            logging.debug('Get system user %s password: %s*** key: %s***' %
                          (system_user.username, password[:4],
                           private_key_log_msg))
            return password, private_key
        else:
            logging.warning('Get system user %s password or private key failed'
                            % system_user.username)
            return None, None

    @dict_to_dotmap
    def send_proxy_log(self, data):
        """
        :param data: 格式如下
        data = {
            "username": "username",
            "name": "name",
            "hostname": "hostname",
            "ip": "IP",
            "system_user": "web",
            "login_type": "ST",
            "was_failed": False,
            "date_start": timestamp,
        }
        """
        assert isinstance(data.date_start, (int, float))
        data.date_start = timestamp_to_datetime_str(data.date_start)
        data.was_failed = 1 if data.was_failed else 0

        r, content = self.post('send-proxy-log', data=data, use_auth=True)
        if r.status_code != 201:
            logging.warning('Send proxy log failed: %s' % content)
            return None
        else:
            return content

    @dict_to_dotmap
    def finish_proxy_log(self, data):
        """ 退出登录资产后, 需要汇报结束 时间等

        :param data: 格式如下
        data = {
            "proxy_log_id": 123123,
            "date_finished": timestamp,
        }
        """
        assert isinstance(data.date_finished, (int, float))
        data.date_finished = timestamp_to_datetime_str(data.date_finished)
        data.was_failed = 1 if data.was_failed else 0
        data.is_finished = 1
        proxy_log_id = data.proxy_log_id or 0
        r, content = self.patch('finish-proxy-log', pk=proxy_log_id, data=data)

        if r.status_code != 200:
            logging.warning('Finish proxy log failed: %s' % proxy_log_id)
            return False
        return True

    @dict_to_dotmap
    def send_command_log(self, data):
        """用户输入命令后发送到Jumpserver保存审计
        :param data: 格式如下
        data = {
            'proxy_log': 22,
            'command_no': 1,
            'command': 'ls',
            'output': cmd_output, ## base64.b64encode(output),
            'datetime': timestamp,
        }
        """
        data.output = base64.b64encode(data.output)
        assert isinstance(data.datetime, (int, float))
        data.datetime = timestamp_to_datetime_str(data.datetime)
        result, content = self.post('send-command-log', data=data)
        if result.status_code != 201:
            logging.warning('Create command log failed: %s' % content)
            return False
        return True

    def check_user_authentication(self, token=None, session_id=None,
                                  csrf_token=None):
        """
        用户登陆webterminal或其它网站时,检测用户cookie中的sessionid和csrf_token
        是否合法, 如果合法返回用户,否则返回空
        :param session_id: cookie中的 sessionid
        :param csrf_token: cookie中的 csrftoken
        :return: user object or None
        """
        user_service = UserService(endpoint=self.endpoint)
        user_service.auth(token=token, session_id=session_id,
                          csrf_token=csrf_token)
        user = user_service.is_authenticated()
        return user


class UserService(ApiRequest):
    """使用用户的认证方式请求api, 如 获取用户资产信息

        from jms import UserService
        user_service = UserService('http://localhost:8080')
        data = {'username': 'guanghongwei', 'password': 'pass',
                'public_key': 'public_key string', 'login_type': 'ST',
                'remote_addr': '127.0.0.1'}
        user_service.login(data)
        user_service.is_authenticated()
        user_service.get_my_assets()
        ...
    """

    def __init__(self, endpoint, auth=None):
        super(UserService, self).__init__('', endpoint, auth=auth)
        self.user = None
        self.token = None
        self.session_id = None
        self.csrf_token = None

    def auth_from_token(self, token):
        self._auth = Auth(token=token)

    def auth_from_session(self, session_id, csrf_token):
        self._auth = Auth(session_id=session_id, csrf_token=csrf_token)

    def auth(self, token=None, session_id=None, csrf_token=None):
        if token:
            self.auth_from_token(token)
        elif session_id and csrf_token:
            self.auth_from_session(session_id, csrf_token)
        else:
            raise ValueError('Token or session_id, csrf_token required')

    @dict_to_dotmap
    def login(self, data):
        """用户登录Terminal时需要向Jumpserver进行认证, 登陆成功后返回用户和token
        data = {
            'username': 'guanghongwei',
            'password': 'password',
            'public_key': 'public key string',
            'login_type': 'ST',  # (('ST', 'SSH Terminal'),
                                 #  ('WT', 'Web Terminal'))
            'remote_addr': '2.2.2.2',  # User ip address not app address
        }
        """
        r, content = self.post('user-auth', data=data, use_auth=False)
        if r.status_code == 200:
            self.token = content.token
            self.user = content.user
            self.auth(self.token)
            return self.user, self.token
        else:
            return None, None

    def is_authenticated(self):
        """根据签名判断用户是否认证"""
        r, content = self.post('my-profile', use_auth=True)
        if r.status_code == 200:
            self.user = content
            return self.user
        else:
            return None

    def get_my_assets(self):
        """获取用户被授权的资产列表
        [{'hostname': 'x', 'ip': 'x', ...,
         'system_users_granted': [{'id': 1, 'username': 'x',..}]
        ]
        """
        r, content = self.get('my-assets', use_auth=True)
        if r.status_code == 200:
            assets = content
        else:
            assets = []

        assets = sort_assets(assets)
        for asset in assets:
            asset.system_users_granted = \
                [system_user for system_user in asset.system_users_granted]
        return assets

    def get_my_asset_groups(self):
        """获取用户授权的资产组列表
        [{'name': 'x', 'comment': 'x', 'assets_amount': 2}, ..]
        """
        r, content = self.get('my-asset-groups', use_auth=True)
        if r.status_code == 200:
            asset_groups = content
        else:
            asset_groups = []
        asset_groups = [asset_group for asset_group in asset_groups]
        return asset_groups

    def get_user_asset_group_assets(self, asset_group_id):
        """获取用户在该资产组下的资产, 并非该资产组下的所有资产,而是授权了的
        返回资产列表, 和获取资产格式一致

        :param asset_group_id: 资产组id
        """
        r, content = self.get('assets-of-group', use_auth=True,
                              pk=asset_group_id)
        if r.status_code == 200:
            assets = content
        else:
            assets = []
        assets = sort_assets(assets)
        return [asset for asset in assets]

