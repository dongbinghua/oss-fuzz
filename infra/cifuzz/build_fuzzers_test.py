# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests the functionality of the cifuzz module."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import parameterized

# pylint: disable=wrong-import-position
INFRA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(INFRA_DIR)

OSS_FUZZ_DIR = os.path.dirname(INFRA_DIR)

import build_fuzzers
import continuous_integration
import repo_manager
import test_helpers

# NOTE: This integration test relies on
# https://github.com/google/oss-fuzz/tree/master/projects/example project.
EXAMPLE_PROJECT = 'example'

# Location of data used for testing.
TEST_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'test_data')

# An example fuzzer that triggers an crash.
# Binary is a copy of the example project's do_stuff_fuzzer and can be
# generated by running "python3 infra/helper.py build_fuzzers example".
EXAMPLE_CRASH_FUZZER = 'example_crash_fuzzer'

# An example fuzzer that does not trigger a crash.
# Binary is a modified version of example project's do_stuff_fuzzer. It is
# created by removing the bug in my_api.cpp.
EXAMPLE_NOCRASH_FUZZER = 'example_nocrash_fuzzer'

# A fuzzer to be built in build_fuzzers integration tests.
EXAMPLE_BUILD_FUZZER = 'do_stuff_fuzzer'

# pylint: disable=no-self-use,protected-access,too-few-public-methods,unused-argument


class BuildFuzzersTest(unittest.TestCase):
  """Unit tests for build_fuzzers."""

  @mock.patch('build_specified_commit.detect_main_repo',
              return_value=('example.com', '/path'))
  @mock.patch('repo_manager._clone', return_value=None)
  @mock.patch('continuous_integration.checkout_specified_commit')
  @mock.patch('helper.docker_run', return_value=False)  # We want to quit early.
  def test_cifuzz_clusterfuzzlite_env_var(self, mock_docker_run, _, __, ___):
    """Tests that the CIFUZZ and CLUSTERFUZZLITE env vars are set."""

    with tempfile.TemporaryDirectory() as tmp_dir:
      build_fuzzers.build_fuzzers(
          test_helpers.create_build_config(
              oss_fuzz_project_name=EXAMPLE_PROJECT,
              project_repo_name=EXAMPLE_PROJECT,
              workspace=tmp_dir,
              pr_ref='refs/pull/1757/merge'))

      docker_run_command = mock_docker_run.call_args_list[0][0][0]

    def command_has_env_var_arg(command, env_var_arg):
      for idx, element in enumerate(command):
        if idx == 0:
          continue

        if element == env_var_arg and command[idx - 1] == '-e':
          return True
      return False

    self.assertTrue(command_has_env_var_arg(docker_run_command, 'CIFUZZ=True'))
    self.assertTrue(
        command_has_env_var_arg(docker_run_command, 'CLUSTERFUZZLITE=True'))


