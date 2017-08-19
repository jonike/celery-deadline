import sys
import os
import os.path
import getpass
import json
import base64

from celery.app.task import Task
from celery.result import ResultSet
from kombu.utils.uuid import uuid

from celery.app.amqp import AMQP
import kombu
import kombu.pools
import kombu.transport.virtual.base as base
from kombu.utils.objects import cached_property

import requests

from pymongo import MongoClient
from bson.objectid import ObjectId

# deadline organizational strategies:
# BatchName  Job Name
# {group_id}    {task_name}-{task_id}   batch name by group id if present, then one task per job
# {group_id}    {task_name}             batch name by group id if present, then tasks of same type in one job
# custom        {task_name}             custom batch name
# {app_name}    {task_name}
#               {group}                 tasks in the same group in one job


def enable_deadline_support(app, mongodb):
    app.amqp_cls = __name__ + ':DeadlineAMQP'


def get_collection(client):
    return client['celery_deadline']['job_tasks']


# override the base virtual channel so that it doesn't use the connection object.  we use this
# for converting to and from Message objects.
class DummyChannel(base.Channel):
    def __init__(self, *args, **kwargs):
        pass


def submit_job(pulse_url, job_info, plugin_info, aux_files=None, auth=None):
    url = pulse_url + '/api/jobs'

    if auth is None:
        auth = (getpass.getuser(), '')
    elif isinstance(auth, basestring):
        auth = (auth, '')

    data = {
        'JobInfo': job_info,
        'PluginInfo': plugin_info,
        'AuxFiles': aux_files or [],
        'IdOnly': True
    }
    print(data)
    resp = requests.post(url, auth=auth, data=json.dumps(data))

    if not resp.ok:
        print ('Request failed with status {0:d}: '
               '{1} {2}').format(resp.status_code, resp.reason, resp.text)
        raise PulseRequestError(resp.status_code, resp.text)

    return resp.json()['_id']


class DeadlineProducer(kombu.Producer):

    def __init__(self, channel, *args, **kwargs):
        self.deadline_pulse_url = kwargs.pop('deadline_pulse_url', None)
        self.deadline_mongo_url = kwargs.pop('deadline_mongo_url', None)
        super(DeadlineProducer, self).__init__(channel, *args, **kwargs)

    @cached_property
    def mongo_client(self):
        return MongoClient(self.deadline_mongo_url)

    def publish(self, body, routing_key=None, delivery_mode=None,
                mandatory=False, immediate=False, priority=0,
                content_type=None, content_encoding=None, serializer=None,
                headers=None, compression=None, exchange=None, retry=False,
                retry_policy=None, declare=None, expiration=None,
                **properties):
        # FIXME:
        # detect if deadline was requested based on properties. 
        # maybe this doubles as way of passing pulse URL?
        deadline_requested = True
        if deadline_requested and self.deadline_pulse_url and self.deadline_mongo_url:
            channel = DummyChannel()
            headers = {} if headers is None else headers
            compression = self.compression if compression is None else compression
            job_info = properties.get('job_info', {}).copy()
            plugin_info = properties.get('plugin_info', {}).copy()
            group_id = properties.pop('deadline_group_id', None)

            if expiration is not None:
                properties['expiration'] = str(int(expiration * 1000))

            if headers['task'] == __name__ + '.plugin_task':
                plugin, frames, frame, index = body[0]
                if index != 0:
                    # don't submit to deadline
                    job_info = None
                else:
                    assert group_id is not None
                    job_info['Plugin'] = plugin
                    job_info['Frames'] = frames

                    i = 0
                    while True:
                        key = 'ExtraInfoKeyValue%d' % i
                        if key not in job_info:
                            job_info[key] = 'celery_id=%s' % group_id
                            break

                print repr(body)
                print properties
            else:
                # setup JobInfo
                job_info['Plugin'] = 'Celery'
                job_info['Frames'] = '1'
                job_info['EventOptIns'] = 'CeleryJobPurgedEvent'

            body, content_type, content_encoding = self._prepare(
                body, serializer, content_type, content_encoding,
                compression, headers)

            message = channel.prepare_message(
                body, priority, content_type,
                content_encoding, headers, properties,
            )

            if job_info:
                job_id = self._submit_deadline_job(headers, job_info, plugin_info)
                if group_id is None:
                    group_id = job_id

                print "JOBID %s" % job_id
            print message
            tasks_col = get_collection(self.mongo_client)

            tasks_col.update_one({"_id": ObjectId(group_id)},
                                 {"$push": {"tasks": json.dumps(message)}},
                                 upsert=True)
        else:
            return super(DeadlineProducer, self).publish(
                body, routing_key, delivery_mode, mandatory, immediate, priority, 
                content_type, content_encoding, serializer, headers, compression,
                exchange, retry, retry_policy, declare, **properties)

    def _submit_deadline_job(self, headers, job_info, plugin_info):

        values = {
            'group_id': headers['group'] or '',
            'root_id': headers['root_id'],
            'task_id': headers['id'],
            'task_name': headers['task'],
            'task_args': headers['argsrepr'],
            'task_kwargs': headers['kwargsrepr'],
            'app_name': headers['task'].rsplit('.', 1)[0],
            'plugin': job_info['Plugin'],
        }

        # set defaults
        if values['group_id']:
            job_info.setdefault('BatchName', '{plugin}-{group_id}')
        job_info.setdefault('Name', '{plugin}-{task_name}-{task_id}')

        # expand values:
        job_info['Name'] = job_info['Name'].format(**values)
        batch_name = job_info.get('BatchName')
        if batch_name:
            job_info['BatchName'] = batch_name.format(**values)
        return submit_job(self.deadline_pulse_url, job_info, plugin_info)


