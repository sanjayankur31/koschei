# Copyright (C) 2014-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Author: Michael Simacek <msimacek@redhat.com>
# Author: Mikolaj Izdebski <mizdebsk@redhat.com>

import os
import sys
import unittest
import shutil
import json
import psycopg2
import rpm
import vcr
import contextlib

from mock import Mock, patch
from datetime import datetime
from functools import wraps

from test import testdir, config, koji_vcr
from koschei import plugin
from koschei.config import get_config
from koschei.db import get_engine, create_all, Base, get_or_create
from koschei.models import (
    Package, Build, Collection, BasePackage,
    PackageGroupRelation, PackageGroup, GroupACL, User,
    KojiTask, Dependency, AppliedChange, LogEntry,
    ResolutionChange, ResolutionProblem
)
from koschei.backend import KoscheiBackendSession, repo_util, service, koji_util

workdir = '.workdir'

my_vcr = vcr.VCR(
    cassette_library_dir=os.path.join(testdir, 'data'),
    serializer='json',
    path_transformer=vcr.VCR.ensure_suffix('.vcr.json'),
)


class DummyKoji(object):
    """
    Dummy Koji Session for tests that don't need to access Koji, but still need a session.
    """
    def __init__(self, koji_id):
        self.koji_id = koji_id

    def __getattr__(self, key):
        assert False, "Unexpected access to Koji"


