# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock
from oslo_db.sqlalchemy import enginefacade
from six.moves import range

from nova import compute
from nova.compute import flavors
import nova.conf
from nova import context
from nova import db
from nova.db.sqlalchemy import models as sqa_models
from nova import exception
from nova import objects
from nova import quota
from nova import test
import nova.tests.unit.image.fake

CONF = nova.conf.CONF


def _get_fake_get_usages(updates=None):
    # These values are not realistic (they should all be 0) and are
    # only for testing that countable usages get included in the
    # results.
    usages = {'security_group_rules': {'in_use': 1},
              'key_pairs': {'in_use': 2},
              'server_group_members': {'in_use': 3},
              'floating_ips': {'in_use': 2},
              'instances': {'in_use': 2},
              'cores': {'in_use': 4},
              'ram': {'in_use': 10 * 1024}}
    if updates:
        usages.update(updates)

    def fake_get_usages(*a, **k):
        return usages

    return fake_get_usages


class QuotaIntegrationTestCase(test.TestCase):

    REQUIRES_LOCKING = True

    def setUp(self):
        super(QuotaIntegrationTestCase, self).setUp()
        self.flags(instances=2,
                   cores=4,
                   group='quota')

        self.user_id = 'admin'
        self.project_id = 'admin'
        self.context = context.RequestContext(self.user_id,
                                              self.project_id,
                                              is_admin=True)

        nova.tests.unit.image.fake.stub_out_image_service(self)

        self.compute_api = compute.API()

        def fake_validate_networks(context, requested_networks, num_instances):
            return num_instances

        # we aren't testing network quota in these tests when creating a server
        # so just mock that out and assume network (port) quota is OK
        self.compute_api.network_api.validate_networks = (
            mock.Mock(side_effect=fake_validate_networks))

    def tearDown(self):
        super(QuotaIntegrationTestCase, self).tearDown()
        nova.tests.unit.image.fake.FakeImageService_reset()

    def _create_instance(self, flavor_name='m1.large'):
        """Create a test instance."""
        inst = objects.Instance(context=self.context)
        inst.image_id = 'cedef40a-ed67-4d10-800e-17455edce175'
        inst.reservation_id = 'r-fakeres'
        inst.user_id = self.user_id
        inst.project_id = self.project_id
        inst.flavor = flavors.get_flavor_by_name(flavor_name)
        # This is needed for instance quota counting until we have the
        # ability to count allocations in placement.
        inst.vcpus = inst.flavor.vcpus
        inst.memory_mb = inst.flavor.memory_mb
        inst.create()
        return inst

    def test_too_many_instances(self):
        for i in range(CONF.quota.instances):
            self._create_instance()
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        try:
            self.compute_api.create(self.context, min_count=1, max_count=1,
                                    instance_type=inst_type,
                                    image_href=image_uuid)
        except exception.QuotaError as e:
            expected_kwargs = {'code': 413,
                               'req': '1, 1',
                               'used': '8, 2',
                               'allowed': '4, 2',
                               'overs': 'cores, instances'}
            self.assertEqual(expected_kwargs, e.kwargs)
        else:
            self.fail('Expected QuotaError exception')

    def test_too_many_cores(self):
        self._create_instance()
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        try:
            self.compute_api.create(self.context, min_count=1, max_count=1,
                                    instance_type=inst_type,
                                    image_href=image_uuid)
        except exception.QuotaError as e:
            expected_kwargs = {'code': 413,
                               'req': '1',
                               'used': '4',
                               'allowed': '4',
                               'overs': 'cores'}
            self.assertEqual(expected_kwargs, e.kwargs)
        else:
            self.fail('Expected QuotaError exception')

    def test_many_cores_with_unlimited_quota(self):
        # Setting cores quota to unlimited:
        self.flags(cores=-1, group='quota')
        # Default is 20 cores, so create 3x m1.xlarge with
        # 8 cores each.
        for i in range(3):
            self._create_instance(flavor_name='m1.xlarge')

    def test_too_many_addresses(self):
        # This test is specifically relying on nova-network.
        self.flags(use_neutron=False,
                   network_manager='nova.network.manager.FlatDHCPManager')
        self.flags(floating_ips=1, group='quota')
        # Apparently needed by the RPC tests...
        self.network = self.start_service('network',
                                          manager=CONF.network_manager)
        address = '192.168.0.100'
        db.floating_ip_create(context.get_admin_context(),
                              {'address': address,
                               'pool': 'nova',
                               'project_id': self.project_id})
        self.assertRaises(exception.QuotaError,
                          self.network.allocate_floating_ip,
                          self.context,
                          self.project_id)
        db.floating_ip_destroy(context.get_admin_context(), address)

    def test_auto_assigned(self):
        # This test is specifically relying on nova-network.
        self.flags(use_neutron=False,
                   network_manager='nova.network.manager.FlatDHCPManager')
        self.flags(floating_ips=1, group='quota')
        # Apparently needed by the RPC tests...
        self.network = self.start_service('network',
                                          manager=CONF.network_manager)
        address = '192.168.0.100'
        db.floating_ip_create(context.get_admin_context(),
                              {'address': address,
                               'pool': 'nova',
                               'project_id': self.project_id})
        # auto allocated addresses should not be counted
        self.assertRaises(exception.NoMoreFloatingIps,
                          self.network.allocate_floating_ip,
                          self.context,
                          self.project_id,
                          True)
        db.floating_ip_destroy(context.get_admin_context(), address)

    def test_too_many_metadata_items(self):
        metadata = {}
        for i in range(CONF.quota.metadata_items + 1):
            metadata['key%s' % i] = 'value%s' % i
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        self.assertRaises(exception.QuotaError, self.compute_api.create,
                                            self.context,
                                            min_count=1,
                                            max_count=1,
                                            instance_type=inst_type,
                                            image_href=image_uuid,
                                            metadata=metadata)

    def _create_with_injected_files(self, files):
        api = self.compute_api
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        api.create(self.context, min_count=1, max_count=1,
                instance_type=inst_type, image_href=image_uuid,
                injected_files=files)

    def test_no_injected_files(self):
        api = self.compute_api
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        api.create(self.context,
                   instance_type=inst_type,
                   image_href=image_uuid)

    def test_max_injected_files(self):
        files = []
        for i in range(CONF.quota.injected_files):
            files.append(('/my/path%d' % i, 'config = test\n'))
        self._create_with_injected_files(files)  # no QuotaError

    def test_too_many_injected_files(self):
        files = []
        for i in range(CONF.quota.injected_files + 1):
            files.append(('/my/path%d' % i, 'my\ncontent%d\n' % i))
        self.assertRaises(exception.QuotaError,
                          self._create_with_injected_files, files)

    def test_max_injected_file_content_bytes(self):
        max = CONF.quota.injected_file_content_bytes
        content = ''.join(['a' for i in range(max)])
        files = [('/test/path', content)]
        self._create_with_injected_files(files)  # no QuotaError

    def test_too_many_injected_file_content_bytes(self):
        max = CONF.quota.injected_file_content_bytes
        content = ''.join(['a' for i in range(max + 1)])
        files = [('/test/path', content)]
        self.assertRaises(exception.QuotaError,
                          self._create_with_injected_files, files)

    def test_max_injected_file_path_bytes(self):
        max = CONF.quota.injected_file_path_length
        path = ''.join(['a' for i in range(max)])
        files = [(path, 'config = quotatest')]
        self._create_with_injected_files(files)  # no QuotaError

    def test_too_many_injected_file_path_bytes(self):
        max = CONF.quota.injected_file_path_length
        path = ''.join(['a' for i in range(max + 1)])
        files = [(path, 'config = quotatest')]
        self.assertRaises(exception.QuotaError,
                          self._create_with_injected_files, files)