class DeadlineConsumer(kombu.Consumer):
    """
    Consumer that reads a single task then shuts down the worker
    """
    def __init__(self, raw_message, channel, *args, **kwargs):
        self.raw_message = raw_message
        super(DeadlineConsumer, self).__init__(channel, *args, **kwargs)

    def consume(self, no_ack=None):
        import kombu.utils
        # override the channel so that we get a simple, consistent decoding scheme. otherwise,
        # the behavior is dependent on the channel type
        self.channel = DummyChannel()
        print "raw_message"
        print self.raw_message
        self.raw_message['properties']['delivery_tag'] = kombu.utils.uuid()
        print "running"
        self._receive_callback(self.raw_message)
        print "done receive"
        sys.exit(0)

    # def _receive_callback(self, message):
    #     print "MESSAGE", message
    #     super(DeadlineConsumer, self)._receive_callback(message)
    #     print "done receive"
    #     sys.exit(0)

    # def consume(self, no_ack=None):
    #     import kombu.utils
    #     from kombu.message import Message
    #
    #     job_id = os.environ['DEADLINE_JOB_ID']
    #     print "JOBID", job_id
    #     queue = get_queue(job_id)
    #     self._basic_consume(queue.bind(self.channel), no_ack=no_ack, nowait=True)


def get_message_from_env():
    message_str = os.environ.get('CELERY_DEADLINE_MESSAGE')
    if message_str:
        return json.loads(base64.b64decode(message_str))


class DeadlineAMQP(AMQP):
    @cached_property
    def mongo_url(self):
        conf = self.app.conf
        mongo_url = conf.get('deadline_mongo_url')
        if mongo_url:
            return mongo_url
        if conf.result_backend and conf.result_backend.startswith('mongodb://'):
            return conf.result_backend
        return

    def Consumer(self, *args, **kwargs):
        message = get_message_from_env()
        if message:
            print("USING DEADLINE CONSUMER")
            return DeadlineConsumer(message, *args, **kwargs)
        else:
            print("USING KOMBU CONSUMER")
            return kombu.Consumer(*args, **kwargs)

    def Producer(self, *args, **kwargs):
        kwargs['deadline_pulse_url'] = self.app.conf.get('deadline_pulse_url')
        print "deadline_pulse_url", kwargs['deadline_pulse_url']
        kwargs['deadline_mongo_url'] = self.mongo_url
        return DeadlineProducer(*args, **kwargs)

    @property
    def producer_pool(self):
        if self._producer_pool is None:
            self._producer_pool = kombu.pools.producers[
                self.app.connection_for_write()]
            self._producer_pool.limit = self.app.pool.limit
            # TODO: submit this patch to celery:
            self._producer_pool.Producer = self.Producer
        return self._producer_pool


