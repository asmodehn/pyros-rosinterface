from __future__ import absolute_import

import os
from collections import namedtuple, MutableMapping
from copy import deepcopy, copy
from itertools import ifilter
import logging

import pyros_utils
import rospy
import rosservice, rostopic, rosparam

import re
import ast
import socket
import threading
import Queue

import time

# create logger
_logger = logging.getLogger(__name__)
# and let it propagate to parent logger, or other handler
# the user of pyros should configure handlers

from ..baseinterface import DiffTuple
from .baseinterface import BaseInterface

from .service import ServiceBack, ServiceTuple
from .topic import TopicBack, TopicTuple
from .param import ParamBack, ParamTuple

try:
    import rocon_python_comms
except ImportError:
    rocon_python_comms = None


class RosInterface(BaseInterface):

    """
    RosInterface.
    """
    def __init__(self, node_name, services=None, topics=None, params=None, enable_cache=False, argv=None):
        # This runs in a child process (managed by PyrosROS) and as a normal ros node)

        # First thing to do : find the rosmaster...
        # master has to be running here or we just wait for ever
        # TODO : improve...
        m, _ = pyros_utils.get_master(spawn=False)
        while not m.is_online():
            _logger.warning("ROSMASTER not found !!!...")
            time.sleep(0.5)

        # Second thing to do : initialize the ros node and disable signals to avoid overriding callers behavior
        rospy.init_node(node_name, argv=argv, disable_signals=True)
        rospy.loginfo('RosInterface {name} node started with args : {argv}'.format(name=node_name, argv=argv))
        # note on dynamic update (config reload, etc.) this is reinitialized
        # rospy.init_node supports being reinitialized with the exact same arguments

        self.enable_cache = enable_cache
        if self.enable_cache and rocon_python_comms is None:
            rospy.logerr("Connection Cache enabled for RosInterface, but rocon_python_comms not found. Disabling.")
            self.enable_cache = False

        self.cb_lock = threading.Lock()
        with self.cb_lock:
            self.cb_ss = Queue.Queue()
            self.cb_ss_dt = Queue.Queue()

        # we add params from ROS environment, if we get something there (bwcompat behavior)
        services = services or []
        topics = topics or []
        params = params or []
        services += list(set(ast.literal_eval(rospy.get_param('~services', "[]"))))
        topics += list(set(ast.literal_eval(rospy.get_param('~topics', "[]"))))
        params += list(set(ast.literal_eval(rospy.get_param('~params', "[]"))))
        enable_cache = rospy.get_param('~enable_cache', enable_cache)

        if enable_cache is not None:
            self.enable_cache = enable_cache
        # Note : None means no change ( different from [] )
        rospy.loginfo("""[{name}] ROS Interface initialized with:
        -    services : {services}
        -    topics : {topics}
        -    params : {params}
        -    enable_cache : {enable_cache}
        """.format(name=__name__,
            topics="\n" + "- ".rjust(10) + "\n\t- ".join(topics) if topics else [],
            services="\n" + "- ".rjust(10) + "\n\t- ".join(services) if services else [],
            params="\n" + "- ".rjust(10) + "\n\t- ".join(params) if params else [],
            enable_cache=enable_cache)
        )

        # This base constructor assumes the system to interface with is already available ( can do a get_svc_available() )
        super(RosInterface, self).__init__(services or [], topics or [], params or [])

        # connecting to the master via proxy object
        self._master = rospy.get_master()

        #: If enabled, connection cache proxy will be setup in update() to allow dynamic update via config.
        # TODO : double check : maybe useless now since we completely reinit the interface for dynamic update...
        self.connection_cache = None

        # Setup our debug log
        # We need this debug log since rospy.logdebug does NOT store debug messages in the log.
        # But it should Ref : http://wiki.ros.org/rospy/Overview/Logging#Reading_log_messages
        # TODO : find why.
        ros_home = os.environ.get('ROS_HOME', os.path.join(os.path.expanduser("~"), '.ros'))
        self._debug_logger = logging.getLogger('pyros.ros_interface')
        if not os.path.exists(os.path.join(ros_home, 'logdebug')):
            os.makedirs(os.path.join(ros_home, 'logdebug'))
        logfilename = os.path.join(ros_home, 'logdebug', rospy.get_name()[1:].replace(os.path.sep, "-") + '_pyros_rosinterface.log')
        file_handler = logging.handlers.RotatingFileHandler(
            logfilename,
            maxBytes=100 * 131072,
            backupCount=10
        )
        self._debug_logger.propagate = False  # to avoid propagating to root logger
        self._debug_logger.setLevel(logging.DEBUG)
        self._debug_logger.addHandler(file_handler)
    # # ros functions that should connect with the ros system we want to interface with
    # # SERVICES
    # def get_svc_available(self):  # function returning all services available on the system
    #     return self.services_available
    #
    # def service_type_resolver(self, service_name):  # function resolving the type of a service
    #     # get first matching service
    #     svc = self.services_available.get(service_name)
    #     if svc:
    #         if svc.type is None:  # if the type is unknown, lets discover it
    #             try:
    #                 resolved_service_name = rospy.resolve_name(service_name)  # required or not ?
    #                 svc.type = rosservice.get_service_type(resolved_service_name)
    #             except rosservice.ROSServiceIOException:  # exception can occur -> just reraise
    #                 raise
    #         return svc.type  # return the type
    #     else:
    #         rospy.logerr("ERROR while resolving {service_name}. Service not known as available. Ignoring".format(**locals()))
    #         return None
    #
    # def ServiceMaker(self, service_name, service_type):  # the service class implementation
    #     return ServiceBack(service_name, service_type)
    #
    # def ServiceCleaner(self, service):  # the service class cleanup implementation
    #     return service.cleanup()
    #
    # # TOPICS
    # def get_topic_available(self):  # function returning all topics available on the system
    #     return self.topics_available
    #
    # def topic_type_resolver(self, topic_name):  # function resolving the type of a topic
    #     # get first matching service
    #     tpc = self.topics_available.get(topic_name)
    #     if tpc:
    #         if tpc.type is None:  # if the type is unknown, lets discover it
    #             try:
    #                 resolved_topic_name = rospy.resolve_name(topic_name)
    #                 tpc.type, _, _ = rostopic.get_topic_type(resolved_topic_name)
    #             except rosservice.ROSServiceIOException:  # exception can occur -> just reraise
    #                raise
    #         return tpc.type  # return the first we find. enough.
    #     else:
    #         rospy.logerr("ERROR while resolving {topic_name}. Topic not known as available. Ignoring".format(**locals()))
    #         return None
    #
    # def TopicMaker(self, topic_name, topic_type, *args, **kwargs):  # the topic class implementation
    #     return TopicBack(topic_name, topic_type, *args, **kwargs)
    #
    # def TopicCleaner(self, topic):  # the topic class implementation
    #     return topic.cleanup()
    #
    # # PARAMS
    # def get_param_available(self):  # function returning all params available on the system
    #     return self.params_available
    #
    # def param_type_resolver(self, param_name):  # function resolving the type of a param
    #     prm = self.params_available.get(param_name)
    #     if prm:
    #         if prm.type is None:  # if the type is unknown, lets discover it (since the param is supposed to exist)
    #             try:
    #                 prm.type = type(rospy.get_param(param_name))  # we use the detected python type here (since there is no rospy param type interface for this)
    #             except KeyError:  # exception can occur -> just reraise
    #                 raise
    #         return prm.type  # return the first we find. enough.
    #     else:
    #         rospy.logerr("ERROR while resolving {param_name}. Param not known as available. Ignoring".format(**locals()))
    #         return None
    #
    # def ParamMaker(self, param_name, param_type):  # the param class implementation
    #     return ParamBack(param_name, param_type)
    #
    # def ParamCleaner(self, param):  # the param class implementation
    #     return param.cleanup()

    def _filter_out_pyros_topics(self, publishers, subscribers, if_topics=None):
        """
        This method filter out the topic pubs / subs that are due to pyros behavior itself.
        These extra pubs/subs should not be used to represent the state of the system we connect to.
        :param publishers:
        :param subscribers:
        :param if_topics: topics that are there because of pyros interface (such as provided from _get_pyros_topics)
        :return:
        """
        # getting the list of interfaced topics from well known node param
        if_topics = if_topics or TopicBack.get_all_interfaces()

        # Examination of topics :
        # We keep publishers that are provided by something else ( not our exposed topic pub if present )
        # OR if we have locally multiple pubs / subs.
        filtered_publishers = []
        for p in publishers:
            # keeping only nodes that are not pyros interface for this topic
            # when added pub, also keeping interface nodes that have more than one interface instance (useful for tests and nodelets, etc. )
            nonif_pub_providers = [pp for pp in p[1] if (not if_topics.get(pp, {}).get(p[0], False) or TopicBack.get_impl_ref_count(p[0]) > 1)]
            if nonif_pub_providers:
                filtered_publishers.append([p[0], nonif_pub_providers])

        # We keep subscribers that are provided by something else ( not our exposed topic sub if present )
        # OR if we have locally multiple pubs / subs.
        filtered_subscribers = []
        for s in subscribers:
            # keeping only nodes that are not pyros interface for this topic
            # when added sub, also keeping interface nodes that have more than one interface instance (useful for tests and nodelets, etc. )
            nonif_sub_providers = [sp for sp in s[1] if (not if_topics.get(sp, {}).get(s[0], False) or TopicBack.get_impl_ref_count(s[0]) > 1)]
            if nonif_sub_providers:
                filtered_subscribers.append([s[0], nonif_sub_providers])

        return filtered_publishers, filtered_subscribers

    # This is really needed because an interfaced topic satisfies itself, and no diff message will be received
    # from ROS, even if the topic is gone. This is because the interface pub still requires the topic.
    # This returns the list of topics that satisfy themselves, to add them to the diff
    def get_lone_interfaced_topics(self):
        # TODO : get rid of that, we cannot rely on number of connections...
        """
        Returns lone interfaced topic in the same way as it would be returned by the ROS system.
        But reading only internal data, since the local interface state (last pub/sub) will trigger the change.
        :return:
        """
        lone_topics = []
        for tname, t in self.topics_pool.transients.iteritems():
            # TODO : separate pub and sub
            if TopicBack.get_impl_ref_count(tname) == 1:
                lone_topics.append([tname, [rospy.get_name()]])
                self.topics_pool.available.pop(tname)  # without this, the topic will remain in self.topics_available until the cache node can update, etc. -> delay
        return lone_topics

    # def reset_params(self, params):
    #     """
    #     called to update params from rospy.
    #     CAREFUL : this can be called from another thread (subscriber callback)
    #     """
    #     with self.params_available_lock:
    #         self.params_available = dict()
    #         for p in params:
    #             pt = []
    #             ptp = ParamTuple(name=p, type=pt[1] if len(pt) > 0 else None)
    #             self.params_available[ptp.name] = ptp
    #
    # def compute_params(self, params_dt):
    #     """
    #     called to update params from rospy.
    #     CAREFUL : this can be called from another thread (subscriber callback)
    #     """
    #
    #     with self.params_available_lock:
    #         for p in params_dt.added:
    #             pt = ParamTuple(name=p, type=None)
    #             if pt.name in self.params_available:
    #                 if self.params_available[pt.name].type is None or pt.type is not None:
    #                     self.params_available[pt.name].type = pt.type
    #             else:
    #                 self.params_available[pt.name] = pt
    #
    #         for p in params_dt.removed:
    #             pt = ParamTuple(name=p, type=None)
    #             if pt.name in self.params_available:
    #                 self.params_available.pop(pt.name, None)
    #
    #     return params_dt

    def retrieve_system_state(self):
        """
        This will retrieve the system state from ROS master if needed, and apply changes to local variable to keep
        a local representation of the connections available up to date.
        """
        try:
            # we call the master only if we dont get system_state from connection cache
            if self.enable_cache and self.connection_cache is not None:
                publishers, subscribers, services = self.connection_cache.getSystemState()
                topic_types = self.connection_cache.getTopicTypes()
                try:
                    service_types = self.connection_cache.getServiceTypes()
                    # handling fallback here since master doesnt have the API
                except rocon_python_comms.UnknownSystemState as exc:
                    service_types = []
            else:
                publishers, subscribers, services = self._master.getSystemState()[2]
                topic_types = self._master.getTopicTypes()[2]
                service_types = []  # master misses this API to be consistent

            # Getting this doesnt depend on cache
            params = set(rospy.get_param_names())

            return publishers, subscribers, services, params, topic_types, service_types

        except socket.error:
            rospy.logerr("[{name}] couldn't get system state from the master ".format(name=__name__))

    # def reset_system_state(self, publishers, subscribers, services, topic_types, service_types):
    #     """
    #     Reset internal system state representation.
    #     expect lists in format similar to masterAPI.
    #     :param publishers:
    #     :param subscribers:
    #     :param services:
    #     :param topic_types:
    #     :param service_types:
    #     :return:
    #     """
    #     # TODO : separate pub and sub
    #     iftopics = TopicBack.get_all_interfaces()
    #     filtered_publishers, filtered_subscribers = self._filter_out_pyros_topics(publishers, subscribers, if_topics=iftopics)
    #     # this is used with full list : we only need to filter out from that list.
    #
    #     # We merge both pubs and subs, so that only one pub or one sub which is not ours is enough to keep the topic
    #     with self.topics_available_lock:
    #         self.topics_available = dict()
    #         for t in (filtered_publishers + filtered_subscribers):
    #             tt = next(ifilter(lambda ltt: t[0] == ltt[0], topic_types), [])
    #             ttp = TopicTuple(name=t[0], type=tt[1] if len(tt) > 0 else None, endpoints=set(t[1]))
    #             self.topics_available[ttp.name] = ttp
    #
    #     with self.services_available_lock:
    #         self.services_available = dict()
    #         for s in services:
    #             st = next(ifilter(lambda lst: s[0] == lst[0], service_types), [])
    #             stp = ServiceTuple(name=s[0], type=st[1] if len(st) > 0 else None)
    #             self.services_available[stp.name] = stp
    #
    #     # We still need to return DiffTuples
    #     return services, filtered_publishers + filtered_subscribers
    #
    # def compute_system_state(self, publishers_dt, subscribers_dt, services_dt, topic_types_dt, service_types_dt):
    #     """
    #     This is called only if there is a cache proxy with a callback, and expects DiffTuple filled up with names or types
    #     :param services_dt:
    #     :param publishers_dt:
    #     :param subscribers_dt:
    #     :return:
    #     """
    #     self._debug_logger.debug("compute_system_state(self, {publishers_dt}, {subscribers_dt}, {services_dt}, {topic_types_dt}, {service_types_dt})".format(**locals()))
    #     iftopics = TopicBack.get_all_interfaces()
    #     filtered_added_publishers, filtered_added_subscribers = self._filter_out_pyros_topics(publishers_dt.added, subscribers_dt.added, if_topics=iftopics)
    #     # this is called with difference tuples
    #     # we need to add removed topics that are only exposed by this pyros interface.
    #     filtered_removed_publishers, filtered_removed_subscribers = self._filter_out_pyros_topics(publishers_dt.removed, subscribers_dt.removed, if_topics=iftopics)
    #
    #     # collapsing add / remove pairs with the help of nodes multiplicity
    #     # here to avoid more complicated side effects later on
    #     added_pubs = {pub[0]: pub[1] for pub in filtered_added_publishers}
    #     added_subs = {sub[0]: sub[1] for sub in filtered_added_subscribers}
    #     removed_pubs = {pub[0]: pub[1] for pub in filtered_removed_publishers}
    #     removed_subs = {sub[0]: sub[1] for sub in filtered_removed_subscribers}
    #
    #     for apn, ap in added_pubs.iteritems():
    #         for rpn, rp in removed_pubs.iteritems():
    #             if rp in ap:  # remove nodes that are added and removed -> no change seen
    #                 ap.remove(rp)
    #                 removed_pubs[rpn].remove(rp)
    #
    #     for rpn, rp in removed_pubs.iteritems():
    #         for apn, ap in added_pubs.iteritems():
    #             if ap in rp:  # remove nodes that are removed and added -> no change seen
    #                 rp.remove(ap)
    #                 added_pubs[apn].remove(ap)
    #
    #     # We merge both pubs and subs, so that only one pub or one sub which is not ours is enough to keep the topic
    #     # Need to be careful if pub and sub are added/removed at same time : only one topic added/removed
    #     added_topics = {pub[0]: pub[1] for pub in filtered_added_publishers}
    #     removed_topics = {pub[0]: pub[1] for pub in filtered_removed_publishers}
    #
    #     for t in filtered_added_subscribers:
    #         added_topics[t[0]] = added_topics.get(t[0], []) + t[1]
    #     for t in filtered_removed_subscribers:
    #         removed_topics[t[0]] = removed_topics.get(t[0], []) + t[1]
    #
    #     # TODO : improve here to make sure of nodes unicity after this collapsing step
    #
    #     topics_dt = DiffTuple(
    #         added=[[k, v] for k, v in added_topics.iteritems()],
    #         removed=[[k, v] for k, v in removed_topics.iteritems()]
    #     )
    #     self._debug_logger.debug("topics_dt : {topics_dt}".format(**locals()))
    #     with self.topics_available_lock:
    #         for t in topics_dt.added:
    #             tt = next(ifilter(lambda ltt: t[0] == ltt[0], topic_types_dt.added), [])
    #             ttp = TopicTuple(name=t[0], type=tt[1] if len(tt) > 0 else None, endpoints=set(t[1]))
    #             if ttp.name in self.topics_available:
    #                 # if already available, we only update the endpoints list
    #                 self.topics_available[ttp.name].endpoints |= ttp.endpoints
    #             else:
    #                 self.topics_available[ttp.name] = ttp
    #
    #         for t in topics_dt.removed:
    #             tt = next(ifilter(lambda ltt: t[0] == ltt[0], topic_types_dt.removed), [])
    #             ttp = TopicTuple(name=t[0], type=tt[1] if len(tt) > 0 else None, endpoints=set(t[1]))
    #             if ttp.name in self.topics_available:
    #                 self.topics_available[ttp.name].endpoints -= ttp.endpoints
    #                 if not self.topics_available[ttp.name].endpoints:
    #                     self.topics_available.pop(ttp.name, None)
    #
    #     with self.services_available_lock:
    #         for s in services_dt.added:
    #             st = next(ifilter(lambda lst: s[0] == lst[0], service_types_dt.added), [])
    #             stp = ServiceTuple(name=s[0], type=st[1] if len(st) > 0 else None)
    #             if stp.name in self.services_available:
    #                 if self.services_available[stp.name].type is None and stp.type is not None:
    #                     self.services_available[stp.name].type = stp.type
    #             else:
    #                 self.services_available[stp.name] = stp
    #
    #         for s in services_dt.removed:
    #             st = next(ifilter(lambda lst: s[0] == lst[0], service_types_dt.removed), [])
    #             stp = ServiceTuple(name=s[0], type=st[1] if len(st) > 0 else None)
    #             if stp.name in self.services_available:
    #                 self.services_available.pop(stp.name, None)
    #
    #     # We still need to return DiffTuples
    #     return services_dt, topics_dt


    # for use with line_profiler or memory_profiler
    # Not working yet... need to solve multiprocess profiling issues...
    #@profile
    def update(self):

        params_if_dt = []
        services_if_dt = []
        topics_if_dt = []

        # Destroying connection cache proxy if needed
        if self.connection_cache is not None and not self.enable_cache:
            # removing existing connection cache proxy to force a reinit of everything
            # to make sure we dont get a messed up system state with wrong list/diff from
            # dynamically switching cache on and off.
            self.connection_cache = None

        # TODO Instead of one or the other, we should have "two layer behaviors" with different frequencies
        # Fast loop checking only diff
        # Slow loop checking full state
        # It will allow recovering from any mistakes because of wrong diffs (update speed/race conditions/etc.)
        if self.enable_cache:
            if self.connection_cache is None:  # Building Connection Cache proxy if needed
                # connectioncache proxy if available (remap the topics if necessary instead of passing params)
                try:
                    self.connection_cache = rocon_python_comms.ConnectionCacheProxy(
                        list_sub='~connections_list',
                        handle_actions=False,
                        user_callback=self._proxy_cb,
                        diff_opt=True,
                        diff_sub='~connections_diff'
                    )

                except AttributeError as attr_exc:
                    # attribute error (likely rocon_python_comms doesnt have ConnectionCacheProxy)
                    # NOT EXPECTED System configuration problem : BE LOUD !
                    # timeout initializing : disabling the feature but we should be LOUD about it
                    rospy.logwarn("Pyros.rosinterface : FAILED during initialization of Connection Cache Proxy. Disabling.")
                    import traceback
                    rospy.logwarn('Exception: {0}'.format(traceback.format_stack()))
                    self.enable_cache = False

                except rocon_python_comms.ConnectionCacheProxy.InitializationTimeout as timeout_exc:

                    # timeout initializing : disabling the feature but we should WARN about it
                    rospy.logwarn("Pyros.rosinterface : TIMEOUT during initialization of Connection Cache Proxy. Disabling.")
                    self.enable_cache = False

                else:
                    rospy.loginfo("Pyros.rosinterface : Connection Cache Optimization enabled")

            # determining params diff despite lack of API
            params = set(rospy.get_param_names())
            params_dt = DiffTuple(
                added=[p for p in params if p not in self.params_available],
                removed=[p for p in self.params_available if p not in params]
            )
            # Needs to be done first, since topic algorithm depends on it
            params_if_dt = self.params_pool.update_delta(params_dt=params_dt)

            # detecting lone topics and simulating a removed detection to trigger remove from interface
            # These are only used for detection.
            # compute_system_state will retrieve them again after filtering out interface topics.
            # TODO : simplify and solidify logic for this case
            # TODO : the double loop system described earlier will solve this without relying on num_connections
            # TMP : ignore for now...
            # early_topics_dt = DiffTuple([], [t[0] for t in self.get_lone_interfaced_topics()])
            #
            # if early_topics_dt.added or early_topics_dt.removed:
            #     self._debug_logger.debug(rospy.get_name() + " Pyros.rosinterface : Early Topics Delta {early_topics_dt}".format(**locals()))

            # If we have a callback setup we process the diff we got since last time
            if (self.cb_ss.qsize() > 0 or self.cb_ss_dt.qsize() > 0):

                # This will be set if we need to ignore current state, and reset it from list
                reset = False

                added_services = dict()
                removed_services = dict()
                added_publishers = dict()
                removed_publishers = dict()
                added_subscribers = dict()
                removed_subscribers = dict()

                added_topic_types = []
                removed_topic_types = []
                added_service_types = []
                removed_service_types = []

                with self.cb_lock:
                    while self.cb_ss.qsize() > 0 or self.cb_ss_dt.qsize() > 0:
                        try:
                            cb_ss_dt = self.cb_ss_dt.get_nowait()

                            # if there was no change but we got a callback,
                            # it means it s the first and we need to set the whole list
                            if cb_ss_dt.added is None and cb_ss_dt.removed is None:
                                try:
                                    cb_ss = self.cb_ss.get_nowait()
                                    # we need to break here already and reset
                                    # and the previous diff we got dont matter any longer

                                    for k, v in cb_ss.services.iteritems():
                                        added_services[k] = added_services.get(k, set()) | v.nodes

                                    for k, v in cb_ss.publishers.iteritems():
                                        added_publishers[k] = added_publishers.get(k, set()) | v.nodes

                                    for k, v in cb_ss.services.iteritems():
                                        added_subscribers[k] = added_subscribers.get(k, set()) | v.nodes

                                    pubset = {(name, chan.type) for name, chan in cb_ss.publishers.iteritems()}
                                    subset = {(name, chan.type) for name, chan in cb_ss.subscribers.iteritems()}
                                    added_topic_types = [list(t) for t in (pubset | subset)]

                                    svcset = {(name, chan.type) for name, chan in cb_ss.services.iteritems()}
                                    added_service_types = [list(t) for t in svcset]

                                    # here we need to force a reset
                                    reset = True

                                except Queue.Empty as exc:
                                    raise  # should not happen

                            else:  # we have a delta
                                try:
                                    # we can skip the full list
                                    self.cb_ss.get_nowait()

                                    for k, v in cb_ss_dt.added.services.iteritems():
                                        added_services[k] = added_services.get(k, set()) | v.nodes
                                    for k, v in cb_ss_dt.removed.services.iteritems():
                                        removed_services[k] = removed_services.get(k, set()) | v.nodes

                                    for k, v in cb_ss_dt.added.publishers.iteritems():
                                        added_publishers[k] = added_publishers.get(k, set()) | v.nodes
                                    for k, v in cb_ss_dt.removed.publishers.iteritems():
                                        removed_publishers[k] = removed_publishers.get(k, set()) | v.nodes

                                    for k, v in cb_ss_dt.added.subscribers.iteritems():
                                        added_subscribers[k] = added_subscribers.get(k, set()) | v.nodes
                                    for k, v in cb_ss_dt.removed.subscribers.iteritems():
                                        removed_subscribers[k] = removed_subscribers.get(k, set()) | v.nodes

                                    # Careful here the previous loop produced result that still matters
                                    pubset = {(name, chan.type) for name, chan in cb_ss_dt.added.publishers.iteritems()}
                                    subset = {(name, chan.type) for name, chan in cb_ss_dt.added.subscribers.iteritems()}
                                    added_topic_types += [list(t) for t in (pubset | subset)]

                                    pubset = {(name, chan.type) for name, chan in cb_ss_dt.removed.publishers.iteritems()}
                                    subset = {(name, chan.type) for name, chan in cb_ss_dt.removed.subscribers.iteritems()}
                                    removed_topic_types += [list(t) for t in (pubset | subset)]

                                    svcset = {(name, chan.type) for name, chan in cb_ss_dt.added.services.iteritems()}
                                    added_service_types += [list(t) for t in svcset]

                                    svcset = {(name, chan.type) for name, chan in cb_ss_dt.removed.services.iteritems()}
                                    removed_service_types += [list(t) for t in svcset]

                                except Queue.Empty as exc:
                                    raise  # should not happen

                        except Queue.Empty as exc:
                            raise  # should not happen

                # if we need to reset we do it right now and return.
                if reset:
                    # TODO : put that in debug log and show based on python logger configuration
                    # print("Pyros ROS interface RESET")
                    # print("Pubs : {0}".format([[k, [n[0] for n in nset]] for k, nset in added_publishers.iteritems()]))
                    # print("Subs : {0}".format([[k, [n[0] for n in nset]] for k, nset in added_subscribers.iteritems()]))
                    # print("Srvs : {0}".format([[k, [n[0] for n in nset]] for k, nset in added_services.iteritems()]))
                    # we will remove what we have now.

                    added_services = [[k, [n[0] for n in nset]] for k, nset in added_services.iteritems()]

                    services_if_dt = self.services_pool.update(added_services, added_service_types)

                    # TODO : separate pubs and subs
                    added_topics = {pub[0]: pub[1] for pub in added_publishers.added}

                    for t in added_subscribers:
                        added_topics[t[0]] = added_topics.get(t[0], []) + t[1]

                    topics_if_dt = self.topics_pool.update(added_topics, added_topic_types)
                    # self.reset_system_state(  # here we need to get only the nodes' names
                    #         [[k, [n[0] for n in nset]] for k, nset in added_publishers.iteritems()],
                    #         [[k, [n[0] for n in nset]] for k, nset in added_subscribers.iteritems()],
                    #         [[k, [n[0] for n in nset]] for k, nset in added_services.iteritems()],
                    #         added_topic_types,
                    #         added_service_types
                    # )
                    # we still need to return a diff to report on our behavior
                    # update() will compute diffs and do the job for us
                    # dt = super(RosInterface, self).update()
                else:  # if we have any change, we process it
                    # converting data format. Here we want only the names/keys.
                    # Resolving the details will be done as usual

                    # here we need to get only the nodes' names
                    services_dt = DiffTuple(
                        added=[[k, [n[0] for n in nset]] for k, nset in added_services.iteritems()],
                        removed=[[k, [n[0] for n in nset]] for k, nset in removed_services.iteritems()]
                    )

                    service_types_dt = DiffTuple(
                        added=added_service_types,
                        removed=removed_service_types
                    )

                    services_if_dt = self.services_pool.update_delta(services_dt, service_types_dt)

                    publishers_dt = DiffTuple(
                        added=[[k, [n[0] for n in nset]] for k, nset in added_publishers.iteritems()],
                        removed=[[k, [n[0] for n in nset]] for k, nset in removed_publishers.iteritems()]
                    )
                    subscribers_dt = DiffTuple(
                        added=[[k, [n[0] for n in nset]] for k, nset in added_subscribers.iteritems()],
                        removed=[[k, [n[0] for n in nset]] for k, nset in removed_subscribers.iteritems()]
                    )

                    added_topics = {pub[0]: pub[1] for pub in publishers_dt.added}
                    removed_topics = {pub[0]: pub[1] for pub in publishers_dt.removed}

                    for t in subscribers_dt.added:
                        added_topics[t[0]] = added_topics.get(t[0], []) + t[1]
                    for t in subscribers_dt.removed:
                        removed_topics[t[0]] = removed_topics.get(t[0], []) + t[1]

                    topics_dt = DiffTuple(
                        added=added_topics,
                        removed=removed_topics
                    )

                    topic_types_dt = DiffTuple(
                        added=added_topic_types,
                        removed=removed_topic_types
                    )

                    topics_if_dt = self.topics_pool.update_delta(topics_dt, topic_types_dt)

                    #OLD
                    #services_dt, topics_dt = self.compute_system_state(publishers_dt, subscribers_dt, services_dt, topic_types_dt, service_types_dt)

                    if topics_if_dt.added or topics_if_dt.removed:
                        self._debug_logger.debug(rospy.get_name() + " Pyros.rosinterface : Topics Delta {topics_if_dt}".format(**locals()))
                    if services_if_dt.added or services_if_dt.removed:
                        self._debug_logger.debug(rospy.get_name() + " Pyros.rosinterface : Services Delta {services_if_dt}".format(**locals()))

                    # TODO : put that in debug log and show based on python logger configuration
                    # print("Pyros ROS interface UPDATE")
                    # print("Params ADDED : {0}".format([p for p in params_dt.added]))
                    # print("Params GONE : {0}".format([p for p in params_dt.removed]))
                    # print("Topics ADDED : {0}".format([t[0] for t in topics_dt.added] + early_topics_dt.added))
                    # print("Topics GONE : {0}".format([t[0] for t in topics_dt.removed] + early_topics_dt.removed))
                    # print("Srvs ADDED: {0}".format([s[0] for s in services_dt.added]))
                    # print("Srvs GONE: {0}".format([s[0] for s in services_dt.removed]))

                    # update_on_diff wants only names
                    # dt = super(RosInterface, self).update_on_diff(
                    #         DiffTuple([s[0] for s in services_dt.added], [s[0] for s in services_dt.removed]),
                    #         DiffTuple([t[0] for t in topics_dt.added] + early_topics_dt.added, [t[0] for t in topics_dt.removed] + early_topics_dt.removed),
                    #         # Careful params_dt has a different content than service and topics, due to different ROS API
                    #         # TODO : make this as uniform as possible
                    #         DiffTuple([p for p in params_dt.added], [p for p in params_dt.removed])
                    # )

        else:  # default retrieve full system state (cache or master otherwise)

            publishers, subscribers, services, params, topic_types, service_types = self.retrieve_system_state()  # This will call the master if needed

            self._debug_logger.debug("""SYSTEM STATE :
                - publishers : {publishers}
                - subscribers : {subscribers}
                - services : {services}
                - topic_types : {topic_types}
                - service_types : {service_types}
            """.format(**locals()))

            #TODO : unify with the reset behavior in case of cache...

            # Needs to be done first, since topic algorithm depends on it
            print("PARAMS : {params}".format(**locals()))
            params_if_dt = self.params_pool.update(params=params)
            print("PARAM IF DT : {params_if_dt}".format(**locals()))

            print("SERVICES : {services}".format(**locals()))
            services_if_dt = self.services_pool.update(services, service_types)
            print("SERVICE IF DT : {services_if_dt}".format(**locals()))

            # TODO : separate pubs and subs
            topics = publishers
            # TODO : passing dictionaries everywhere will avoid this mess...
            found = False
            for s in subscribers:
                found = True
                for t in topics:
                    if t[0] == s[0] and t[1] != s[1]:
                        t[1] += s[1]
                        found = True
                        break
                if not found:
                    topics.append(s)

            # CAREFUL, topic interface by itself also makes the topic detected on system
            # Check if there are any pyros interface with it and ignore them
            topics_if = TopicBack.get_all_interfaces()
            drop_list = {}
            for node, tifs in topics_if.iteritems():
                if node == rospy.get_name():
                    # For our interface, only ignore if the ref_count is == 1
                    # More means we have another pub|sub instance somewhere, and we should reflect it as part of the system.
                    # This is used by tests for example, to simulate a pub|sub without having to spawn a different process.
                    # But IT IS NOT A NORMAL USECASE. For pyros to work multiprocess it needs to stick to one interface instance per process.
                    for tifname, tifon in tifs.iteritems():
                        # In theory : tifon is False <=> ref_count == 0
                        # but if not tifon, we probably want to keep the topic in the list...
                        if tifon and TopicBack.get_impl_ref_count(tifname) <= 1:
                            #Note we could also : self.topics_pool.available[tname].get_impl_ref_count()
                            # We can drop this node from topics list
                            drop_list[tifname] = drop_list.get(tifname, []) + [node]
                        # elif not tifon:
                        #     # useful to drop lingering publishers
                        #     drop_list[tifname] += [node]
                else:
                    # For other pyros interface we can only assume they intend to drop the interface as soon as the system lose that topic
                    # And we are not interested in communication between pyros instances.
                    # We can drop this node from topics list, for all topics
                    for tifname, tifon in tifs.iteritems():
                        if tifon:
                            drop_list[tifname] = drop_list.get(tifname, []) + [node]
                        # elif not tifon: # if not tifon, the presence of the node does NOT reflect an interface...
                        #     # but maybe useful to drop lingering publishers ?
                        #     drop_list[tifname] += [node]


            #filtering the topic list
            for td in topics:
                td[1] = [n for n in td[1] if n not in drop_list.get(td[0], [])]

            topics = [td for td in topics if td[1]]  # filtering out topics with no endpoints

            print("TOPICS : {topics}".format(**locals()))
            topics_if_dt = self.topics_pool.update(topics, topic_types)
            print("TOPIC IF DT : {topics_if_dt}".format(**locals()))

        dt = DiffTuple(
            added=params_if_dt.added + services_if_dt.added + topics_if_dt.added,
            removed=params_if_dt.removed + services_if_dt.removed + topics_if_dt.removed
        )

        self._debug_logger.debug("""
            ROS INTERFACE ADDED : {dt.added}
            ROS INTERFACE REMOVED : {dt.removed}
        """.format(**locals()))

        return dt

    def _proxy_cb(self, system_state, added_system_state, lost_system_state):
        with self.cb_lock:
            self.cb_ss.put(system_state)

            self.cb_ss_dt.put(DiffTuple(
                added=added_system_state,
                removed=lost_system_state
            ))

BaseInterface.register(RosInterface)