@enginefacade.transaction_context_provider
class FakeContext(context.RequestContext):
    def __init__(self, project_id, quota_class):
        super(FakeContext, self).__init__(project_id=project_id,
                                          quota_class=quota_class)
        self.is_admin = False
        self.user_id = 'fake_user'
        self.project_id = project_id
        self.quota_class = quota_class
        self.read_deleted = 'no'

    def elevated(self):
        elevated = self.__class__(self.project_id, self.quota_class)
        elevated.is_admin = True
        return elevated


class FakeDriver(object):
    def __init__(self, by_project=None, by_user=None, by_class=None,
                 reservations=None):
        self.called = []
        self.by_project = by_project or {}
        self.by_user = by_user or {}
        self.by_class = by_class or {}
        self.reservations = reservations or []

    def get_by_project_and_user(self, context, project_id, user_id, resource):
        self.called.append(('get_by_project_and_user',
                            context, project_id, user_id, resource))
        try:
            return self.by_user[user_id][resource]
        except KeyError:
            raise exception.ProjectUserQuotaNotFound(project_id=project_id,
                                                     user_id=user_id)

    def get_by_project(self, context, project_id, resource):
        self.called.append(('get_by_project', context, project_id, resource))
        try:
            return self.by_project[project_id][resource]
        except KeyError:
            raise exception.ProjectQuotaNotFound(project_id=project_id)

    def get_by_class(self, context, quota_class, resource):
        self.called.append(('get_by_class', context, quota_class, resource))
        try:
            return self.by_class[quota_class][resource]
        except KeyError:
            raise exception.QuotaClassNotFound(class_name=quota_class)

    def get_defaults(self, context, resources):
        self.called.append(('get_defaults', context, resources))
        return resources

    def get_class_quotas(self, context, resources, quota_class,
                         defaults=True):
        self.called.append(('get_class_quotas', context, resources,
                            quota_class, defaults))
        return resources

    def get_user_quotas(self, context, resources, project_id, user_id,
                        quota_class=None, defaults=True, usages=True):
        self.called.append(('get_user_quotas', context, resources,
                            project_id, user_id, quota_class, defaults,
                            usages))
        return resources

    def get_project_quotas(self, context, resources, project_id,
                           quota_class=None, defaults=True, usages=True,
                           remains=False):
        self.called.append(('get_project_quotas', context, resources,
                            project_id, quota_class, defaults, usages,
                            remains))
        return resources

    def limit_check(self, context, resources, values, project_id=None,
                    user_id=None):
        self.called.append(('limit_check', context, resources,
                            values, project_id, user_id))

    def limit_check_project_and_user(self, context, resources,
                                     project_values=None, user_values=None,
                                     project_id=None, user_id=None):
        self.called.append(('limit_check_project_and_user', context, resources,
                            project_values, user_values, project_id, user_id))

    def destroy_all_by_project_and_user(self, context, project_id, user_id):
        self.called.append(('destroy_all_by_project_and_user', context,
                            project_id, user_id))

    def destroy_all_by_project(self, context, project_id):
        self.called.append(('destroy_all_by_project', context, project_id))


