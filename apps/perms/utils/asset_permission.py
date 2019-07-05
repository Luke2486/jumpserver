# coding: utf-8

import time
import uuid
from collections import defaultdict
import json
from hashlib import md5
import itertools

from django.utils import timezone
from django.db.models import Q
from django.core.cache import cache
from django.conf import settings
from django.utils.translation import ugettext as _

from orgs.utils import set_to_root_org
from common.utils import get_logger, timeit
from common.tree import TreeNode
from .. import const
from ..models import AssetPermission, Action
from ..hands import Node, Asset
from assets.utils import NodeUtil

logger = get_logger(__file__)


__all__ = [
    'AssetPermissionUtil', 'is_obj_attr_has', 'sort_assets',
    'parse_asset_to_tree_node', 'parse_node_to_tree_node',
]


class GenerateTree:
    def __init__(self):
        """
        nodes = {
          "<node1>": {
            "system_users": {
              "system_user": action,
              "system_user2": action,
            },
            "assets": set([<asset_instance>]),
          }
        }
        assets = {
           "<asset_instance2>": {
             "system_user": action,
             "system_user2": action,
           },
        }
        """
        self._node_util = None
        self.nodes = defaultdict(lambda: {"system_users": defaultdict(int), "assets": set(), "assets_amount": 0})
        self.assets = defaultdict(lambda: defaultdict(int))
        self._root_node = None
        self._ungroup_node = None
        self._nodes_with_assets = None
        self._all_assets_nodes_key = None
        self._asset_counter = 0
        self._system_user_counter = 0
        self._nodes_assets_counter = 0

    @property
    def node_util(self):
        if not self._node_util:
            self._node_util = NodeUtil()
        return self._node_util

    @staticmethod
    def key_sort(key):
        key_list = [int(i) for i in key.split(':')]
        return len(key_list), key_list

    @property
    def root_key(self):
        if self._root_node:
            return self._root_node
        all_nodes = self.nodes.keys()
        # 如果没有授权节点，就放到默认的根节点下
        if not all_nodes:
            return None
        root_node = min(all_nodes, key=self.key_sort)
        self._root_node = root_node
        return root_node

    @property
    def all_assets_nodes_keys(self):
        if not self._all_assets_nodes_key:
            self._all_assets_nodes_key = Asset.get_all_nodes_keys()
        return self._all_assets_nodes_key

    @property
    def ungrouped_key(self):
        if self._ungroup_node:
            return self._ungroup_node
        if self.root_key:
            node_key = "{}:{}".format(self.root_key, '0')
        else:
            node_key = '0:1'
        self.add_node(node_key, {})
        self._ungroup_node = node_key
        return node_key

    @property
    def empty_node(self):
        return '0:2'

    @timeit
    def add_assets_without_system_users(self, assets_ids):
        for asset_id in assets_ids:
            self.add_asset(asset_id, {})
        print("Couter assets: {}".format(self._asset_counter))
        print("Couter system user: {}".format(self._system_user_counter))
        print("Couter nodes: {}".format(self._nodes_assets_counter))

    @timeit
    def add_assets(self, assets_ids_with_system_users):
        for asset_id, system_users_ids in assets_ids_with_system_users.items():
            self.add_asset(asset_id, system_users_ids)

    # @timeit
    def add_asset(self, asset_id, system_users_ids=None):
        asset_nodes_keys = self.all_assets_nodes_keys.get(asset_id, [])
        if not system_users_ids:
            system_users_ids = defaultdict(int)
        self._asset_counter += 1

        old_system_users_ids = self.assets[asset_id]
        for system_user_id, action in old_system_users_ids.items():
            system_users_ids[system_user_id] |= action
            self._system_user_counter += 1

        # 获取父节点们
        parents_keys = self.node_util.get_nodes_parents_keys_by_keys(
            asset_nodes_keys, with_self=True
        )
        # for node_key in parents_keys:
        #     _system_users_ids = self.nodes[node_key]["system_users"]
        #     self.nodes[node_key]["assets_amount"] += 1
        #     for system_user_id, action in _system_users_ids.items():
        #         system_users_ids[system_user_id] |= action
        #         self._counter += 1

        self.assets[asset_id] = system_users_ids
        in_nodes = set(self.nodes.keys()) & set(parents_keys)
        if not in_nodes:
            self.nodes[self.ungrouped_key]["assets_amount"] += 1
            self.nodes[self.ungrouped_key]["assets"].add(asset_id)
            return

        # for key in in_nodes:
        #     self._nodes_assets_counter += 1
        #     self.nodes[key]["assets"].add(asset_id)

    def add_node(self, node_key, system_users_ids=None):
        if not system_users_ids:
            system_users_ids = defaultdict(int)
        self.nodes[node_key]["system_users"] = system_users_ids

    # 添加树节点
    @timeit
    def add_nodes(self, nodes_keys_with_system_users_ids):
        _nodes_keys = nodes_keys_with_system_users_ids.keys()
        family = self.node_util.get_family_keys_by_keys(_nodes_keys, with_children=True)
        for node_key in family:
            system_users_ids = nodes_keys_with_system_users_ids.get(node_key)
            self.add_node(node_key, system_users_ids)

    def get_assets(self):
        assets = []
        for asset_id, system_users in self.assets.items():
            assets.append({"id": asset_id, "system_users": system_users})
        return assets

    @timeit
    def get_nodes_with_assets(self):
        if self._nodes_with_assets:
            return self._nodes_with_assets
        nodes = {}
        for node, values in self.nodes.items():
            node._assets_amount = values["assets_amount"]
            nodes[node] = {asset: self.assets.get(asset, {}) for asset in values["assets"]}
        # 如果返回空节点，页面构造授权资产树报错
        if not nodes:
            nodes[self.empty_node] = {}
        self._nodes_with_assets = nodes
        return dict(nodes)

    def get_nodes(self):
        return list(self.nodes.keys())


