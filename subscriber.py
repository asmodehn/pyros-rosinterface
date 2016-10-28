from __future__ import absolute_import

import time

import roslib
import rospy

from importlib import import_module
from collections import deque, OrderedDict


from .message_conversion import get_msg, get_msg_dict, populate_instance, extract_values, FieldTypeMismatchException
from .poolparam import PoolParam
from .topicbase import TopicBase


class SubscriberBack(TopicBase):
    """
    TopicBack is the class handling conversion from Python to ROS Topic
    Requirement : Only one topicBack per actual ROS Topic.
    Since we connect to an already existing ros topic, our number of connections should never drop under 1
    """

    # We need some kind of instance count here since system state returns only one node instance
    # as publisher for multiple publisher in this process.
    #
    # This is used in ros_interface update for added puba/subs to determine
    # if we previously started publishing / subscribing to this topic
    # usually the count will be just 1, but more is possible during tests
    #
    # This is also used in ros_interface update for removed pubs/subs to determine
    # if we previously stopped publishing / subscribing to this topic
    # usually the count will be just 1, but more is possible during tests

    pool = PoolParam(rospy.Subscriber, "subscribers")

    def __init__(self, topic_name, topic_type, msg_queue_size=1):
        # Parent class will resolve/normalize topic_name
        super(SubscriberBack, self).__init__(topic_name, topic_type)

        rospy.loginfo(
            rospy.get_name() + " Pyros.rosinterface : Adding subscriber {name} {typename}".format(
                name=self.name, typename=self.rostype))

        self.topic = self.pool.acquire(self.name, self.rostype, self.topic_callback, queue_size=1)

        self.msg = deque([], msg_queue_size)

        self.empty_cb = None

    def cleanup(self):
        """
        Launched when we want to whithhold this interface instance
        :return:
        """

        # TODO : should we do this in del method instead ? to allow reuse until garbage collection actually happens...
        rospy.loginfo(
            rospy.get_name() + " Pyros.rosinterface : Removing subscriber {name} {typename}".format(
                name=self.name, typename=self.rostype))

        self.pool.release(self.topic)

        super(SubscriberBack, self).cleanup()

    def asdict(self):
        """
        Here we provide a dictionary suitable for a representation of the Topic instance
        the main point here is to make it possible to transfer this to remote processes.
        We are not interested in pickleing the whole class with Subscriber and Publisher
        :return:
        """
        d = super(SubscriberBack, self).asdict()
        d['publishers'] = self.topic.impl.get_stats_info()
        return d

    def get(self, num=0, consume=False):
        if not self.msg:
            return None
        # TODO : implement a way to have "plug and play" behaviors (some can be "all, paged, FIFO, etc." with custom code that can be insterted here...)
        res = None
        #TODO : implement returning multiple messages ( paging/offset like for long REST requests )
        if consume:
            res = self.msg.popleft()
            if 0 == len(self.msg) and self.empty_cb:
                self.empty_cb()
                #TODO : CHECK that we can survive here even if we get dropped from the topic list
        else:
            res = self.msg[0]
            try:
                res = extract_values(res)
            except FieldTypeMismatchException as e:
                rospy.logerr("[{name}] : field type mismatch {e}".format(name=__name__, e=e))
                raise
                # TODO : reraise a topic exception ?
        return res

    #returns the number of unread message
    def unread(self):
        return len(self.msg)

    def set_empty_callback(self, cb):
        self.empty_cb = cb

    def topic_callback(self, msg):
        # TODO : we are duplicating the queue behavior that is already in rospy... Is there a better way ?
        self.msg.appendleft(msg)