# ---

def _get_deadline_task_id(parent_task_id, frame):
    # FIXME: which approach?
    # frame_id = uuid()
    frame_id = '%s-%06d' % (parent_task_id, frame)
    # frame_id = uuid5(parent_task_id, '%06' % frame)
    return frame_id


# class DeadlinePluginTask(Task):
#     def apply_async(self, args=None, kwargs=None, task_id=None, producer=None,
#                     link=None, link_error=None, shadow=None, **options):
#         job_info = options.get('job_info', {})
#         plugin_info = options['plugin_info']
#         pulse_url = self.app['deadline_pulse_url']
#         frames = job_info.get('Frames', '1')
#         results = []
#         task_id = task_id or uuid()
#         for frame in parse_frames(frames):
#             frame_id =_get_deadline_task_id(task_id, frame)
#             results.append(self.AsyncResult(frame_id))
#         # TODO: insert the task_id as job metadata
#         job_id = submit_job(pulse_url, job_info, plugin_info)
#         return ResultSet(results, app=self.app)


import re
from celery import Celery, group
app = Celery('celery_deadline')
app.amqp_cls = 'celery_deadline:DeadlineAMQP'
app.config_from_object('celery_deadline_config')

@app.task
def plugin_task(plugin, frames, frame, index):
    return frame


def job(plugin, frames, **plugin_info):
    deadline_group_id = ObjectId()
    return group(
        [plugin_task.signature((plugin, frames, frame, i),
                              plugin_info=plugin_info,
                              deadline_group_id=deadline_group_id)
         for i, frame in enumerate(parse_frames(frames))])


def set_task_result(value):
    # get celery task_id for current frame
    frame_id = _get_deadline_task_id(task_id, frame)


# TODO: create a plugin that returns registered OutputFilenames as results. Example:
# OutputDirectory0=\\fileserver\Project\Renders\OutputFolder\
# OutputFilename0=o_HDP_010_BG_v01.####.exr
# OutputDirectory1=\\fileserver\Project\Renders\OutputFolder\
# OutputFilename1=o_HDP_010_SPEC_v01####.dpx
# OutputDirectory2=\\fileserver\Project\Renders\OutputFolder\
# OutputFilename2=o_HDP_010_RAW_v01_####.png


_frame_regex = None

def _get_frame_regex():
    global _frame_regex
    if _frame_regex is None:
        stepSep = 'x'
        _frame_regex = re.compile('(-?\d+)(?:-(\d+)(?:%s(\d+))?)?' % stepSep)
    return _frame_regex

    # regexNamedGrp = '(?P<frames>%s)'
    # groupSep = ','
    # frameRegexStr = '%s(?:%s(%s))*' % (partRegexStr, groupSep,
    #                                          partRegexStr)
    # regexStr = regexNamedGrp % frameRegexStr
    #
    # # group the whole thing (for re.split)
    # regexStr = '(' + regexStr + ')'
    # regex = re.compile(regexStr + '$')


def parse_frames(frames):
    # match = self.getValidator().match(sequenceStr)
    # if not match:
    #     raise InvalidSequenceError("Invalidly formatted sequence "
    #                                "string: %r" % sequenceStr)
    # # sequenceStr = sequenceStr.rstrip(self.paddingFormat)
    # # remove the padding
    # frames = match.group('frames')
    # if not frames:
    #     raise NonExplicitFramesError(sequenceStr)

    sequence = []
    for start, stop, step in _get_frame_regex().findall(frames):
        start = int(start)
        if not stop:
            sequence.append(start)
        else:
            stop = int(stop)
            if stop == start:
                sequence.append(start)
                continue

            if not step:
                step = 1
            else:
                step = int(step)

            # Head off attempts to create ridiculously large frame lists
            if (stop - start) / step > 1000000:
                raise SequenceParseOverflow('Attempted to parse range of '
                    '%d frames from range string %r'
                    % ((stop - start) / step, frames))
            sequence.extend(xrange(start, stop + 1, step))
    print sequence
    return sequence
