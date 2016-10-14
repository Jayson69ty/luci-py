# coding: utf-8
# Copyright 2014 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Tasks definition.

Each user request creates a new TaskRequest. The TaskRequest instance saves the
metadata of the request, e.g. who requested it, when why, etc. It links to the
actual data of the request in a TaskProperties. The TaskProperties represents
everything needed to run the task.

This means if two users request an identical task, it can be deduped
accordingly and efficiently by the scheduler.

Note that the mere existence of a TaskRequest in the db doesn't mean it will be
scheduled, see task_scheduler.py for the actual scheduling. Registering tasks
and scheduling are kept separated to keep the immutable and mutable models in
separate files.


Overview of transactions:
- TaskRequest() are created inside a transaction.


Graph of the schema:

    +--------Root---------+
    |TaskRequest          |
    |    +--------------+ |
    |    |TaskProperties| |
    |    |              | |
    |    |   +--------+ | |
    |    |   |FilesRef| | |
    |    |   +--------+ | |
    |    +--------------+ |
    |                     |
    |id=<based on epoch>  |
    +---------------------+
               ^
               |
    <See task_to_run.py and task_result.py>

TaskProperties is embedded in TaskRequest. TaskProperties is still declared as a
separate entity to clearly declare the boundary for task request deduplication.
"""

import datetime
import hashlib
import random
import re
import urlparse

from google.appengine.api import datastore_errors
from google.appengine.ext import ndb

from components import auth
from components import datastore_utils
from components import pubsub
from components import utils

from server import config
from server import task_pack
import acl
import cipd


# Maximum acceptable priority value, which is effectively the lowest priority.
MAXIMUM_PRIORITY = 255


# Enforced on both task request and bots.
DIMENSION_KEY_RE = ur'^[a-zA-Z\-\_\.]+$'


# One day in seconds. Add 10s to account for small jitter.
_ONE_DAY_SECS = 24*60*60 + 10


# Seven day in seconds. Add 10s to account for small jitter.
_SEVEN_DAYS_SECS = 7*24*60*60 + 10


# Minimum value for timeouts.
_MIN_TIMEOUT_SECS = 1 if utils.is_local_dev_server() else 30


# The world started on 2010-01-01 at 00:00:00 UTC. The rationale is that using
# EPOCH (1970) means that 40 years worth of keys are wasted.
#
# Note: This creates a 'naive' object instead of a formal UTC object. Note that
# datetime.datetime.utcnow() also return naive objects. That's python.
_BEGINING_OF_THE_WORLD = datetime.datetime(2010, 1, 1, 0, 0, 0, 0)


# Used for isolated files.
_HASH_CHARS = frozenset('0123456789abcdef')


### Properties validators must come before the models.


def _validate_isolated(prop, value):
  if value:
    if not _HASH_CHARS.issuperset(value) or len(value) != 40:
      raise datastore_errors.BadValueError(
          '%s must be lowercase hex of length 40, not %s' % (prop._name, value))


def _validate_url(prop, value):
  # pylint: disable=unused-argument
  if value:
    # It must be https://*.appspot.com or https?://*
    parsed = urlparse.urlparse(value)
    if not parsed.netloc:
      raise datastore_errors.BadValueError(
          '%s must be valid hostname, not %s' % (prop._name, value))
    if parsed.netloc.endswith('appspot.com'):
      if parsed.scheme != 'https':
        raise datastore_errors.BadValueError(
            '%s must be https://, not %s' % (prop._name, value))
    elif parsed.scheme not in ('http', 'https'):
        raise datastore_errors.BadValueError(
            '%s must be https:// or http://, not %s' % (prop._name, value))


def _validate_namespace(prop, value):
  if not config.NAMESPACE_RE.match(value):
    raise datastore_errors.BadValueError('malformed %s' % prop._name)


def _validate_dict_of_strings(prop, value):
  """Validates TaskProperties.env."""
  if not all(
         isinstance(k, unicode) and isinstance(v, unicode)
         for k, v in value.iteritems()):
    # pylint: disable=W0212
    raise TypeError(
        '%s must be a dict of strings, not %r' % (prop._name, value))


def _validate_dimensions(prop, value):
  """Validates TaskProperties.dimensions."""
  # pylint: disable=W0212
  if not value:
    raise datastore_errors.BadValueError(u'%s must be specified' % prop._name)
  _validate_dict_of_strings(prop, value)
  for key in value:
    if not re.match(DIMENSION_KEY_RE, key):
      raise datastore_errors.BadValueError(
          u'key %r doesn\'t match %s' % (key, DIMENSION_KEY_RE))
  if u'pool' not in value and u'id' not in value:
    raise datastore_errors.BadValueError(
        u'At least one of \'id\' or \'pool\' must be used as %s' % prop._name)


def _validate_expiration(prop, value):
  """Validates TaskRequest.expiration_ts."""
  now = utils.utcnow()
  offset = int(round((value - now).total_seconds()))
  if not (_MIN_TIMEOUT_SECS <= offset <= _SEVEN_DAYS_SECS):
    # pylint: disable=W0212
    raise datastore_errors.BadValueError(
        '%s (%s, %ds from now) must effectively be between %ds and 7 days '
        'from now (%s)' %
        (prop._name, value, offset, _MIN_TIMEOUT_SECS, now))


def _validate_grace(prop, value):
  """Validates grace_period_secs in TaskProperties."""
  if not (0 <= value <= _ONE_DAY_SECS):
    # pylint: disable=W0212
    raise datastore_errors.BadValueError(
        '%s (%ds) must be between %ds and one day' % (prop._name, value, 0))


def _validate_priority(_prop, value):
  """Validates TaskRequest.priority."""
  validate_priority(value)
  return value


def _validate_task_run_id(_prop, value):
  """Validates a task_id looks valid without fetching the entity."""
  if not value:
    return None
  task_pack.unpack_run_result_key(value)
  return value


def _validate_timeout(prop, value):
  """Validates timeouts in seconds in TaskProperties."""
  if value and not (_MIN_TIMEOUT_SECS <= value <= _ONE_DAY_SECS):
    # pylint: disable=W0212
    raise datastore_errors.BadValueError(
        '%s (%ds) must be 0 or between %ds and one day' %
            (prop._name, value, _MIN_TIMEOUT_SECS))


def _validate_tags(prop, value):
  """Validates and sorts TaskRequest.tags."""
  if not ':' in value:
    # pylint: disable=W0212
    raise datastore_errors.BadValueError(
        '%s must be key:value form, not %s' % (prop._name, value))


def _validate_package_name_template(prop, value):
  """Validates a CIPD package name template."""
  if not cipd.is_valid_package_name_template(value):
    raise datastore_errors.BadValueError(
        '%s must be a valid CIPD package name template "%s"' % (
              prop._name, value))


def _validate_package_version(prop, value):
  """Validates a CIPD package version."""
  if not cipd.is_valid_version(value):
    raise datastore_errors.BadValueError(
        '%s must be a valid package version "%s"' % (prop._name, value))


def _validate_package_path(_prop, path):
  """Validates a CIPD installation path."""
  if not path:
    raise datastore_errors.BadValueError(
        'CIPD package path is required. Use "." to install to run dir.')
  if '\\' in path:
    raise datastore_errors.BadValueError(
        'CIPD package path cannot contain \\. On Windows forward-slashes '
        'will be replaced with back-slashes.')
  if '..' in path.split('/'):
    raise datastore_errors.BadValueError(
        'CIPD package path cannot contain "..".')
  if path.startswith('/'):
    raise datastore_errors.BadValueError(
        'CIPD package path cannot start with "/".')


def _validate_service_account(prop, value):
  """Validates that 'service_account' field is 'bot', 'none' or email."""
  if not value:
    return None
  if value in ('bot', 'none') or '@' in value:
    return value
  raise datastore_errors.BadValueError(
      '%r must be an email, "bot" or "none" string, got %r' %
      (prop._name, value))


### Models.


class FilesRef(ndb.Model):
  """Defines a data tree reference, normally a reference to a .isolated file."""

  # TODO(maruel): make this class have one responsibility. Currently it is used
  # in two modes:
  # - a reference to a tree, as class docstring says.
  # - input/output settings in TaskProperties.

  # The hash of an isolated archive.
  isolated = ndb.StringProperty(validator=_validate_isolated, indexed=False)
  # The hostname of the isolated server to use.
  isolatedserver = ndb.StringProperty(
      validator=_validate_url, indexed=False)
  # Namespace on the isolate server.
  namespace = ndb.StringProperty(validator=_validate_namespace, indexed=False)

  def _pre_put_hook(self):
    super(FilesRef, self)._pre_put_hook()
    if not self.isolatedserver or not self.namespace:
      raise datastore_errors.BadValueError(
          'isolate server and namespace are required')


class CipdPackage(ndb.Model):
  """A CIPD package to install in $CIPD_PATH and $PATH before task execution.

  A part of TaskProperties.
  """
  # Package name template. May use cipd.ALL_PARAMS.
  # Most users will specify ${platform} parameter.
  package_name = ndb.StringProperty(
      indexed=False, validator=_validate_package_name_template)
  # Package version that is valid for all packages matched by package_name.
  # Most users will specify tags.
  version = ndb.StringProperty(
      indexed=False, validator=_validate_package_version)
  # Path to dir, relative to the run dir, where to install the package.
  # If empty, the package will be installed in run dir.
  path = ndb.StringProperty(indexed=False, validator=_validate_package_path)

  def __str__(self):
    return '%s:%s' % (self.package_name, self.version)

  def _pre_put_hook(self):
    super(CipdPackage, self)._pre_put_hook()
    if not self.package_name:
      raise datastore_errors.BadValueError('CIPD package name is required')
    if not self.version:
      raise datastore_errors.BadValueError('CIPD package version is required')


class CipdInput(ndb.Model):
  """Specifies which CIPD client and packages to install, from which server.

  A part of TaskProperties.
  """
  # URL of the CIPD server. Must start with "https://" or "http://".
  server = ndb.StringProperty(indexed=False, validator=_validate_url)

  # CIPD package of CIPD client to use.
  # client_package.version is required.
  # client_package.path must be None.
  client_package = ndb.LocalStructuredProperty(CipdPackage)

  # List of packages to install in $CIPD_PATH prior task execution.
  packages = ndb.LocalStructuredProperty(CipdPackage, repeated=True)

  def _pre_put_hook(self):
    if not self.server:
      raise datastore_errors.BadValueError('cipd server is required')
    if not self.client_package:
      raise datastore_errors.BadValueError('client_package is required')
    if self.client_package.path:
      raise datastore_errors.BadValueError('client_package.path must be unset')
    self.client_package._pre_put_hook()

    if not self.packages:
      raise datastore_errors.BadValueError(
          'cipd_input cannot have an empty package list')

    package_names = set()
    for p in self.packages:
      p._pre_put_hook()
      if not p.path:
        raise datastore_errors.BadValueError(
            'package %s:%s: path is required' % (p.package_name, p.version))
      if p.package_name in package_names:
        raise datastore_errors.BadValueError(
           'package %s is specified more than once' % p.package_name)
      package_names.add(p.package_name)
    self.packages.sort(key=lambda p: p.package_name)


class TaskProperties(ndb.Model):
  """Defines all the properties of a task to be run on the Swarming
  infrastructure.

  This entity is not saved in the DB as a standalone entity, instead it is
  embedded in a TaskRequest.

  This model is immutable.

  New-style TaskProperties supports invocation of run_isolated. When this
  behavior is desired, the member .inputs_ref with an .isolated field value must
  be supplied. .extra_args can be supplied to pass extraneous arguments.
  """

  # TODO(maruel): convert inputs_ref and _TaskResultCommon.outputs_ref as:
  # - input = String which is the isolated input, if any
  # - isolated_server = <server, metadata e.g. namespace> which is a
  #   simplified version of FilesRef
  # - _TaskResultCommon.output = String which is isolated output, if any.

  # Hashing algorithm used to hash TaskProperties to create its key.
  HASHING_ALGO = hashlib.sha1

  # Commands to run. It is a list of 1 item, the command to run.
  # TODO(maruel): Remove after 2016-06-01.
  commands = datastore_utils.DeterministicJsonProperty(
      json_type=list, indexed=False)
  # Command to run. This is only relevant when self.inputs_ref.isolated is None.
  # This is what is called 'raw commands', in the sense that no inputs files are
  # declared.
  command = ndb.StringProperty(repeated=True, indexed=False)

  # Isolate server, namespace and input isolate hash.
  #
  # Despite its name, contains isolate server URL and namespace for isolated
  # output too. See TODO at the top of this class.
  # May be non-None even if task input is not isolated.
  #
  # Only inputs_ref.isolated or command can be specified.
  inputs_ref = ndb.LocalStructuredProperty(FilesRef)

  # CIPD packages to install.
  cipd_input = ndb.LocalStructuredProperty(CipdInput)

  # Filter to use to determine the required properties on the bot to run on. For
  # example, Windows or hostname. Encoded as json. Either 'pool' or 'id'
  # dimension are required (see _validate_dimensions).
  dimensions = datastore_utils.DeterministicJsonProperty(
      validator=_validate_dimensions, json_type=dict, indexed=False)

  # Environment variables. Encoded as json. Optional.
  env = datastore_utils.DeterministicJsonProperty(
      validator=_validate_dict_of_strings, json_type=dict, indexed=False)

  # Maximum duration the bot can take to run this task. It's named hard_timeout
  # in the bot.
  execution_timeout_secs = ndb.IntegerProperty(
      validator=_validate_timeout, required=True, indexed=False)

  # Extra arguments to supply to the command `python run_isolated ...`. Can only
  # be set if inputs_ref.isolated is set.
  extra_args = ndb.StringProperty(repeated=True, indexed=False)

  # Grace period is the time between signaling the task it timed out and killing
  # the process. During this time the process should clean up itself as quickly
  # as possible, potentially uploading partial results back.
  grace_period_secs = ndb.IntegerProperty(
      validator=_validate_grace, default=30, indexed=False)

  # Bot controlled timeout for new bytes from the subprocess. If a subprocess
  # doesn't output new data to stdout for .io_timeout_secs, consider the command
  # timed out. Optional.
  io_timeout_secs = ndb.IntegerProperty(
      validator=_validate_timeout, indexed=False)

  # If True, the task can safely be served results from a previously succeeded
  # task.
  idempotent = ndb.BooleanProperty(default=False, indexed=False)

  @property
  def is_terminate(self):
    """If True, it is a terminate request."""
    return (
        not self.commands and
        not self.command and
        self.dimensions.keys() == [u'id'] and
        not (self.inputs_ref and self.inputs_ref.isolated) and
        not self.env and
        not self.execution_timeout_secs and
        not self.extra_args and
        not self.grace_period_secs and
        not self.io_timeout_secs and
        not self.idempotent)

  @property
  def properties_hash(self):
    """Calculates the hash for this entity IFF the task is idempotent.

    It uniquely identifies the TaskProperties instance to permit deduplication
    by the task scheduler. It is None if the task is not idempotent.

    Returns:
      Hash as a compact byte str.
    """
    if not self.idempotent:
      return None
    return self.HASHING_ALGO(utils.encode_to_json(self)).digest()

  def to_dict(self):
    out = super(TaskProperties, self).to_dict(exclude=['commands'])
    out['command'] = self.commands[0] if self.commands else self.command
    return out

  def _pre_put_hook(self):
    super(TaskProperties, self)._pre_put_hook()
    if self.commands:
        raise datastore_errors.BadValueError(
            'commands is not supported anymore')
    if not self.is_terminate:
      isolated_input = self.inputs_ref and self.inputs_ref.isolated
      if bool(self.command) == bool(isolated_input):
        raise datastore_errors.BadValueError(
            'use one of command or inputs_ref.isolated')
      if self.extra_args and not isolated_input:
        raise datastore_errors.BadValueError(
            'extra_args require inputs_ref.isolated')
      if self.inputs_ref:
        self.inputs_ref._pre_put_hook()

      if self.cipd_input:
        self.cipd_input._pre_put_hook()
        if self.idempotent:
          pinned = lambda p: cipd.is_pinned_version(p.version)
          assert self.cipd_input.packages  # checked by cipd_input._pre_put_hook
          if any(not pinned(p) for p in self.cipd_input.packages):
            raise datastore_errors.BadValueError(
                'an idempotent task cannot have unpinned packages; '
                'use tags or instance IDs as package versions')


class TaskRequest(ndb.Model):
  """Contains a user request.

  Key id is a decreasing integer based on time since utils.EPOCH plus some
  randomness on lower order bits. See new_request_key() for the complete gory
  details.

  This model is immutable.
  """
  # Time this request was registered. It is set manually instead of using
  # auto_now_add=True so that expiration_ts can be set very precisely relative
  # to this property.
  created_ts = ndb.DateTimeProperty(required=True)

  # The name for this task request. It's only for description.
  name = ndb.StringProperty(required=True)

  # Authenticated client that triggered this task.
  authenticated = auth.IdentityProperty()

  # Which user to blame for this task. Can be arbitrary, not asserted by any
  # credentials.
  user = ndb.StringProperty(default='')

  # Indicates what OAuth2 credentials the task uses when calling other services.
  #
  # Possible values are: 'none', 'bot' or <email>. For more information see
  # swarming_rpcs.NewTaskRequest.
  #
  # This property exists only for informational purposes and for indexing. When
  # actually getting an OAuth credentials, the original token (stored in hidden
  # 'service_account_token' field) is used.
  service_account = ndb.StringProperty(validator=_validate_service_account)

  # The delegation token passed via 'service_account_token' when creating a new
  # task that should by run with the authority of some service account. Can also
  # be set to literal 'none' or 'bot'. See swarming_rpcs.NewTaskRequest for more
  # information.
  #
  # This property never shows up in UI or API responses.
  service_account_token = ndb.BlobProperty()

  # The actual properties are embedded in this model.
  properties = ndb.LocalStructuredProperty(
      TaskProperties, compressed=True, required=True)

  # Priority of the task to be run. A lower number is higher priority, thus will
  # preempt requests with lower priority (higher numbers).
  priority = ndb.IntegerProperty(
      indexed=False, validator=_validate_priority, required=True)

  # If the task request is not scheduled by this moment, it will be aborted by a
  # cron job. It is saved instead of scheduling_expiration_secs so finding
  # expired jobs is a simple query.
  expiration_ts = ndb.DateTimeProperty(
      indexed=True, validator=_validate_expiration, required=True)

  # Tags that specify the category of the task.
  tags = ndb.StringProperty(repeated=True, validator=_validate_tags)

  # Set when a task (the parent) reentrantly create swarming tasks. Must be set
  # to a valid task_id pointing to a TaskRunResult or be None.
  parent_task_id = ndb.StringProperty(validator=_validate_task_run_id)

  # PubSub topic to send task completion notification to.
  pubsub_topic = ndb.StringProperty(indexed=False)

  # Secret token to send as 'auth_token' attribute with PubSub messages.
  pubsub_auth_token = ndb.StringProperty(indexed=False)

  # Data to send in 'userdata' field of PubSub messages.
  pubsub_userdata = ndb.StringProperty(indexed=False)

  @property
  def task_id(self):
    """Returns the TaskResultSummary packed id, not the task request key."""
    return task_pack.pack_result_summary_key(
        task_pack.request_key_to_result_summary_key(self.key))

  @property
  def expiration_secs(self):
    """Reconstructs this value from expiration_ts and created_ts. Integer."""
    return int((self.expiration_ts - self.created_ts).total_seconds())

  @property
  def has_access(self):
    """Returns True if the current user has read-write access to this request.

    This is used for:
      * Read access: ability to read the task info and logs.
      * Write access: ability to cancel the task.

    Warning: This function looks at the current Authentication context.
    """
    return (
        acl.is_privileged_user() or
        self.authenticated == auth.get_current_identity())

  def to_dict(self):
    """Converts properties_hash to hex so it is json serializable."""
    # to_dict() doesn't recurse correctly into ndb.LocalStructuredProperty!
    out = super(TaskRequest, self).to_dict(
        exclude=['pubsub_auth_token', 'properties', 'service_account_token'])
    properties_hash = self.properties.properties_hash
    out['properties'] = self.properties.to_dict()
    out['properties_hash'] = (
        properties_hash.encode('hex') if properties_hash else None)
    return out

  def _pre_put_hook(self):
    """Adds automatic tags."""
    super(TaskRequest, self)._pre_put_hook()
    self.properties._pre_put_hook()
    if self.properties.is_terminate:
      if not self.priority == 0:
        raise datastore_errors.BadValueError(
            'terminate request must be priority 0')
    elif self.priority == 0:
      raise datastore_errors.BadValueError(
          'priority 0 can only be used for terminate request')

    if (self.pubsub_topic and
        not pubsub.validate_full_name(self.pubsub_topic, 'topics')):
      raise datastore_errors.BadValueError(
          'bad pubsub topic name - %s' % self.pubsub_topic)
    if self.pubsub_auth_token and not self.pubsub_topic:
      raise datastore_errors.BadValueError(
          'pubsub_auth_token requires pubsub_topic')
    if self.pubsub_userdata and not self.pubsub_topic:
      raise datastore_errors.BadValueError(
          'pubsub_userdata requires pubsub_topic')


### Public API.


def create_termination_task(bot_id, allow_high_priority):
  """Returns a task to terminate the given bot.

  Returns:
    TaskRequest for priority 0 (highest) termination task.
  """
  properties = TaskProperties(
      dimensions={u'id': unicode(bot_id)},
      execution_timeout_secs=0,
      grace_period_secs=0,
      io_timeout_secs=0)
  now = utils.utcnow()
  request = TaskRequest(
      created_ts=now,
      expiration_ts=now + datetime.timedelta(days=1),
      name=u'Terminate %s' % bot_id,
      priority=0,
      properties=properties,
      tags=[u'terminate:1'],
      user=auth.get_current_identity().to_bytes())
  assert request.properties.is_terminate
  init_new_request(request, allow_high_priority)
  return request


def new_request_key():
  """Returns a valid ndb.Key for this entity.

  Task id is a 64 bits integer represented as a string to the user:
  - 1 highest order bits set to 0 to keep value positive.
  - 43 bits is time since _BEGINING_OF_THE_WORLD at 1ms resolution.
    It is good for 2**43 / 365.3 / 24 / 60 / 60 / 1000 = 278 years or 2010+278 =
    2288. The author will be dead at that time.
  - 16 bits set to a random value or a server instance specific value. Assuming
    an instance is internally consistent with itself, it can ensure to not reuse
    the same 16 bits in two consecutive requests and/or throttle itself to one
    request per millisecond.
    Using random value reduces to 2**-15 the probability of collision on exact
    same timestamp at 1ms resolution, so a maximum theoretical rate of 65536000
    requests/sec but an effective rate in the range of ~64k requests/sec without
    much transaction conflicts. We should be fine.
  - 4 bits set to 0x1. This is to represent the 'version' of the entity schema.
    Previous version had 0. Note that this value is XOR'ed in the DB so it's
    stored as 0xE. When the TaskRequest entity tree is modified in a breaking
    way that affects the packing and unpacking of task ids, this value should be
    bumped.

  The key id is this value XORed with task_pack.TASK_REQUEST_KEY_ID_MASK. The
  reason is that increasing key id values are in decreasing timestamp order.
  """
  # TODO(maruel): Use real randomness.
  suffix = random.getrandbits(16)
  return convert_to_request_key(utils.utcnow(), suffix)


def request_key_to_datetime(request_key):
  """Converts a TaskRequest.key to datetime.

  See new_request_key() for more details.
  """
  if request_key.kind() != 'TaskRequest':
    raise ValueError('Expected key to TaskRequest, got %s' % request_key.kind())
  # Ignore lowest 20 bits.
  xored = request_key.integer_id() ^ task_pack.TASK_REQUEST_KEY_ID_MASK
  offset_ms = (xored >> 20) / 1000.
  return _BEGINING_OF_THE_WORLD + datetime.timedelta(seconds=offset_ms)


def datetime_to_request_base_id(now):
  """Converts a datetime into a TaskRequest key base value.

  Used for query order().
  """
  if now < _BEGINING_OF_THE_WORLD:
    raise ValueError(
        'Time %s is set to before %s' % (now, _BEGINING_OF_THE_WORLD))
  delta = now - _BEGINING_OF_THE_WORLD
  return int(round(delta.total_seconds() * 1000.)) << 20


def convert_to_request_key(date, suffix=0):
  assert 0 <= suffix <= 0xffff
  request_id_base = datetime_to_request_base_id(date)
  return request_id_to_key(int(request_id_base | suffix << 4 | 0x1))


def request_id_to_key(request_id):
  """Converts a request id into a TaskRequest key.

  Note that this function does NOT accept a task id. This functions is primarily
  meant for limiting queries to a task creation range.
  """
  return ndb.Key(TaskRequest, request_id ^ task_pack.TASK_REQUEST_KEY_ID_MASK)


def validate_request_key(request_key):
  if request_key.kind() != 'TaskRequest':
    raise ValueError('Expected key to TaskRequest, got %s' % request_key.kind())
  task_id = request_key.integer_id()
  if not task_id:
    raise ValueError('Invalid null TaskRequest key')
  if (task_id & 0xF) == 0xE:
    # New style key.
    return

  # Check the shard.
  # TODO(maruel): Remove support 2015-02-01.
  request_shard_key = request_key.parent()
  if not request_shard_key:
    raise ValueError('Expected parent key for TaskRequest, got nothing')
  if request_shard_key.kind() != 'TaskRequestShard':
    raise ValueError(
        'Expected key to TaskRequestShard, got %s' % request_shard_key.kind())
  root_entity_shard_id = request_shard_key.string_id()
  if (not root_entity_shard_id or
      len(root_entity_shard_id) != task_pack.DEPRECATED_SHARDING_LEVEL):
    raise ValueError(
        'Expected root entity key (used for sharding) to be of length %d but '
        'length was only %d (key value %r)' % (
            task_pack.DEPRECATED_SHARDING_LEVEL,
            len(root_entity_shard_id or ''),
            root_entity_shard_id))


def init_new_request(request, allow_high_priority):
  """Initializes a new TaskRequest but doesn't store it.

  Fills up some values and does minimal checks.

  If parent_task_id is set, properties for the parent are used:
  - priority: defaults to parent.priority - 1
  - user: overridden by parent.user

  """
  assert request.__class__ is TaskRequest, request
  if request.parent_task_id:
    run_result_key = task_pack.unpack_run_result_key(request.parent_task_id)
    result_summary_key = task_pack.run_result_key_to_result_summary_key(
        run_result_key)
    request_key = task_pack.result_summary_key_to_request_key(
        result_summary_key)
    parent = request_key.get()
    if not parent:
      raise ValueError('parent_task_id is not a valid task')
    request.priority = max(min(request.priority, parent.priority - 1), 0)
    # Drop the previous user.
    request.user = parent.user

  # If the priority is below 100, make sure the user has right to do so.
  if request.priority < 100 and not allow_high_priority:
    # Special case for terminate request.
    if not request.properties.is_terminate:
      # Silently drop the priority of normal users.
      request.priority = 100

  request.authenticated = auth.get_current_identity()
  if (not request.properties.is_terminate and
      request.properties.grace_period_secs is None):
    request.properties.grace_period_secs = 30
  if request.properties.idempotent is None:
    request.properties.idempotent = False

  request.service_account = 'none'
  if request.service_account_token and request.service_account_token != 'none':
    if request.service_account_token == 'bot':
      request.service_account = 'bot'
    else:
      # TODO(vadimsh): Check the token signature, verify it can be used by the
      # current user, extract service account email.
      raise auth.AuthorizationError('service_account_token is not implemented')

  request.tags.append('priority:%s' % request.priority)
  request.tags.append('user:%s' % request.user)
  request.tags.append('service_account:%s' % request.service_account)
  for key, value in request.properties.dimensions.iteritems():
    request.tags.append('%s:%s' % (key, value))
  request.tags = sorted(set(request.tags))


def new_request_clone(original_request, allow_high_priority):
  """Creates a new TaskRequest as a copy of another one but doesn't store it.

  Used by "Retry task" UI button.

  Modifications:
  - Enforces idempotent=False.
  - Removes the parent_task_id if any.
  - Appends suffix '(Retry #1)' to the task name, incrementing the number of
    followup retries.
  - Strips any tag starting with 'user:'.
  - Overrides request's user with the credentials of the currently logged in
    user.
  - Does not inherit pubsub parameter.

  New request uses same 'service_account_token', but 'init_new_request' will
  recheck it again, ensuring the current user (the one that triggers the retry)
  is able to use the token.

  Returns:
    The newly created TaskRequest.
  """
  now = utils.utcnow()
  if original_request.properties.is_terminate:
    raise ValueError('cannot clone a terminate request')
  properties = TaskProperties(**original_request.properties.to_dict())
  properties.idempotent = False
  expiration_ts = (
      now + (original_request.expiration_ts - original_request.created_ts))
  name = original_request.name
  match = re.match(r'^(.*) \(Retry #(\d+)\)$', name)
  if match:
    name = '%s (Retry #%d)' % (match.group(1), int(match.group(2)) + 1)
  else:
    name += ' (Retry #1)'
  user = auth.get_current_identity()
  username = user.to_bytes()
  prefix = 'user:'
  if not username.startswith(prefix):
    raise ValueError('a request can only be cloned by a user, not a bot')
  username = username[len(prefix):]
  tags = set(t for t in original_request.tags if not t.startswith('user:'))
  request = TaskRequest(
      created_ts=now,
      expiration_ts=expiration_ts,
      name=name,
      parent_task_id=None,
      priority=original_request.priority,
      properties=properties,
      service_account_token=original_request.service_account_token,
      tags=tags,
      user=username)
  init_new_request(request, allow_high_priority)
  return request


def validate_priority(priority):
  """Throws ValueError if priority is not a valid value."""
  if 0 > priority or MAXIMUM_PRIORITY < priority:
    raise datastore_errors.BadValueError(
        'priority (%d) must be between 0 and %d (inclusive)' %
        (priority, MAXIMUM_PRIORITY))