class InternalGithubBuildTest(unittest.TestCase):
  """Tests for building OSS-Fuzz projects on GitHub actions."""
  PROJECT_REPO_NAME = 'myproject'
  SANITIZER = 'address'
  GIT_SHA = 'fake'
  PR_REF = 'fake'

  def _create_builder(self, tmp_dir, oss_fuzz_project_name='myproject'):
    """Creates an InternalGithubBuilder and returns it."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=oss_fuzz_project_name,
        project_repo_name=self.PROJECT_REPO_NAME,
        workspace=tmp_dir,
        sanitizer=self.SANITIZER,
        git_sha=self.GIT_SHA,
        pr_ref=self.PR_REF,
        cfl_platform='github')
    cfl_platform = continuous_integration.get_ci(config)
    builder = build_fuzzers.Builder(config, cfl_platform)
    builder.repo_manager = repo_manager.RepoManager('/fake')
    return builder

  @mock.patch('helper.docker_run', return_value=True)
  @mock.patch('continuous_integration.checkout_specified_commit',
              side_effect=None)
  def test_correct_host_repo_path(self, _, __):
    """Tests that the correct self.host_repo_path is set by
    build_image_and_checkout_src. Specifically, we want the name of the
    directory the repo is in to match the name used in the docker
    image/container, so that it will replace the host's copy properly."""
    image_repo_path = '/src/repo_dir'
    with tempfile.TemporaryDirectory() as tmp_dir, mock.patch(
        'build_specified_commit.detect_main_repo',
        return_value=('inferred_url', image_repo_path)):
      builder = self._create_builder(tmp_dir)
      builder.build_image_and_checkout_src()

    self.assertEqual(os.path.basename(builder.host_repo_path),
                     os.path.basename(image_repo_path))

  @mock.patch('clusterfuzz_deployment.ClusterFuzzLite.upload_build',
              return_value=True)
  def test_upload_build_disabled(self, mock_upload_build):
    """Test upload build (disabled)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
      builder = self._create_builder(tmp_dir)
      builder.upload_build()

    mock_upload_build.assert_not_called()

  @mock.patch('repo_manager.RepoManager.get_current_commit',
              return_value='commit')
  @mock.patch('clusterfuzz_deployment.ClusterFuzzLite.upload_build',
              return_value=True)
  def test_upload_build(self, mock_upload_build, mock_get_current_commit):
    """Test upload build."""
    with tempfile.TemporaryDirectory() as tmp_dir:
      builder = self._create_builder(tmp_dir, oss_fuzz_project_name='')
      builder.config.upload_build = True
      builder.upload_build()

    mock_upload_build.assert_called_with('commit')


@unittest.skipIf(not os.getenv('INTEGRATION_TESTS'),
                 'INTEGRATION_TESTS=1 not set')
class BuildFuzzersIntegrationTest(unittest.TestCase):
  """Integration tests for build_fuzzers."""

  def setUp(self):
    self.temp_dir_ctx_manager = test_helpers.docker_temp_dir()
    self.workspace = self.temp_dir_ctx_manager.__enter__()
    self.out_dir = os.path.join(self.workspace, 'build-out')
    test_helpers.patch_environ(self)

    base_runner_path = os.path.join(INFRA_DIR, 'base-images', 'base-runner')
    os.environ['PATH'] = os.environ['PATH'] + os.pathsep + base_runner_path

  def tearDown(self):
    self.temp_dir_ctx_manager.__exit__(None, None, None)

  def test_external_github_project(self):
    """Tests building fuzzers from an external project on Github."""
    project_repo_name = 'external-project'
    git_url = 'https://github.com/jonathanmetzman/cifuzz-external-example.git'
    # This test is dependant on the state of
    # github.com/jonathanmetzman/cifuzz-external-example.
    config = test_helpers.create_build_config(
        project_repo_name=project_repo_name,
        workspace=self.workspace,
        git_url=git_url,
        git_sha='HEAD',
        cfl_platform='github',
        base_commit='HEAD^1')
    self.assertTrue(build_fuzzers.build_fuzzers(config))
    self.assertTrue(
        os.path.exists(os.path.join(self.out_dir, EXAMPLE_BUILD_FUZZER)))

  def test_external_generic_project(self):
    """Tests building fuzzers from an external project not on Github."""
    project_repo_name = 'cifuzz-external-example'
    git_url = 'https://github.com/jonathanmetzman/cifuzz-external-example.git'
    # This test is dependant on the state of
    # github.com/jonathanmetzman/cifuzz-external-example.
    manager = repo_manager.clone_repo_and_get_manager(
        'https://github.com/jonathanmetzman/cifuzz-external-example',
        self.workspace)
    project_src_path = manager.repo_dir
    config = test_helpers.create_build_config(
        project_repo_name=project_repo_name,
        workspace=self.workspace,
        git_url=git_url,
        filestore='no_filestore',
        git_sha='HEAD',
        project_src_path=project_src_path,
        base_commit='HEAD^1')
    self.assertTrue(build_fuzzers.build_fuzzers(config))
    self.assertTrue(
        os.path.exists(os.path.join(self.out_dir, EXAMPLE_BUILD_FUZZER)))

  def test_valid_commit(self):
    """Tests building fuzzers with valid inputs."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        project_repo_name='oss-fuzz',
        workspace=self.workspace,
        git_sha='0b95fe1039ed7c38fea1f97078316bfc1030c523',
        base_commit='da0746452433dc18bae699e355a9821285d863c8',
        cfl_platform='github')
    self.assertTrue(build_fuzzers.build_fuzzers(config))
    self.assertTrue(
        os.path.exists(os.path.join(self.out_dir, EXAMPLE_BUILD_FUZZER)))

  def test_valid_pull_request(self):
    """Tests building fuzzers with valid pull request."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        project_repo_name='oss-fuzz',
        workspace=self.workspace,
        pr_ref='refs/pull/1757/merge',
        base_ref='master',
        cfl_platform='github')
    self.assertTrue(build_fuzzers.build_fuzzers(config))
    self.assertTrue(
        os.path.exists(os.path.join(self.out_dir, EXAMPLE_BUILD_FUZZER)))

  def test_invalid_pull_request(self):
    """Tests building fuzzers with invalid pull request."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        project_repo_name='oss-fuzz',
        workspace=self.workspace,
        pr_ref='ref-1/merge',
        base_ref='master',
        cfl_platform='github')
    self.assertTrue(build_fuzzers.build_fuzzers(config))

  def test_invalid_oss_fuzz_project_name(self):
    """Tests building fuzzers with invalid project name."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name='not_a_valid_project',
        project_repo_name='oss-fuzz',
        workspace=self.workspace,
        git_sha='0b95fe1039ed7c38fea1f97078316bfc1030c523')
    self.assertFalse(build_fuzzers.build_fuzzers(config))

  def test_invalid_repo_name(self):
    """Tests building fuzzers with invalid repo name."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        project_repo_name='not-real-repo',
        workspace=self.workspace,
        git_sha='0b95fe1039ed7c38fea1f97078316bfc1030c523')
    self.assertFalse(build_fuzzers.build_fuzzers(config))

  def test_invalid_git_sha(self):
    """Tests building fuzzers with invalid commit SHA."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        project_repo_name='oss-fuzz',
        workspace=self.workspace,
        git_sha='',
        cfl_platform='github')
    with self.assertRaises(AssertionError):
      build_fuzzers.build_fuzzers(config)

  def test_invalid_workspace(self):
    """Tests building fuzzers with invalid workspace."""
    config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        project_repo_name='oss-fuzz',
        workspace=os.path.join(self.workspace, 'not', 'a', 'dir'),
        git_sha='0b95fe1039ed7c38fea1f97078316bfc1030c523')
    self.assertFalse(build_fuzzers.build_fuzzers(config))


class CheckFuzzerBuildTest(unittest.TestCase):
  """Tests the check_fuzzer_build function in the cifuzz module."""

  SANITIZER = 'address'
  LANGUAGE = 'c++'

  def setUp(self):
    self.temp_dir_obj = tempfile.TemporaryDirectory()
    workspace_path = os.path.join(self.temp_dir_obj.name, 'workspace')
    self.config = test_helpers.create_build_config(
        oss_fuzz_project_name=EXAMPLE_PROJECT,
        sanitizer=self.SANITIZER,
        language=self.LANGUAGE,
        workspace=workspace_path,
        pr_ref='refs/pull/1757/merge')
    self.workspace = test_helpers.create_workspace(workspace_path)
    shutil.copytree(TEST_DATA_PATH, workspace_path)
    test_helpers.patch_environ(self, runner=True)

  def tearDown(self):
    self.temp_dir_obj.cleanup()

  def test_correct_fuzzer_build(self):
    """Checks check_fuzzer_build function returns True for valid fuzzers."""
    self.assertTrue(build_fuzzers.check_fuzzer_build(self.config))

  def test_not_a_valid_path(self):
    """Tests that False is returned when a nonexistent path is given."""
    self.config.workspace = 'not/a/valid/path'
    self.assertFalse(build_fuzzers.check_fuzzer_build(self.config))

  def test_no_valid_fuzzers(self):
    """Tests that False is returned when an empty directory is given."""
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.config.workspace = tmp_dir
      os.mkdir(os.path.join(self.config.workspace, 'build-out'))
      self.assertFalse(build_fuzzers.check_fuzzer_build(self.config))

  @mock.patch('utils.execute', return_value=(None, None, 0))
  def test_allow_broken_fuzz_targets_percentage(self, mock_execute):
    """Tests that ALLOWED_BROKEN_TARGETS_PERCENTAGE is set when running
    docker if passed to check_fuzzer_build."""
    percentage = '0'
    self.config.allowed_broken_targets_percentage = percentage
    build_fuzzers.check_fuzzer_build(self.config)
    self.assertEqual(
        mock_execute.call_args[1]['env']['ALLOWED_BROKEN_TARGETS_PERCENTAGE'],
        percentage)


@unittest.skip('Test is too long to be run with presubmit.')
class BuildSantizerIntegrationTest(unittest.TestCase):
  """Integration tests for the build_fuzzers.
    Note: This test relies on "curl" being an OSS-Fuzz project."""
  PROJECT_NAME = 'curl'
  PR_REF = 'fake_pr'

  @classmethod
  def _create_config(cls, tmp_dir, sanitizer):
    return test_helpers.create_build_config(
        oss_fuzz_project_name=cls.PROJECT_NAME,
        project_repo_name=cls.PROJECT_NAME,
        workspace=tmp_dir,
        pr_ref=cls.PR_REF,
        sanitizer=sanitizer)

  @parameterized.parameterized.expand([('memory',), ('undefined',)])
  def test_valid_project_curl(self, sanitizer):
    """Tests that MSAN can be detected from project.yaml"""
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.assertTrue(
          build_fuzzers.build_fuzzers(self._create_config(tmp_dir, sanitizer)))


class GetDockerBuildFuzzersArgsNotContainerTest(unittest.TestCase):
  """Tests that _get_docker_build_fuzzers_args_not_container works as
  intended."""

  def test_get_docker_build_fuzzers_args_no_container(self):
    """Tests that _get_docker_build_fuzzers_args_not_container works
    as intended."""
    host_repo_path = '/host/repo'
    result = build_fuzzers._get_docker_build_fuzzers_args_not_container(
        host_repo_path)
    expected_result = ['-v', '/host/repo:/host/repo']
    self.assertEqual(result, expected_result)


if __name__ == '__main__':
  unittest.main()