def get_user_permissions(user, include_group=True):
    if include_group:
        groups = user.groups.all()
        arg = Q(users=user) | Q(user_groups__in=groups)
    else:
        arg = Q(users=user)
    return AssetPermission.get_queryset_with_prefetch().filter(arg)


def get_user_group_permissions(user_group):
    return AssetPermission.get_queryset_with_prefetch().filter(
        user_groups=user_group
    )


def get_asset_permissions(asset, include_node=True):
    if include_node:
        nodes = asset.get_all_nodes(flat=True)
        arg = Q(assets=asset) | Q(nodes__in=nodes)
    else:
        arg = Q(assets=asset)
    return AssetPermission.objects.valid().filter(arg)


def get_node_permissions(node):
    return AssetPermission.objects.valid().filter(nodes=node)


def get_system_user_permissions(system_user):
    return AssetPermission.objects.valid().filter(
        system_users=system_user
    )


class AssetPermissionCacheMixin:
    CACHE_KEY_PREFIX = '_ASSET_PERM_CACHE_'
    CACHE_META_KEY_PREFIX = '_ASSET_PERM_META_KEY_'
    CACHE_TIME = settings.ASSETS_PERM_CACHE_TIME
    CACHE_POLICY_MAP = (('0', 'never'), ('1', 'using'), ('2', 'refresh'))
    cache_policy = '1'
    obj_id = ''
    _filter_id = None

    @classmethod
    def is_not_using_cache(cls, cache_policy):
        return cls.CACHE_TIME == 0 or cache_policy in cls.CACHE_POLICY_MAP[0]

    @classmethod
    def is_using_cache(cls, cache_policy):
        return cls.CACHE_TIME != 0 and cache_policy in cls.CACHE_POLICY_MAP[1]

    @classmethod
    def is_refresh_cache(cls, cache_policy):
        return cache_policy in cls.CACHE_POLICY_MAP[2]

    def _is_not_using_cache(self):
        return self.is_not_using_cache(self.cache_policy)

    def _is_using_cache(self):
        return self.is_using_cache(self.cache_policy)

    def _is_refresh_cache(self):
        return self.is_refresh_cache(self.cache_policy)

    @timeit
    def get_cache_key(self, resource):
        cache_key = self.CACHE_KEY_PREFIX + '{obj_id}_{filter_id}_{resource}'
        return cache_key.format(
            obj_id=self.obj_id, filter_id=self._filter_id,
            resource=resource
        )

    @property
    def node_asset_key(self):
        return self.get_cache_key('NODES_WITH_ASSETS')

    @property
    def node_key(self):
        return self.get_cache_key('NODES')

    @property
    def asset_key(self):
        key = self.get_cache_key('ASSETS')
        return key

    @property
    def system_key(self):
        return self.get_cache_key('SYSTEM_USER')

    def get_resource_from_cache(self, resource):
        key_map = {
            "assets": self.asset_key,
            "nodes": self.node_key,
            "nodes_with_assets": self.node_asset_key,
            "system_users": self.system_key
        }
        key = key_map.get(resource)
        if not key:
            raise ValueError("Not a valid resource: {}".format(resource))
        cached = cache.get(key)
        if not cached:
            self.update_cache()
            cached = cache.get(key)
        return cached

    def get_resource(self, resource):
        if self._is_using_cache():
            return self.get_resource_from_cache(resource)
        elif self._is_refresh_cache():
            self.expire_cache()
            data = self.get_resource_from_cache(resource)
            return data
        else:
            return self.get_resource_without_cache(resource)

    def get_resource_without_cache(self, resource):
        attr = 'get_{}_without_cache'.format(resource)
        return getattr(self, attr)()

    def get_nodes_with_assets(self):
        return self.get_resource("nodes_with_assets")

    def get_assets(self):
        return self.get_resource("assets")

    def get_nodes(self):
        return self.get_resource("nodes")

    def get_system_users(self):
        return self.get_resource("system_users")

    def get_meta_cache_key(self):
        cache_key = self.CACHE_META_KEY_PREFIX + '{obj_id}_{filter_id}'
        key = cache_key.format(
            obj_id=self.obj_id, filter_id=self._filter_id
        )
        return key

    @property
    def cache_meta(self):
        key = self.get_meta_cache_key()
        meta = cache.get(key) or {}
        # print("Meta key: {}".format(key))
        # print("Meta id: {}".format(meta["id"]))
        return meta

    def update_cache(self):
        assets = self.get_resource_without_cache("assets")
        nodes_with_assets = self.get_resource_without_cache("nodes_with_assets")
        system_users = self.get_resource_without_cache("system_users")
        nodes = self.get_resource_without_cache("nodes")
        cache.set(self.asset_key, assets, self.CACHE_TIME)
        cache.set(self.node_asset_key, nodes_with_assets, self.CACHE_TIME)
        cache.set(self.system_key, system_users, self.CACHE_TIME)
        cache.set(self.node_key, nodes, self.CACHE_TIME)
        self.set_meta_to_cache()

    def set_meta_to_cache(self):
        key = self.get_meta_cache_key()
        meta = {
            'id': str(uuid.uuid4()),
            'datetime': timezone.now(),
            'object': str(self.object)
        }
        # print("Set meta key: {}".format(key))
        # print("set meta to cache: {}".format(meta["id"]))
        cache.set(key, meta, self.CACHE_TIME)

    def expire_cache_meta(self):
        cache_key = self.CACHE_META_KEY_PREFIX + '{obj_id}_*'
        key = cache_key.format(obj_id=self.obj_id)
        cache.delete_pattern(key)

    def expire_cache(self):
        """
        因为 获取用户的节点，资产，系统用户等都能会缓存，这里会清理所有与该对象有关的
        缓存，以免造成不统一的情况
        :return:
        """
        cache_key = self.CACHE_KEY_PREFIX + '{obj_id}_*'
        key = cache_key.format(obj_id='*')
        cache.delete_pattern(key)
        self.expire_cache_meta()

    @classmethod
    def expire_all_cache_meta(cls):
        key = cls.CACHE_META_KEY_PREFIX + '*'
        cache.delete_pattern(key)

    @classmethod
    def expire_all_cache(cls):
        key = cls.CACHE_KEY_PREFIX + '*'
        cache.delete_pattern(key)