class BaseResourceTestCase(test.TestCase):
    def test_no_flag(self):
        resource = quota.BaseResource('test_resource')

        self.assertEqual(resource.name, 'test_resource')
        self.assertIsNone(resource.flag)
        self.assertEqual(resource.default, -1)

    def test_with_flag(self):
        # We know this flag exists, so use it...
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')

        self.assertEqual(resource.name, 'test_resource')
        self.assertEqual(resource.flag, 'instances')
        self.assertEqual(resource.default, 10)

    def test_with_flag_no_quota(self):
        self.flags(instances=-1, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')

        self.assertEqual(resource.name, 'test_resource')
        self.assertEqual(resource.flag, 'instances')
        self.assertEqual(resource.default, -1)

    def test_quota_no_project_no_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver()
        context = FakeContext(None, None)
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 10)

    def test_quota_with_project_no_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=15),
                ))
        context = FakeContext('test_project', None)
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 15)

    def test_quota_no_project_with_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_class=dict(
                test_class=dict(test_resource=20),
                ))
        context = FakeContext(None, 'test_class')
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 20)

    def test_quota_with_project_with_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=15),
                ),
                            by_class=dict(
                test_class=dict(test_resource=20),
                ))
        context = FakeContext('test_project', 'test_class')
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 15)

    def test_quota_override_project_with_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=15),
                override_project=dict(test_resource=20),
                ))
        context = FakeContext('test_project', 'test_class')
        quota_value = resource.quota(driver, context,
                                     project_id='override_project')

        self.assertEqual(quota_value, 20)

    def test_quota_with_project_override_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_class=dict(
                test_class=dict(test_resource=15),
                override_class=dict(test_resource=20),
                ))
        context = FakeContext('test_project', 'test_class')
        quota_value = resource.quota(driver, context,
                                     quota_class='override_class')

        self.assertEqual(quota_value, 20)

    def test_valid_method_call_check_invalid_input(self):
        resources = {'dummy': 1}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'limit', quota.QUOTAS._resources)

    def test_valid_method_call_check_invalid_method(self):
        resources = {'key_pairs': 1}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'dummy', quota.QUOTAS._resources)

    def test_valid_method_call_check_multiple(self):
        resources = {'key_pairs': 1, 'dummy': 2}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'check', quota.QUOTAS._resources)

        resources = {'key_pairs': 1, 'instances': 2, 'dummy': 3}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'check', quota.QUOTAS._resources)

    def test_valid_method_call_check_wrong_method(self):
        resources = {'key_pairs': 1}
        engine_resources = {'key_pairs': quota.CountableResource('key_pairs',
                                                                 None,
                                                                 'key_pairs')}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'bogus', engine_resources)


