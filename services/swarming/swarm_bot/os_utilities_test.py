#!/usr/bin/env python
# Copyright 2014 The Swarming Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

import logging
import os
import re
import subprocess
import sys
import time
import unittest

# Import os_utilities first before manipulating sys.path to ensure it can load
# fine.
import os_utilities

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import test_env

test_env.setup_test_env()

from depot_tools import auto_stub

VERBOSE = '-v' in sys.argv


# Access to a protected member _XXX of a client class
# pylint: disable=W0212


class TestOsUtilitiesPrivate(auto_stub.TestCase):
  def setUp(self):
    super(TestOsUtilitiesPrivate, self).setUp()
    if not VERBOSE:
      self.mock(logging, 'error', lambda *_: None)

  def test_from_cygwin_path(self):
    data = [
      ('foo', None),
      ('x:\\foo$', None),
      ('X:\\foo$', None),
      ('/cygdrive/x/foo$', 'x:\\foo$'),
    ]
    for i, (inputs, expected) in enumerate(data):
      actual = os_utilities._from_cygwin_path(inputs)
      self.assertEqual(expected, actual, (inputs, expected, actual, i))

  def test_to_cygwin_path(self):
    data = [
      ('foo', None),
      ('x:\\foo$', '/cygdrive/x/foo$'),
      ('X:\\foo$', '/cygdrive/x/foo$'),
      ('/cygdrive/x/foo$', None),
    ]
    for i, (inputs, expected) in enumerate(data):
      actual = os_utilities._to_cygwin_path(inputs)
      self.assertEqual(expected, actual, (inputs, expected, actual, i))

class TestOsUtilities(auto_stub.TestCase):
  def test_get_os_version(self):
    version = os_utilities.get_os_version()
    self.assertTrue(version)
    self.assertTrue(re.match(r'^\d+\.\d+$', version), version)

  def test_get_os_name(self):
    expected = ('Linux', 'Mac', 'Windows')
    self.assertIn(os_utilities.get_os_name(), expected)

  def test_get_cpu_type(self):
    expected = ('arm', 'x86')
    self.assertIn(os_utilities.get_cpu_type(), expected)

  def test_get_cpu_bitness(self):
    expected = ('32', '64')
    self.assertIn(os_utilities.get_cpu_bitness(), expected)

  def test_get_attributes(self):
    # Just assert it's not empty, no need to verify each individual values.
    self.assertTrue(os_utilities.get_attributes('id'))

  def test_setup_auto_startup_win(self):
    # TODO(maruel): Figure out a way to test properly.
    pass

  def test_setup_auto_startup_osx(self):
    # TODO(maruel): Figure out a way to test properly.
    pass

  def test_restart(self):
    class Foo(Exception):
      pass

    def raise_exception(x):
      raise x

    self.mock(subprocess, 'check_call', lambda _: None)
    self.mock(time, 'sleep', lambda _: raise_exception(Foo()))
    self.mock(logging, 'error', lambda *_: None)
    with self.assertRaises(Foo):
      os_utilities.restart()

  def test_restart_and_return(self):
    self.mock(subprocess, 'check_call', lambda _: None)
    self.assertIs(True, os_utilities.restart_and_return())


if __name__ == '__main__':
  logging.basicConfig(
      level=logging.DEBUG if '-v' in sys.argv else logging.ERROR)
  unittest.main()