class AssetPermissionUtil(AssetPermissionCacheMixin):
    get_permissions_map = {
        "User": get_user_permissions,
        "UserGroup": get_user_group_permissions,
        "Asset": get_asset_permissions,
        "Node": get_node_permissions,
        "SystemUser": get_system_user_permissions,
    }
    assets_only = (
        'id', 'hostname', 'ip', "platform", "domain_id",
        'comment', 'is_active', 'os', 'org_id', 'protocols'
    )

    def __init__(self, obj, cache_policy='0'):
        self.object = obj
        self.obj_id = str(obj.id)
        self._permissions = None
        self._permissions_id = None  # 标记_permission的唯一值
        self._assets = None
        self._filter_id = 'None'  # 当通过filter更改 permission是标记
        self.cache_policy = cache_policy
        self.tree = GenerateTree()
        self.change_org_if_need()
        self.nodes = None
        self._nodes = None
        self._assets_direct = None
        self._nodes_direct = None

    @staticmethod
    def change_org_if_need():
        set_to_root_org()

    @property
    def permissions(self):
        if self._permissions:
            return self._permissions
        object_cls = self.object.__class__.__name__
        func = self.get_permissions_map[object_cls]
        permissions = func(self.object)
        self._permissions = permissions
        return permissions

    @timeit
    def filter_permissions(self, **filters):
        filters_json = json.dumps(filters, sort_keys=True)
        self._permissions = self.permissions.filter(**filters)
        self._filter_id = md5(filters_json.encode()).hexdigest()

    @timeit
    def get_nodes_direct(self):
        """
        返回用户/组授权规则直接关联的节点
        :return: {node1: {system_user1: {'actions': set()},}}
        """
        if self._nodes_direct:
            return self._nodes_direct
        nodes_keys = defaultdict(lambda: defaultdict(int))
        for perm in self.permissions:
            actions = [perm.actions]
            system_users_ids = perm.system_users.all().values_list('id', flat=True)
            _nodes_keys = perm.nodes.all().values_list('key', flat=True)
            iterable = itertools.product(_nodes_keys, system_users_ids, actions)
            for node_key, sys_id, action in iterable:
                nodes_keys[node_key][sys_id] |= action
        self.tree.add_nodes(nodes_keys)
        self._nodes_direct = nodes_keys
        return nodes_keys

    def get_nodes_without_cache(self):
        self.get_assets_direct()
        return self.tree.get_nodes()

    @timeit
    def get_assets_direct(self):
        """
        返回用户授权规则直接关联的资产
        :return: {asset1: {system_user1: 1,}}
        """
        if self._assets_direct:
            return self._assets_direct
        assets_ids = defaultdict(lambda: defaultdict(int))
        for perm in self.permissions:
            actions = [perm.actions]
            _assets_ids = perm.assets.all().values_list('id', flat=True)
            system_users_ids = perm.system_users.all().values_list('id', flat=True)
            iterable = itertools.product(assets_ids, system_users_ids, actions)
            for asset_id, sys_id, action in iterable:
                assets_ids[asset_id][sys_id] |= action
        self.tree.add_assets(assets_ids)
        self._assets_direct = assets_ids
        return assets_ids

    @timeit
    def get_assets_without_cache(self):
        """
        :return: {asset1: set(system_user1,)}
        """
        if self._assets:
            return self._assets
        self.get_assets_direct()
        nodes_keys = self.get_nodes_direct()
        pattern = set()
        for key in nodes_keys:
            pattern.add(r'^{0}$|^{0}:'.format(key))
        pattern = '|'.join(list(pattern))
        print("Start get assets ids")
        clock = time.clock()
        if pattern:
            assets_ids = Asset.objects.filter(nodes__key__regex=pattern)\
                .values_list("id", flat=True).distinct()
        else:
            assets_ids = []
        print("get assetd ids, using: {}".format(time.clock()-clock))
        self.tree.add_assets_without_system_users(assets_ids)
        assets = self.tree.get_assets()
        self._assets = assets
        return assets

    @timeit
    def get_nodes_with_assets_without_cache(self):
        """
        返回节点并且包含资产
        {"node": {"asset": {"system_user": 1})}}
        :return:
        """
        self.get_assets_without_cache()
        nodes_assets = self.tree.get_nodes_with_assets()
        return nodes_assets

    def get_system_users_without_cache(self):
        system_users = set()
        permissions = self.permissions.prefetch_related('system_users')
        for perm in permissions:
            system_users.update(perm.system_users.all())
        return system_users