class QuotaEngineTestCase(test.TestCase):
    def test_init(self):
        quota_obj = quota.QuotaEngine()

        self.assertEqual(quota_obj._resources, {})
        self.assertIsInstance(quota_obj._driver, quota.DbQuotaDriver)

    def test_init_override_string(self):
        quota_obj = quota.QuotaEngine(
            quota_driver_class='nova.tests.unit.test_quota.FakeDriver')

        self.assertEqual(quota_obj._resources, {})
        self.assertIsInstance(quota_obj._driver, FakeDriver)

    def test_init_override_obj(self):
        quota_obj = quota.QuotaEngine(quota_driver_class=FakeDriver)

        self.assertEqual(quota_obj._resources, {})
        self.assertEqual(quota_obj._driver, FakeDriver)

    def test_register_resource(self):
        quota_obj = quota.QuotaEngine()
        resource = quota.AbsoluteResource('test_resource')
        quota_obj.register_resource(resource)

        self.assertEqual(quota_obj._resources, dict(test_resource=resource))

    def test_register_resources(self):
        quota_obj = quota.QuotaEngine()
        resources = [
            quota.AbsoluteResource('test_resource1'),
            quota.AbsoluteResource('test_resource2'),
            quota.AbsoluteResource('test_resource3'),
            ]
        quota_obj.register_resources(resources)

        self.assertEqual(quota_obj._resources, dict(
                test_resource1=resources[0],
                test_resource2=resources[1],
                test_resource3=resources[2],
                ))

    def test_get_by_project_and_user(self):
        context = FakeContext('test_project', 'test_class')
        driver = FakeDriver(by_user=dict(
                fake_user=dict(test_resource=42)))
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        result = quota_obj.get_by_project_and_user(context, 'test_project',
                                       'fake_user', 'test_resource')

        self.assertEqual(driver.called, [
                ('get_by_project_and_user', context, 'test_project',
                 'fake_user', 'test_resource'),
                ])
        self.assertEqual(result, 42)

    def test_get_by_project(self):
        context = FakeContext('test_project', 'test_class')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=42)))
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        result = quota_obj.get_by_project(context, 'test_project',
                                          'test_resource')

        self.assertEqual(driver.called, [
                ('get_by_project', context, 'test_project', 'test_resource'),
                ])
        self.assertEqual(result, 42)

    def test_get_by_class(self):
        context = FakeContext('test_project', 'test_class')
        driver = FakeDriver(by_class=dict(
                test_class=dict(test_resource=42)))
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        result = quota_obj.get_by_class(context, 'test_class', 'test_resource')

        self.assertEqual(driver.called, [
                ('get_by_class', context, 'test_class', 'test_resource'),
                ])
        self.assertEqual(result, 42)

    def _make_quota_obj(self, driver):
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        resources = [
            quota.AbsoluteResource('test_resource4'),
            quota.AbsoluteResource('test_resource3'),
            quota.AbsoluteResource('test_resource2'),
            quota.AbsoluteResource('test_resource1'),
            ]
        quota_obj.register_resources(resources)

        return quota_obj

    def test_get_defaults(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result = quota_obj.get_defaults(context)

        self.assertEqual(driver.called, [
                ('get_defaults', context, quota_obj._resources),
                ])
        self.assertEqual(result, quota_obj._resources)

    def test_get_class_quotas(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.get_class_quotas(context, 'test_class')
        result2 = quota_obj.get_class_quotas(context, 'test_class', False)

        self.assertEqual(driver.called, [
                ('get_class_quotas', context, quota_obj._resources,
                 'test_class', True),
                ('get_class_quotas', context, quota_obj._resources,
                 'test_class', False),
                ])
        self.assertEqual(result1, quota_obj._resources)
        self.assertEqual(result2, quota_obj._resources)

    def test_get_user_quotas(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.get_user_quotas(context, 'test_project',
                                            'fake_user')
        result2 = quota_obj.get_user_quotas(context, 'test_project',
                                            'fake_user',
                                            quota_class='test_class',
                                            defaults=False,
                                            usages=False)

        self.assertEqual(driver.called, [
                ('get_user_quotas', context, quota_obj._resources,
                 'test_project', 'fake_user', None, True, True),
                ('get_user_quotas', context, quota_obj._resources,
                 'test_project', 'fake_user', 'test_class', False, False),
                ])
        self.assertEqual(result1, quota_obj._resources)
        self.assertEqual(result2, quota_obj._resources)

    def test_get_project_quotas(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.get_project_quotas(context, 'test_project')
        result2 = quota_obj.get_project_quotas(context, 'test_project',
                                               quota_class='test_class',
                                               defaults=False,
                                               usages=False)

        self.assertEqual(driver.called, [
                ('get_project_quotas', context, quota_obj._resources,
                 'test_project', None, True, True, False),
                ('get_project_quotas', context, quota_obj._resources,
                 'test_project', 'test_class', False, False, False),
                ])
        self.assertEqual(result1, quota_obj._resources)
        self.assertEqual(result2, quota_obj._resources)

    def test_count_as_dict_no_resource(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        self.assertRaises(exception.QuotaResourceUnknown,
                          quota_obj.count_as_dict, context, 'test_resource5',
                          True, foo='bar')

    def test_count_as_dict_wrong_resource(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        self.assertRaises(exception.QuotaResourceUnknown,
                          quota_obj.count_as_dict, context, 'test_resource1',
                          True, foo='bar')

    def test_count_as_dict(self):
        def fake_count_as_dict(context, *args, **kwargs):
            self.assertEqual(args, (True,))
            self.assertEqual(kwargs, dict(foo='bar'))
            return {'project': {'test_resource5': 5}}

        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.register_resource(
            quota.CountableResource('test_resource5', fake_count_as_dict))
        result = quota_obj.count_as_dict(context, 'test_resource5', True,
                                         foo='bar')

        self.assertEqual({'project': {'test_resource5': 5}}, result)

    def test_limit_check(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.limit_check(context, test_resource1=4, test_resource2=3,
                              test_resource3=2, test_resource4=1)

        self.assertEqual(driver.called, [
                ('limit_check', context, quota_obj._resources, dict(
                        test_resource1=4,
                        test_resource2=3,
                        test_resource3=2,
                        test_resource4=1,
                        ), None, None),
                ])

    def test_limit_check_project_and_user(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        project_values = dict(test_resource1=4, test_resource2=3)
        user_values = dict(test_resource3=2, test_resource4=1)
        quota_obj.limit_check_project_and_user(context,
                                               project_values=project_values,
                                               user_values=user_values)

        self.assertEqual([('limit_check_project_and_user', context,
                          quota_obj._resources,
                          dict(test_resource1=4, test_resource2=3),
                          dict(test_resource3=2, test_resource4=1),
                          None, None)],
                         driver.called)

    def test_destroy_all_by_project_and_user(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.destroy_all_by_project_and_user(context,
                                                  'test_project', 'fake_user')

        self.assertEqual(driver.called, [
                ('destroy_all_by_project_and_user', context, 'test_project',
                 'fake_user'),
                ])

    def test_destroy_all_by_project(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.destroy_all_by_project(context, 'test_project')

        self.assertEqual(driver.called, [
                ('destroy_all_by_project', context, 'test_project'),
                ])

    def test_resources(self):
        quota_obj = self._make_quota_obj(None)

        self.assertEqual(quota_obj.resources,
                         ['test_resource1', 'test_resource2',
                          'test_resource3', 'test_resource4'])


class DbQuotaDriverTestCase(test.TestCase):
    def setUp(self):
        super(DbQuotaDriverTestCase, self).setUp()

        self.flags(instances=10,
                   cores=20,
                   ram=50 * 1024,
                   floating_ips=10,
                   fixed_ips=10,
                   metadata_items=128,
                   injected_files=5,
                   injected_file_content_bytes=10 * 1024,
                   injected_file_path_length=255,
                   security_groups=10,
                   security_group_rules=20,
                   server_groups=10,
                   server_group_members=10,
                   reservation_expire=86400,
                   until_refresh=0,
                   max_age=0,
                   group='quota'
                   )

        self.driver = quota.DbQuotaDriver()

        self.calls = []

        self.useFixture(test.TimeOverride())

    def test_get_defaults(self):
        # Use our pre-defined resources
        self._stub_quota_class_get_default()
        result = self.driver.get_defaults(None, quota.QUOTAS._resources)

        self.assertEqual(result, dict(
                instances=5,
                cores=20,
                ram=25 * 1024,
                floating_ips=10,
                fixed_ips=10,
                metadata_items=64,
                injected_files=5,
                injected_file_content_bytes=5 * 1024,
                injected_file_path_bytes=255,
                security_groups=10,
                security_group_rules=20,
                key_pairs=100,
                server_groups=10,
                server_group_members=10,
                ))

    def _stub_quota_class_get_default(self):
        # Stub out quota_class_get_default
        def fake_qcgd(cls, context):
            self.calls.append('quota_class_get_default')
            return dict(
                instances=5,
                ram=25 * 1024,
                metadata_items=64,
                injected_file_content_bytes=5 * 1024,
                )
        self.stub_out('nova.objects.Quotas.get_default_class', fake_qcgd)

    def _stub_quota_class_get_all_by_name(self):
        # Stub out quota_class_get_all_by_name
        def fake_qcgabn(cls, context, quota_class):
            self.calls.append('quota_class_get_all_by_name')
            self.assertEqual(quota_class, 'test_class')
            return dict(
                instances=5,
                ram=25 * 1024,
                metadata_items=64,
                injected_file_content_bytes=5 * 1024,
                )
        self.stub_out('nova.objects.Quotas.get_all_class_by_name', fake_qcgabn)

    def test_get_class_quotas(self):
        self._stub_quota_class_get_all_by_name()
        result = self.driver.get_class_quotas(None, quota.QUOTAS._resources,
                                              'test_class')

        self.assertEqual(self.calls, ['quota_class_get_all_by_name'])
        self.assertEqual(result, dict(
                instances=5,
                cores=20,
                ram=25 * 1024,
                floating_ips=10,
                fixed_ips=10,
                metadata_items=64,
                injected_files=5,
                injected_file_content_bytes=5 * 1024,
                injected_file_path_bytes=255,
                security_groups=10,
                security_group_rules=20,
                key_pairs=100,
                server_groups=10,
                server_group_members=10,
                ))

    def test_get_class_quotas_no_defaults(self):
        self._stub_quota_class_get_all_by_name()
        result = self.driver.get_class_quotas(None, quota.QUOTAS._resources,
                                              'test_class', False)

        self.assertEqual(self.calls, ['quota_class_get_all_by_name'])
        self.assertEqual(result, dict(
                instances=5,
                ram=25 * 1024,
                metadata_items=64,
                injected_file_content_bytes=5 * 1024,
                ))

    def _stub_get_by_project_and_user(self):
        def fake_qgabpau(context, project_id, user_id):
            self.calls.append('quota_get_all_by_project_and_user')
            self.assertEqual(project_id, 'test_project')
            self.assertEqual(user_id, 'fake_user')
            return dict(
                cores=10,
                injected_files=2,
                injected_file_path_bytes=127,
                )

        def fake_qgabp(context, project_id):
            self.calls.append('quota_get_all_by_project')
            self.assertEqual(project_id, 'test_project')
            return {
                'cores': 10,
                'injected_files': 2,
                'injected_file_path_bytes': 127,
                }

        self.stub_out('nova.db.quota_get_all_by_project_and_user',
                       fake_qgabpau)
        self.stub_out('nova.db.quota_get_all_by_project', fake_qgabp)

        self._stub_quota_class_get_all_by_name()

    def _get_fake_countable_resources(self):
        # Create several countable resources with fake count functions
        def fake_instances_cores_ram_count(*a, **k):
            return {'project': {'instances': 2, 'cores': 4, 'ram': 1024},
                    'user': {'instances': 1, 'cores': 2, 'ram': 512}}

        def fake_security_group_count(*a, **k):
            return {'project': {'security_groups': 2},
                    'user': {'security_groups': 1}}

        def fake_server_group_count(*a, **k):
            return {'project': {'server_groups': 5},
                    'user': {'server_groups': 3}}

        resources = {}
        resources['key_pairs'] = quota.CountableResource(
            'key_pairs', lambda *a, **k: {'user': {'key_pairs': 1}},
            'key_pairs')
        resources['instances'] = quota.CountableResource(
            'instances', fake_instances_cores_ram_count, 'instances')
        resources['cores'] = quota.CountableResource(
            'cores', fake_instances_cores_ram_count, 'cores')
        resources['ram'] = quota.CountableResource(
            'ram', fake_instances_cores_ram_count, 'ram')
        resources['security_groups'] = quota.CountableResource(
            'security_groups', fake_security_group_count, 'security_groups')
        resources['floating_ips'] = quota.CountableResource(
            'floating_ips', lambda *a, **k: {'project': {'floating_ips': 4}},
            'floating_ips')
        resources['fixed_ips'] = quota.CountableResource(
            'fixed_ips', lambda *a, **k: {'project': {'fixed_ips': 5}},
            'fixed_ips')
        resources['server_groups'] = quota.CountableResource(
            'server_groups', fake_server_group_count, 'server_groups')
        resources['server_group_members'] = quota.CountableResource(
            'server_group_members',
            lambda *a, **k: {'user': {'server_group_members': 7}},
            'server_group_members')
        resources['security_group_rules'] = quota.CountableResource(
            'security_group_rules',
            lambda *a, **k: {'project': {'security_group_rules': 8}},
            'security_group_rules')
        return resources

    def test_get_usages_for_project(self):
        resources = self._get_fake_countable_resources()
        actual = self.driver._get_usages(
            FakeContext('test_project', 'test_class'), resources,
            'test_project')
        # key_pairs, server_group_members, and security_group_rules are never
        # counted as a usage. Their counts are only for quota limit checking.
        expected = {'key_pairs': {'in_use': 0},
                    'instances': {'in_use': 2},
                    'cores': {'in_use': 4},
                    'ram': {'in_use': 1024},
                    'security_groups': {'in_use': 2},
                    'floating_ips': {'in_use': 4},
                    'fixed_ips': {'in_use': 5},
                    'server_groups': {'in_use': 5},
                    'server_group_members': {'in_use': 0},
                    'security_group_rules': {'in_use': 0}}
        self.assertEqual(expected, actual)

    def test_get_usages_for_user(self):
        resources = self._get_fake_countable_resources()
        actual = self.driver._get_usages(
            FakeContext('test_project', 'test_class'), resources,
            'test_project', user_id='fake_user')
        # key_pairs, server_group_members, and security_group_rules are never
        # counted as a usage. Their counts are only for quota limit checking.
        expected = {'key_pairs': {'in_use': 0},
                    'instances': {'in_use': 1},
                    'cores': {'in_use': 2},
                    'ram': {'in_use': 512},
                    'security_groups': {'in_use': 1},
                    'floating_ips': {'in_use': 4},
                    'fixed_ips': {'in_use': 5},
                    'server_groups': {'in_use': 3},
                    'server_group_members': {'in_use': 0},
                    'security_group_rules': {'in_use': 0}}
        self.assertEqual(expected, actual)

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
               floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    def _stub_get_by_project_and_user_specific(self):
        def fake_quota_get(context, project_id, resource, user_id=None):
            self.calls.append('quota_get')
            self.assertEqual(project_id, 'test_project')
            self.assertEqual(user_id, 'fake_user')
            self.assertEqual(resource, 'test_resource')
            return dict(
                test_resource=dict(in_use=20, reserved=10),
                )
        self.stub_out('nova.db.quota_get', fake_quota_get)

    def test_get_by_project_and_user(self):
        self._stub_get_by_project_and_user_specific()
        result = self.driver.get_by_project_and_user(
            FakeContext('test_project', 'test_class'),
            'test_project', 'fake_user', 'test_resource')

        self.assertEqual(self.calls, ['quota_get'])
        self.assertEqual(result, dict(
            test_resource=dict(in_use=20, reserved=10),
            ))

    def _stub_get_by_project(self):
        def fake_qgabp(context, project_id):
            self.calls.append('quota_get_all_by_project')
            self.assertEqual(project_id, 'test_project')
            return dict(
                cores=10,
                injected_files=2,
                injected_file_path_bytes=127,
                )

        def fake_quota_get_all(context, project_id):
            self.calls.append('quota_get_all')
            self.assertEqual(project_id, 'test_project')
            return [sqa_models.ProjectUserQuota(resource='instances',
                                                hard_limit=5),
                    sqa_models.ProjectUserQuota(resource='cores',
                                                hard_limit=2)]

        self.stub_out('nova.db.quota_get_all_by_project', fake_qgabp)
        self.stub_out('nova.db.quota_get_all', fake_quota_get_all)

        self._stub_quota_class_get_all_by_name()
        self._stub_quota_class_get_default()

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
               floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_with_remains(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', remains=True)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                'quota_get_all',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=0,
                    remains=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    remains=8,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    remains=25 * 1024,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    remains=10,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    remains=10,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    remains=64,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    remains=2,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    remains=5 * 1024,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    remains=127,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    remains=10,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    remains=20,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    remains=100,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    remains=10,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    remains=10,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas_alt_context_no_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('other_project', None)
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
                ram=dict(
                    limit=50 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=128,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=10 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_alt_context_no_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('other_project', None)
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
               floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas_alt_context_with_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('other_project', 'other_class')
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user',
            quota_class='test_class')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_alt_context_with_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('other_project', 'other_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project',
            quota_class='test_class')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas_no_defaults(self, mock_get_usages):
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user',
            defaults=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
               injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_no_defaults(self, mock_get_usages):
        self._stub_get_by_project()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', defaults=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=0,
                    ),
               injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                ))

    def test_get_user_quotas_no_usages(self):
        self._stub_get_by_project_and_user()
        result = self.driver.get_user_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', 'fake_user', usages=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                ])
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    ),
                cores=dict(
                    limit=10,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    ),
                floating_ips=dict(
                    limit=10,
                    ),
                fixed_ips=dict(
                    limit=10,
                    ),
                metadata_items=dict(
                    limit=64,
                    ),
                injected_files=dict(
                    limit=2,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    ),
                security_groups=dict(
                    limit=10,
                    ),
                security_group_rules=dict(
                    limit=20,
                    ),
                key_pairs=dict(
                    limit=100,
                    ),
                server_groups=dict(
                    limit=10,
                    ),
                server_group_members=dict(
                    limit=10,
                    ),
                ))

    def test_get_project_quotas_no_usages(self):
        self._stub_get_by_project()
        result = self.driver.get_project_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', usages=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    ),
                cores=dict(
                    limit=10,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    ),
                floating_ips=dict(
                    limit=10,
                    ),
                fixed_ips=dict(
                    limit=10,
                    ),
                metadata_items=dict(
                    limit=64,
                    ),
                injected_files=dict(
                    limit=2,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    ),
                security_groups=dict(
                    limit=10,
                    ),
                security_group_rules=dict(
                    limit=20,
                    ),
                key_pairs=dict(
                    limit=100,
                    ),
                server_groups=dict(
                    limit=10,
                    ),
                server_group_members=dict(
                    limit=10,
                    ),
                ))

    def _stub_get_settable_quotas(self):

        def fake_quota_get_all_by_project(context, project_id):
            self.calls.append('quota_get_all_by_project')
            return {'floating_ips': 20}

        def fake_get_project_quotas(dbdrv, context, resources, project_id,
                                    quota_class=None, defaults=True,
                                    usages=True, remains=False,
                                    project_quotas=None):
            self.calls.append('get_project_quotas')
            result = {}
            for k, v in resources.items():
                limit = v.default
                if k == 'instances':
                    remains = v.default - 5
                    in_use = 1
                elif k == 'cores':
                    remains = -1
                    in_use = 5
                    limit = -1
                elif k == 'floating_ips':
                    remains = 20
                    in_use = 0
                    limit = 20
                else:
                    remains = v.default
                    in_use = 0
                result[k] = {'limit': limit, 'in_use': in_use,
                             'remains': remains}
            return result

        def fake_process_quotas_in_get_user_quotas(dbdrv, context, resources,
                                                   project_id, quotas,
                                                   quota_class=None,
                                                   defaults=True, usages=None,
                                                   remains=False):
            self.calls.append('_process_quotas')
            result = {}
            for k, v in resources.items():
                if k == 'instances':
                    in_use = 1
                elif k == 'cores':
                    in_use = 15
                else:
                    in_use = 0
                result[k] = {'limit': v.default,
                             'in_use': in_use}
            return result

        def fake_qgabpau(context, project_id, user_id):
            self.calls.append('quota_get_all_by_project_and_user')
            return {'instances': 2, 'cores': -1}

        self.stub_out('nova.db.quota_get_all_by_project',
                       fake_quota_get_all_by_project)
        self.stub_out('nova.quota.DbQuotaDriver.get_project_quotas',
                       fake_get_project_quotas)
        self.stub_out('nova.quota.DbQuotaDriver._process_quotas',
                       fake_process_quotas_in_get_user_quotas)
        self.stub_out('nova.db.quota_get_all_by_project_and_user',
                       fake_qgabpau)

    def test_get_settable_quotas_with_user(self):
        self._stub_get_settable_quotas()
        result = self.driver.get_settable_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', user_id='test_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'get_project_quotas',
                'quota_get_all_by_project_and_user',
                '_process_quotas',
                ])
        self.assertEqual(result, {
                'instances': {
                    'minimum': 1,
                    'maximum': 7,
                    },
                'cores': {
                    'minimum': 15,
                    'maximum': -1,
                    },
                'ram': {
                    'minimum': 0,
                    'maximum': 50 * 1024,
                    },
                'floating_ips': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'fixed_ips': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'metadata_items': {
                    'minimum': 0,
                    'maximum': 128,
                    },
                'injected_files': {
                    'minimum': 0,
                    'maximum': 5,
                    },
                'injected_file_content_bytes': {
                    'minimum': 0,
                    'maximum': 10 * 1024,
                    },
                'injected_file_path_bytes': {
                    'minimum': 0,
                    'maximum': 255,
                    },
                'security_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'security_group_rules': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'key_pairs': {
                    'minimum': 0,
                    'maximum': 100,
                    },
                'server_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'server_group_members': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                })

    def test_get_settable_quotas_without_user(self):
        self._stub_get_settable_quotas()
        result = self.driver.get_settable_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'get_project_quotas',
                ])
        self.assertEqual(result, {
                'instances': {
                    'minimum': 5,
                    'maximum': -1,
                    },
                'cores': {
                    'minimum': 5,
                    'maximum': -1,
                    },
                'ram': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'floating_ips': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'fixed_ips': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'metadata_items': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'injected_files': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'injected_file_content_bytes': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'injected_file_path_bytes': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'security_groups': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'security_group_rules': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'key_pairs': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'server_groups': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'server_group_members': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                })

    def test_get_settable_quotas_by_user_with_unlimited_value(self):
        self._stub_get_settable_quotas()
        result = self.driver.get_settable_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', user_id='test_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'get_project_quotas',
                'quota_get_all_by_project_and_user',
                '_process_quotas',
                ])
        self.assertEqual(result, {
                'instances': {
                    'minimum': 1,
                    'maximum': 7,
                    },
                'cores': {
                    'minimum': 15,
                    'maximum': -1,
                    },
                'ram': {
                    'minimum': 0,
                    'maximum': 50 * 1024,
                    },
                'floating_ips': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'fixed_ips': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'metadata_items': {
                    'minimum': 0,
                    'maximum': 128,
                    },
                'injected_files': {
                    'minimum': 0,
                    'maximum': 5,
                    },
                'injected_file_content_bytes': {
                    'minimum': 0,
                    'maximum': 10 * 1024,
                    },
                'injected_file_path_bytes': {
                    'minimum': 0,
                    'maximum': 255,
                    },
                'security_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'security_group_rules': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'key_pairs': {
                    'minimum': 0,
                    'maximum': 100,
                    },
                'server_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'server_group_members': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                })

    def _stub_get_project_quotas(self):
        def fake_get_project_quotas(dbdrv, context, resources, project_id,
                                    quota_class=None, defaults=True,
                                    usages=True, remains=False,
                                    project_quotas=None):
            self.calls.append('get_project_quotas')
            return {k: dict(limit=v.default) for k, v in resources.items()}

        self.stub_out('nova.quota.DbQuotaDriver.get_project_quotas',
                       fake_get_project_quotas)

    def test_get_quotas_unknown(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.QuotaResourceUnknown,
                          self.driver._get_quotas,
                          None, quota.QUOTAS._resources,
                          ['unknown'])
        self.assertEqual(self.calls, [])

    def test_limit_check_under(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.InvalidQuotaValue,
                          self.driver.limit_check,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(metadata_items=-1))

    def test_limit_check_over(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(metadata_items=129))

    def test_limit_check_project_overs(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(injected_file_content_bytes=10241,
                               injected_file_path_bytes=256))

    def test_limit_check_unlimited(self):
        self.flags(metadata_items=-1, group='quota')
        self._stub_get_project_quotas()
        self.driver.limit_check(FakeContext('test_project', 'test_class'),
                                quota.QUOTAS._resources,
                                dict(metadata_items=32767))

    def test_limit_check(self):
        self._stub_get_project_quotas()
        self.driver.limit_check(FakeContext('test_project', 'test_class'),
                                quota.QUOTAS._resources,
                                dict(metadata_items=128))

    def test_limit_check_project_and_user_no_values(self):
        self.assertRaises(exception.Invalid,
                          self.driver.limit_check_project_and_user,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources)

    def test_limit_check_project_and_user_under(self):
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': -1}},
                  {'user_values': {'key_pairs': -1}},
                  {'project_values': {'instances': -1},
                   'user_values': {'instances': -1}}]
        for kwarg in kwargs:
            self.assertRaises(exception.InvalidQuotaValue,
                              self.driver.limit_check_project_and_user,
                              ctxt, resources, **kwarg)

    def test_limit_check_project_and_user_over_project(self):
        # Check the case where user_values pass user quota but project_values
        # exceed project quota.
        self.flags(instances=5, group='quota')
        self._stub_get_project_quotas()
        resources = self._get_fake_countable_resources()
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check_project_and_user,
                          FakeContext('test_project', 'test_class'),
                          resources,
                          project_values=dict(instances=6),
                          user_values=dict(instances=5))

    def test_limit_check_project_and_user_over_user(self):
        self.flags(instances=5, group='quota')
        self._stub_get_project_quotas()
        resources = self._get_fake_countable_resources()
        # It's not realistic for user_values to be higher than project_values,
        # but this is just for testing the fictional case where project_values
        # pass project quota but user_values exceed user quota.
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check_project_and_user,
                          FakeContext('test_project', 'test_class'),
                          resources,
                          project_values=dict(instances=5),
                          user_values=dict(instances=6))

    def test_limit_check_project_and_user_overs(self):
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 10241}},
                  {'user_values': {'key_pairs': 256}},
                  {'project_values': {'instances': 512},
                   'user_values': {'instances': 256}}]
        for kwarg in kwargs:
            self.assertRaises(exception.OverQuota,
                              self.driver.limit_check_project_and_user,
                              ctxt, resources, **kwarg)

    def test_limit_check_project_and_user_unlimited(self):
        self.flags(fixed_ips=-1, group='quota')
        self.flags(key_pairs=-1, group='quota')
        self.flags(instances=-1, group='quota')
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 32767}},
                  {'user_values': {'key_pairs': 32767}},
                  {'project_values': {'instances': 32767},
                   'user_values': {'instances': 32767}}]
        for kwarg in kwargs:
            self.driver.limit_check_project_and_user(ctxt, resources, **kwarg)

    def test_limit_check_project_and_user(self):
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 5}},
                  {'user_values': {'key_pairs': 5}},
                  {'project_values': {'instances': 5},
                   'user_values': {'instances': 5}}]
        for kwarg in kwargs:
            self.driver.limit_check_project_and_user(ctxt, resources, **kwarg)

    def test_limit_check_project_and_user_zero_values(self):
        """Tests to make sure that we don't compare 0 to None and fail with
        a TypeError in python 3 when calculating merged_values between
        project_values and user_values.
        """
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 0}},
                  {'user_values': {'key_pairs': 0}},
                  {'project_values': {'instances': 0},
                   'user_values': {'instances': 0}}]
        for kwarg in kwargs:
            self.driver.limit_check_project_and_user(ctxt, resources, **kwarg)


