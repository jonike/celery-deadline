from Deadline.Events import DeadlineEventListener

# --
import json
import base64

from Deadline.Scripting import ClientUtils, RepositoryUtils
from MongoDB.Driver import MongoClient
from MongoDB.Driver.Builders import Query, Fields
from MongoDB.Bson import ObjectId, BsonExtensionMethods, BsonString, BsonObjectId


def GetTaskCollection():
    connStr = RepositoryUtils.GetDatabaseConnectionString()
    urls = connStr.strip('()').split(',')
    url = urls[0]
    client = MongoClient('mongodb://' + url)
    db = client.GetServer().GetDatabase('celery_deadline')
    return db.GetCollection('job_tasks')


def GetCeleryGroupId(job):
    groupIdStr = job.GetJobExtraInfoKeyValueWithDefault('celery_id', job.JobId)
    return BsonObjectId(ObjectId.Parse(groupIdStr))


def GetCeleryTasks(job, frames):
    """
    Get raw celery task messages for the current deadline task.
    """
    collection = GetTaskCollection()
    groupId = GetCeleryGroupId(job)
    query = Query.EQ('_id', groupId)
    allFrames = list(job.JobFramesList)
    currentFrame = frames[0]
    index = allFrames.index(currentFrame)
    packetSize = len(frames)
    cursor = collection.Find(query).SetFields(Fields.Slice('tasks', index, packetSize)).SetLimit(1)
    doc = list(cursor)[0]
    results = []
    for task in doc.GetValue('tasks'):
        results.append(task.ToString())
    return results


def GetCeleryArguments(tasks, plugin):
    plugin.SetProcessEnvironmentVariable("CELERY_DEADLINE_NUM_MESSAGES", str(len(tasks)))
    apps = []
    for i, task in enumerate(tasks):
        plugin.SetProcessEnvironmentVariable("CELERY_DEADLINE_MESSAGE%d" % i, base64.b64encode(task))
        # plugin.SetEnvironmentVariable("CELERY_DEADLINE_MESSAGE", base64.b64encode(task))
        apps.append(json.loads(task)['headers']['task'].rsplit('.', 1)[0])

    assert len(set(apps)) == 1, "Tasks span more than one celery app"
    app = apps[0]
    return '-A %s worker -l debug -P solo --without-gossip --without-mingle --without-heartbeat' % app


def ExecuteTasks(plugin, tasks, mode='execute'):
    args = GetCeleryArguments(tasks, plugin)
    plugin.SetProcessEnvironmentVariable("CELERY_DEADLINE_MODE", mode)
    plugin.LogInfo("Running celery callback")
    plugin.RunProcess('celery', args, '', -1)
    # not available on DeadlineEventListener:
    # plugin.StartMonitoredProgram('celery', 'celery', args, '')
# --


def GetIncompleteFrames(job):
    # second argument is invalidate. not sure if it's necessary to invalidate the cache
    tasks = RepositoryUtils.GetJobTasks(job, False).TaskCollectionTasks
    incomplete = []
    for task in tasks:
        if task.TaskStatus != 'Done':
            incomplete.extend(list(task.TaskFrameList))
    return incomplete


def GetDeadlineEventListener():
    return CeleryEvents()


def CleanupDeadlineEventListener(deadlinePlugin):
    deadlinePlugin.Cleanup()


class CeleryEvents(DeadlineEventListener):
    def __init__(self):
        self.OnJobPurgedCallback += self.OnJobPurged
        self.OnJobDeletedCallback += self.OnJobDeleted

    def Cleanup(self):
        del self.OnJobPurgedCallback
        del self.OnJobDeletedCallback

    def OnJobPurged(self, job):
        print("RUNNING PURGE ------------")
        collection = GetTaskCollection()
        id = GetCeleryGroupId(job)
        query = Query.EQ('_id', id)
        collection.deleteOne(query)

    def OnJobDeleted(self, job):
        print("RUNNING DELETE ------------")
        frames = GetIncompleteFrames(job)
        tasks = GetCeleryTasks(job, frames)
        ExecuteTasks(self, tasks, mode='delete')