class AbstractTest(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(AbstractTest, self).__init__(*args, **kwargs)
        os.chdir(testdir)
        if 'http_proxy' in os.environ:
            del os.environ['http_proxy']
        self.oldpwd = os.getcwd()

    def koji(self, koji_id):
        """
        Returns a Koji session for particular `koji_id`, like the `koji` method of
        KoscheiBackendSession. If the session is expected to be used, it needs to be
        setup first, using `koji_cassette` method or `with_koji_cassette` decorator
        function.

        :param koji_id: 'primary' or 'secondary'
        """
        return DummyKoji(koji_id)

    def _rm_workdir(self):
        try:
            shutil.rmtree(workdir)
        except OSError:
            pass

    def setUp(self):
        self._rm_workdir()
        os.mkdir(workdir)
        os.chdir(workdir)

    def tearDown(self):
        os.chdir(testdir)
        self._rm_workdir()

    @staticmethod
    def get_json_data(name):
        with open(os.path.join(testdir, 'data', name)) as fo:
            return json.load(fo)

    @contextlib.contextmanager
    def koji_cassette(self, *cassettes, secondary_mode=False):
        """
        Creates a mock Koji session(s) backed by VCR-like cassette storage. When
        running for the first time with no cassette, the code under test accesses real
        Koji over the Internet, but interactions of the code with Koji are recorded to
        a YAML file called a cassette. Next time the test is run, the calls from the
        cassette are replayed and no Internet connection is made. Cassettes can also be
        written by hand or edited. To force a re-recoding of a cassette, delete the
        file on disk.

        To make the session authenticated when recording, set `TEST_ALLOW_LOGGED_IN=1`
        environment variable.

        To use different Koji instance than Fedora's production Koji when recording,
        set `koji_url` (and possibly `secondary_koji_url`) class attributes on the test
        suite class.

        The Koji session(s) is accessible via the `koji` method of this class.
        When using mock backend session (such as, when inheriting from `DBTest`),
        the mock backend session will automatically use the VCR-backed Koji session.

        :param cassettes: A list of cassette names. Only the last one will be used for
                          recording. Slashes in names are interpreted as directory
                          separator, nonexistent directories will be created.
        :param secondary_mode: Whether to create a separate secondary session with
                               separate cassettes suffixed with `.secondary`. Otherwise,
                               requests for secondary session will return primary session.
        """
        koji_url = getattr(
            self, 'koji_url',
            'https://koji.fedoraproject.org/kojihub'
        )
        secondary_koji_url = getattr(self, 'secondary_koji_url', koji_url)

        logged_in = os.environ.get('TEST_ALLOW_LOGGED_IN') in ('1', 'true', 'y')

        with patch_config('koji_config.server', koji_url):
            with patch_config('secondary_koji_config.server', secondary_koji_url):
                primary_koji = koji_util.KojiSession('primary', anonymous=not logged_in)
                primary_vcr = koji_vcr.KojiVCR(primary_koji, cassettes)
                primary_mock = primary_vcr.create_mock()
                if secondary_mode:
                    secondary_koji = koji_util.KojiSession('secondary')
                    secondary_cassettes = [f'{c}.secondary' for c in cassettes]
                    secondary_vcr = koji_vcr.KojiVCR(secondary_koji, secondary_cassettes)
                    secondary_mock = secondary_vcr.create_mock()

                def get_koji(koji_id):
                    assert koji_id in ('primary', 'secondary')
                    if koji_id == 'secondary':
                        if secondary_mode:
                            return secondary_mock
                        return DummyKoji('secondary')
                    return primary_mock

                with patch.object(self, 'koji', new_callable=lambda: get_koji):
                    try:
                        yield
                    finally:
                        primary_vcr.write_cassette()
                        if secondary_mode:
                            secondary_vcr.write_cassette()


class KoscheiMockSessionMixin(object):
    def __init__(self, testsuite):
        super().__init__()
        self.repo_cache_mock = RepoCacheMock()
        self.log = Mock()
        self.build_from_repo_id_override = False
        self.testsuite = testsuite

    def koji(self, koji_id):
        return self.testsuite.koji(koji_id)

    @property
    def repo_cache(self):
        return self.repo_cache_mock

    @property
    def build_from_repo_id(self):
        return self.build_from_repo_id_override


class KoscheiBackendSessionMock(KoscheiMockSessionMixin, KoscheiBackendSession):
    pass


class DBTest(AbstractTest):
    POSTGRES_OPTS = {
        # Enable faster, but unsafe operation.
        # (COMMIT statements return before WAL is written to disk.)
        'synchronous_commit': 'off',
        # Log all SQL statements.
        'log_statement': 'all',
        # Use sequential scanning only when absolutely necessary. Also enable logging
        # of statement execution plans. With these two options combined, logs can be
        # searched for sequential scans to find missing indexes.
        'enable_seqscan': 'off',
        'debug_print_plan': 'on',
    }
    postgres_initialized = None

    @staticmethod
    def init_postgres():
        print("Initializing test database...", file=sys.stderr)
        dbname = config['database_config']['database']
        with psycopg2.connect(dbname='postgres') as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("DROP DATABASE IF EXISTS {0}".format(dbname))
                cur.execute("CREATE DATABASE {0}".format(dbname))
                for option, value in DBTest.POSTGRES_OPTS.items():
                    cur.execute("ALTER DATABASE {0} SET {1} TO '{2}'".format(dbname,
                                                                             option,
                                                                             value))
        create_all()

    def __init__(self, *args, **kwargs):
        super(DBTest, self).__init__(*args, **kwargs)
        self.db = None
        self.task_id_counter = 1
        self.pkg_name_counter = 1
        self.collection = Collection(
            name="f25", display_name="Fedora Rawhide", target="f25",
            dest_tag='f25', build_tag="f25-build", priority_coefficient=1.0,
            latest_repo_resolved=True, latest_repo_id=123,
            bugzilla_product="Fedora", bugzilla_version="25",
        )
        self.session = None

    @classmethod
    def setUpClass(cls):
        super(DBTest, cls).setUpClass()
        if DBTest.postgres_initialized is None:
            DBTest.postgres_initialized = False
            if not os.environ.get('TEST_WITHOUT_POSTGRES'):
                DBTest.init_postgres()
                DBTest.postgres_initialized = True

    def setUp(self):
        super(DBTest, self).setUp()
        if not DBTest.postgres_initialized:
            self.skipTest("requires PostgreSQL")
        conn = get_engine().connect()
        for table in Base.metadata.non_materialized_view_tables:
            conn.execute(table.delete())
            if hasattr(table.c, 'id'):
                conn.execute("ALTER SEQUENCE {}_id_seq RESTART".format(table.name))
        for materialized_view in Base.metadata.materialized_views:
            materialized_view.refresh(conn)
        conn.close()
        self.session = self.create_session()
        self.db = self.session.db
        self.db.add(self.collection)
        self.db.commit()

    def create_session(self):
        return KoscheiBackendSessionMock(self)

    def tearDown(self):
        super(DBTest, self).tearDown()
        self.session.close()

    def prepare_basic_data(self):
        package = self.prepare_package('rnv')
        build = self.prepare_build(package, task_id=666, repo_id=1)
        return package, build

    def prepare_collection(self, name, **kwargs):
        values = dict(
            name=name,
            target=name,
            display_name="Fedora Rawhide",
            latest_repo_resolved=True,
            latest_repo_id=123,
            bugzilla_product="Fedora",
            bugzilla_version="rawhide",
            build_tag=f'{name}-build',
            dest_tag=f'{name}-build',
        )
        values.update(**kwargs)

        collection = Collection(**values)
        self.db.add(collection)
        self.db.commit()
        return collection

    def prepare_package(self, name=None, collection=None, tracked=True, **kwargs):
        if 'collection_id' not in kwargs:
            if collection is None:
                collection = self.collection
        if not name:
            name = 'p{}'.format(self.pkg_name_counter)
            self.pkg_name_counter += 1
        base = self.db.query(BasePackage).filter_by(name=name).first()
        if not base:
            base = BasePackage(name=name)
        pkg = Package(name=name, base=base, collection=collection, tracked=tracked, **kwargs)
        self.db.add(pkg)
        self.db.commit()
        return pkg

    def prepare_packages(self, *pkg_names):
        pkgs = []
        for name in pkg_names:
            pkg = self.db.query(Package).filter_by(name=name).first()
            if not pkg:
                pkg = self.prepare_package(name)
            pkgs.append(pkg)
        self.db.commit()
        return pkgs

    def prepare_task(self, build, state=1, arch='x86_64', started=None, task_id=None):
        if not task_id:
            task_id = self.task_id_counter
            self.task_id_counter += 1
        task = KojiTask(
            build=build,
            state=state,
            arch=arch,
            task_id=task_id,
            started=started or datetime.fromtimestamp(task_id),
        )
        self.db.add(task)
        self.db.commit()
        return task

    def prepare_build(self, package, state=None, repo_id=None, resolved=True,
                      arches=(), task_id=None, started=None, untagged=False,
                      epoch=None, version='1', release='1.fc25', real=False):
        states = {
            True: Build.COMPLETE,
            False: Build.FAILED,
            None: Build.RUNNING,
            'complete': Build.COMPLETE,
            'failed': Build.FAILED,
            'running': Build.RUNNING,
        }
        if isinstance(state, (bool, str)):
            state = states[state]
        if isinstance(package, str):
            found = self.db.query(Package).\
                filter_by(name=package, collection_id=self.collection.id).first()
            package = found or self.prepare_package(package)
        build = Build(package=package, state=state,
                      repo_id=repo_id or (1 if state != Build.RUNNING else None),
                      version=version, release=release,
                      untagged=untagged, real=real,
                      task_id=task_id or self.task_id_counter,
                      started=started or datetime.fromtimestamp(self.task_id_counter),
                      deps_resolved=resolved)
        if not task_id:
            self.task_id_counter += 1
        self.db.add(build)
        self.db.commit()
        for arch in arches:
            koji_task = KojiTask(task_id=7541,
                                 arch=arch,
                                 state=1,
                                 started=datetime.fromtimestamp(123),
                                 build_id=build.id)
            self.db.add(koji_task)
        self.db.commit()
        return build

    def prepare_user(self, **kwargs):
        user = self.db.query(User).filter_by(**kwargs).first()
        if user:
            return user
        user = User(**kwargs)
        self.db.add(user)
        self.db.commit()
        return user

    def prepare_group(self, name, content=(), namespace=None, owners=('john.doe',)):
        users = [self.prepare_user(name=username) for username in owners]
        packages = self.prepare_packages(*content)
        group = PackageGroup(name=name, namespace=namespace)
        self.db.add(group)
        self.db.commit()
        self.db.execute(PackageGroupRelation.__table__.insert(),
                        [dict(group_id=group.id, base_id=package.base_id)
                         for package in packages])
        self.db.execute(GroupACL.__table__.insert(),
                        [dict(group_id=group.id, user_id=user.id)
                         for user in users])
        self.db.commit()
        return group

    def prepare_depchange(self, dep_name, prev_epoch, prev_version, prev_release,
                          curr_epoch, curr_version, curr_release, build_id, distance):
        prev_dep = get_or_create(
            self.db,
            Dependency,
            name=dep_name,
            epoch=prev_epoch, version=prev_version, release=prev_release,
            arch='x86_64',
        )
        curr_dep = get_or_create(
            self.db,
            Dependency,
            name=dep_name,
            epoch=curr_epoch, version=curr_version, release=curr_release,
            arch='x86_64',
        )
        self.db.flush()
        change = AppliedChange(
            prev_dep_id=prev_dep.id,
            curr_dep_id=curr_dep.id,
            distance=distance,
            build_id=build_id,
        )
        self.db.add(change)
        self.db.commit()
        return change

    def prepare_resolution_change(self, package, problems):
        resolution_change = ResolutionChange(
            package_id=package.id,
            resolved=not problems,
        )
        self.db.add(resolution_change)
        self.db.flush()
        for problem in problems:
            self.db.add(ResolutionProblem(
                resolution_id=resolution_change.id,
                problem=problem,
            ))
        self.db.commit()

    @staticmethod
    def parse_pkg(string):
        epoch = None
        if ':' in string:
            epoch, _, string = string.partition(':')
        name, version, release = string.rsplit('-', 2)
        return dict(epoch=epoch, name=name, version=version, release=release,
                    arch='x86_64')

    def assert_action_log(self, *messages):
        logs = self.db.query(LogEntry.message).all_flat(set)
        self.assertCountEqual(messages, logs)


@contextlib.contextmanager
def patch_config(key, value):
    config_dict = get_config(None)
    *parts, last = key.split('.')
    for part in parts:
        config_dict = config_dict[part]

    old_value = config_dict[last]
    config_dict[last] = value
    try:
        yield
    finally:
        config_dict[last] = old_value


def with_config(key, value):
    def decorator(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            with patch_config(key, value):
                return fn(*args, **kwargs)
        return decorated
    return decorator


def with_koji_cassette(*cassettes):
    """
    Decorator version of `AbstractTest.koji_cassette`, see its documentation.

    The decorator can be invoked both with and without arguments. If no cassette name
    is provided, it defaults to `{TestCase}/{test_method}` name, where test case and
    method name are determined from the function name and enclosing class.
    """
    def decorator(fn):
        @wraps(fn)
        def decorated(self, *args, **kwargs):
            with self.koji_cassette(*(cassettes or [fn.__qualname__.replace('.', '/')])):
                fn(self, *args, **kwargs)
        return decorated
    if cassettes and callable(cassettes[0]):
        fn = cassettes[0]
        cassettes = None
        # invocation without arguments
        return decorator(fn)
    return decorator


class RepoCacheMock(object):
    @contextlib.contextmanager
    def get_sack(self, desc):
        if 123 < desc.repo_id < 130:
            desc = koji_util.KojiRepoDescriptor(desc.koji_id, desc.build_tag, 123)
        yield repo_util.load_sack(os.path.join(testdir, 'repos'), desc)

    def get_comps_path(self, desc):
        return os.path.join(testdir, 'repos', str(desc), 'repodata', 'comps.xml')

    def get_sack_copy(self, desc):
        with self.get_sack(desc) as sack:
            return sack


def service_ctor(name, plugin_name=None, plugin_endpoint='backend'):
    def inner(*args, **kwargs):
        if plugin_name:
            plugin.load_plugins(plugin_endpoint, [plugin_name])
        ctor = service.load_service(name)
        return ctor(*args, **kwargs)
    return inner


def rpmvercmp(v1, v2):
    return rpm.labelCompare(('0', '0', v1), ('0', '0', v2))