def is_obj_attr_has(obj, val, attrs=("hostname", "ip", "comment")):
    if not attrs:
        vals = [val for val in obj.__dict__.values() if isinstance(val, (str, int))]
    else:
        vals = [getattr(obj, attr) for attr in attrs if
                hasattr(obj, attr) and isinstance(hasattr(obj, attr), (str, int))]

    for v in vals:
        if str(v).find(val) != -1:
            return True
    return False


def sort_assets(assets, order_by='hostname', reverse=False):
    if order_by == 'ip':
        assets = sorted(assets, key=lambda asset: [int(d) for d in asset.ip.split('.') if d.isdigit()], reverse=reverse)
    else:
        assets = sorted(assets, key=lambda asset: getattr(asset, order_by), reverse=reverse)
    return assets


def parse_node_to_tree_node(node):
    name = '{} ({})'.format(node.value, node.assets_amount)
    data = {
        'id': node.key,
        'name': name,
        'title': name,
        'pId': node.parent_key,
        'isParent': True,
        'open': node.is_root(),
        'meta': {
            'node': {
                "id": node.id,
                "key": node.key,
                "value": node.value,
            },
            'type': 'node'
        }
    }
    tree_node = TreeNode(**data)
    return tree_node


def parse_asset_to_tree_node(node, asset, system_users):
    icon_skin = 'file'
    if asset.platform.lower() == 'windows':
        icon_skin = 'windows'
    elif asset.platform.lower() == 'linux':
        icon_skin = 'linux'
    _system_users = []
    for system_user, action in system_users.items():
        _system_users.append({
            'id': system_user.id,
            'name': system_user.name,
            'username': system_user.username,
            'protocol': system_user.protocol,
            'priority': system_user.priority,
            'login_mode': system_user.login_mode,
            'actions': [Action.value_to_choices(action)],
        })
    data = {
        'id': str(asset.id),
        'name': asset.hostname,
        'title': asset.ip,
        'pId': node.key,
        'isParent': False,
        'open': False,
        'iconSkin': icon_skin,
        'meta': {
            'system_users': _system_users,
            'type': 'asset',
            'asset': {
                'id': asset.id,
                'hostname': asset.hostname,
                'ip': asset.ip,
                'protocols': asset.protocols_as_list,
                'platform': asset.platform,
                'domain': None if not asset.domain else asset.domain.id,
                'is_active': asset.is_active,
                'comment': asset.comment
            },
        }
    }
    tree_node = TreeNode(**data)
    return tree_node