class NoopQuotaDriverTestCase(test.TestCase):
    def setUp(self):
        super(NoopQuotaDriverTestCase, self).setUp()

        self.flags(instances=10,
                   cores=20,
                   ram=50 * 1024,
                   floating_ips=10,
                   metadata_items=128,
                   injected_files=5,
                   injected_file_content_bytes=10 * 1024,
                   injected_file_path_length=255,
                   security_groups=10,
                   security_group_rules=20,
                   reservation_expire=86400,
                   until_refresh=0,
                   max_age=0,
                   group='quota'
                   )

        self.expected_with_usages = {}
        self.expected_without_usages = {}
        self.expected_without_dict = {}
        self.expected_settable_quotas = {}
        for r in quota.QUOTAS._resources:
            self.expected_with_usages[r] = dict(limit=-1,
                                                in_use=-1,
                                                reserved=-1)
            self.expected_without_usages[r] = dict(limit=-1)
            self.expected_without_dict[r] = -1
            self.expected_settable_quotas[r] = dict(minimum=0, maximum=-1)

        self.driver = quota.NoopQuotaDriver()

    def test_get_defaults(self):
        # Use our pre-defined resources
        result = self.driver.get_defaults(None, quota.QUOTAS._resources)
        self.assertEqual(self.expected_without_dict, result)

    def test_get_class_quotas(self):
        result = self.driver.get_class_quotas(None,
                                              quota.QUOTAS._resources,
                                              'test_class')
        self.assertEqual(self.expected_without_dict, result)

    def test_get_class_quotas_no_defaults(self):
        result = self.driver.get_class_quotas(None,
                                              quota.QUOTAS._resources,
                                              'test_class',
                                              False)
        self.assertEqual(self.expected_without_dict, result)

    def test_get_project_quotas(self):
        result = self.driver.get_project_quotas(None,
                                                quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(self.expected_with_usages, result)

    def test_get_user_quotas(self):
        result = self.driver.get_user_quotas(None,
                                             quota.QUOTAS._resources,
                                             'test_project',
                                             'fake_user')
        self.assertEqual(self.expected_with_usages, result)

    def test_get_project_quotas_no_defaults(self):
        result = self.driver.get_project_quotas(None,
                                                quota.QUOTAS._resources,
                                                'test_project',
                                                defaults=False)
        self.assertEqual(self.expected_with_usages, result)

    def test_get_user_quotas_no_defaults(self):
        result = self.driver.get_user_quotas(None,
                                             quota.QUOTAS._resources,
                                             'test_project',
                                             'fake_user',
                                             defaults=False)
        self.assertEqual(self.expected_with_usages, result)

    def test_get_project_quotas_no_usages(self):
        result = self.driver.get_project_quotas(None,
                                                quota.QUOTAS._resources,
                                                'test_project',
                                                usages=False)
        self.assertEqual(self.expected_without_usages, result)

    def test_get_user_quotas_no_usages(self):
        result = self.driver.get_user_quotas(None,
                                             quota.QUOTAS._resources,
                                             'test_project',
                                             'fake_user',
                                             usages=False)
        self.assertEqual(self.expected_without_usages, result)

    def test_get_settable_quotas_with_user(self):
        result = self.driver.get_settable_quotas(None,
                                                 quota.QUOTAS._resources,
                                                 'test_project',
                                                 'fake_user')
        self.assertEqual(self.expected_settable_quotas, result)

    def test_get_settable_quotas_without_user(self):
        result = self.driver.get_settable_quotas(None,
                                                 quota.QUOTAS._resources,
                                                 'test_project')
        self.assertEqual(self.expected_settable_quotas, result)
