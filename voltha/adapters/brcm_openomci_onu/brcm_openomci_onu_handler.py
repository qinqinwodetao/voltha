#
# Copyright 2017 the original author or authors.
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
#

"""
Broadcom OpenOMCI OLT/ONU adapter handler.
"""

import structlog
from twisted.internet import reactor, task
from twisted.internet.defer import DeferredQueue, inlineCallbacks, returnValue, TimeoutError

from heartbeat import HeartBeat
from voltha.extensions.kpi.onu.onu_pm_metrics import OnuPmMetrics
from voltha.extensions.kpi.onu.onu_omci_pm import OnuOmciPmMetrics

from common.utils.indexpool import IndexPool
import voltha.core.flow_decomposer as fd
from voltha.registry import registry
from voltha.protos import third_party
from voltha.protos.common_pb2 import OperStatus, ConnectStatus, AdminState
from voltha.protos.openflow_13_pb2 import OFPXMC_OPENFLOW_BASIC, ofp_port
from voltha.protos.bbf_fiber_tcont_body_pb2 import TcontsConfigData
from voltha.protos.bbf_fiber_gemport_body_pb2 import GemportsConfigData
from voltha.extensions.omci.onu_configuration import OMCCVersion
from voltha.extensions.omci.onu_device_entry import OnuDeviceEvents, \
            OnuDeviceEntry, IN_SYNC_KEY
from voltha.adapters.brcm_openomci_onu.omci.brcm_mib_download_task import BrcmMibDownloadTask
from voltha.adapters.brcm_openomci_onu.omci.brcm_uni_lock_task import BrcmUniLockTask
from voltha.adapters.brcm_openomci_onu.omci.brcm_vlan_filter_task import BrcmVlanFilterTask
from voltha.adapters.brcm_openomci_onu.onu_gem_port import *
from voltha.adapters.brcm_openomci_onu.onu_tcont import *
from voltha.adapters.brcm_openomci_onu.pon_port import *
from voltha.adapters.brcm_openomci_onu.uni_port import *
from voltha.adapters.brcm_openomci_onu.onu_traffic_descriptor import *

OP = EntityOperations
RC = ReasonCodes


_ = third_party
log = structlog.get_logger()


BRDCM_DEFAULT_VLAN = 4091
ADMIN_STATE_LOCK = 1
ADMIN_STATE_UNLOCK = 0
RESERVED_VLAN_ID = 4095
_STARTUP_RETRY_WAIT = 20
_MAXIMUM_PORT = 128          # UNI ports



_ = third_party



class BrcmOpenomciOnuHandler(object):

    def __init__(self, adapter, device_id):
        self.log = structlog.get_logger(device_id=device_id)
        self.log.debug('function-entry')
        self.adapter = adapter
        self.adapter_agent = adapter.adapter_agent
        self.parent_adapter = None
        self.parent_id = None
        self.device_id = device_id
        self.incoming_messages = DeferredQueue()
        self.event_messages = DeferredQueue()
        self.proxy_address = None
        self.tx_id = 0
        self._enabled = False
        self.pm_metrics = None
        self._omcc_version = OMCCVersion.Unknown
        self._total_tcont_count = 0  # From ANI-G ME
        self._qos_flexibility = 0  # From ONT2_G ME

        self._onu_indication = None
        self._unis = dict()  # Port # -> UniPort
        self._port_number_pool = IndexPool(_MAXIMUM_PORT, 0)

        self._pon = None
        #TODO: probably shouldnt be hardcoded, determine from olt maybe?
        self._pon_port_number = 100
        self.logical_device_id = None

        self._heartbeat = HeartBeat.create(self, device_id)

        # Set up OpenOMCI environment
        self._onu_omci_device = None
        self._dev_info_loaded = False
        self._deferred = None

        self._in_sync_subscription = None
        self._connectivity_subscription = None
        self._capabilities_subscription = None

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        if self._enabled != value:
            self._enabled = value

    @property
    def omci_agent(self):
        return self.adapter.omci_agent

    @property
    def omci_cc(self):
        return self._onu_omci_device.omci_cc if self._onu_omci_device is not None else None

    @property
    def heartbeat(self):
        return self._heartbeat

    @property
    def uni_ports(self):
        return self._unis.values()

    def uni_port(self, port_no_or_name):
        if isinstance(port_no_or_name, (str, unicode)):
            return next((uni for uni in self.uni_ports
                         if uni.name == port_no_or_name), None)

        assert isinstance(port_no_or_name, int), 'Invalid parameter type'
        return self._unis.get(port_no_or_name)

    @property
    def pon_port(self):
        return self._pon

    @property
    def _next_port_number(self):
        return self._port_number_pool.get_next()

    def _release_port_number(self, number):
        self._port_number_pool.release(number)

    def receive_message(self, msg):
        if self.omci_cc is not None:
            self.omci_cc.receive_message(msg)

    # Called once when the adapter creates the device/onu instance
    def activate(self, device):
        self.log.debug('function-entry', device=device)

        # first we verify that we got parent reference and proxy info
        assert device.parent_id
        assert device.proxy_address.device_id

        # register for proxied messages right away
        self.proxy_address = device.proxy_address
        self.adapter_agent.register_for_proxied_messages(device.proxy_address)
        self.parent_id = device.parent_id
        parent_device = self.adapter_agent.get_device(self.parent_id)
        if parent_device.type == 'openolt':
            self.parent_adapter = registry('adapter_loader').\
                                  get_agent(parent_device.adapter).adapter

        if self.enabled is not True:
            self.log.info('activating-new-onu')
            # populate what we know.  rest comes later after mib sync
            device.root = True
            device.vendor = 'Broadcom'
            device.connect_status = ConnectStatus.REACHABLE
            device.oper_status = OperStatus.DISCOVERED
            device.reason = 'activating-onu'

            # pm_metrics requires a logical device id
            parent_device = self.adapter_agent.get_device(device.parent_id)
            self.logical_device_id = parent_device.parent_id
            assert self.logical_device_id, 'Invalid logical device ID'

            self.adapter_agent.update_device(device)

            self.log.debug('set-device-discovered')

            self._init_pon_state(device)

            ############################################################################
            # Setup PM configuration for this device
            # Pass in ONU specific options
            kwargs = {
                OnuPmMetrics.DEFAULT_FREQUENCY_KEY: OnuPmMetrics.DEFAULT_ONU_COLLECTION_FREQUENCY,
                'heartbeat': self.heartbeat,
                OnuOmciPmMetrics.OMCI_DEV_KEY: self._onu_omci_device
            }
            self.pm_metrics = OnuPmMetrics(self.adapter_agent, self.device_id,
                                           self.logical_device_id, grouped=True,
                                           freq_override=False, **kwargs)
            pm_config = self.pm_metrics.make_proto()
            self._onu_omci_device.set_pm_config(self.pm_metrics.omci_pm.openomci_interval_pm)
            self.log.info("initial-pm-config", pm_config=pm_config)
            self.adapter_agent.update_device_pm_config(pm_config, init=True)

            self.enabled = True
        else:
            self.log.info('onu-already-activated')


    # Called once when the adapter needs to re-create device.  usually on vcore restart
    def reconcile(self, device):
        self.log.debug('function-entry', device=device)

        # first we verify that we got parent reference and proxy info
        assert device.parent_id
        assert device.proxy_address.device_id

        # register for proxied messages right away
        self.proxy_address = device.proxy_address
        self.adapter_agent.register_for_proxied_messages(device.proxy_address)

        if self.enabled is not True:
            self.log.info('reconciling-broadcom-onu-device')

            self._init_pon_state(device)

            # need to restart state machines on vcore restart.  there is no indication to do it for us.
            self._onu_omci_device.start()
            device.reason = "restarting-openomci"
            self.adapter_agent.update_device(device)

            # TODO: this is probably a bit heavy handed
            # Force a reboot for now.  We need indications to reflow to reassign tconts and gems given vcore went away
            # This may not be necessary when mib resync actually works
            reactor.callLater(1, self.reboot)

            self.enabled = True
        else:
            self.log.info('onu-already-activated')


    def _init_pon_state(self, device):
        self.log.debug('function-entry', device=device)

        self._pon = PonPort.create(self, self._pon_port_number)
        self.adapter_agent.add_port(device.id, self._pon.get_port())

        self.log.debug('added-pon-port-to-agent', pon=self._pon)

        parent_device = self.adapter_agent.get_device(device.parent_id)
        self.logical_device_id = parent_device.parent_id

        self.adapter_agent.update_device(device)

        # Create and start the OpenOMCI ONU Device Entry for this ONU
        self._onu_omci_device = self.omci_agent.add_device(self.device_id,
                                                           self.adapter_agent,
                                                           support_classes=self.adapter.broadcom_omci,
                                                           custom_me_map=self.adapter.custom_me_entities())
        # Port startup
        if self._pon is not None:
            self._pon.enabled = True

    # TODO: move to UniPort
    def update_logical_port(self, logical_device_id, port_id, state):
        try:
            self.log.info('updating-logical-port', logical_port_id=port_id,
                          logical_device_id=logical_device_id, state=state)
            logical_port = self.adapter_agent.get_logical_port(logical_device_id,
                                                               port_id)
            logical_port.ofp_port.state = state
            self.adapter_agent.update_logical_port(logical_device_id,
                                                   logical_port)
        except Exception as e:
            self.log.exception("exception-updating-port",e=e)

    def delete(self, device):
        self.log.info('delete-onu', device=device)
        if self.parent_adapter:
            try:
                self.parent_adapter.delete_child_device(self.parent_id, device)
            except AttributeError:
                self.log.debug('parent-device-delete-child-not-implemented')
        else:
            self.log.debug("parent-adapter-not-available")

    def update_pm_config(self, device, pm_config):
        # TODO: This has not been tested
        self.log.info('update_pm_config', pm_config=pm_config)
        self.pm_metrics.update(pm_config)

    # Calling this assumes the onu is active/ready and had at least an initial mib downloaded.   This gets called from
    # flow decomposition that ultimately comes from onos
    @inlineCallbacks
    def update_flow_table(self, device, flows):
        self.log.debug('function-entry', device=device, flows=flows)
        #
        # We need to proxy through the OLT to get to the ONU
        # Configuration from here should be using OMCI
        #
        #self.log.info('bulk-flow-update', device_id=device.id, flows=flows)

        def is_downstream(port):
            return port == self._pon_port_number

        def is_upstream(port):
            return not is_downstream(port)

        for flow in flows:
            _type = None
            _port = None
            _vlan_vid = None
            _udp_dst = None
            _udp_src = None
            _ipv4_dst = None
            _ipv4_src = None
            _metadata = None
            _output = None
            _push_tpid = None
            _field = None
            _set_vlan_vid = None
            self.log.debug('bulk-flow-update', device_id=device.id, flow=flow)
            try:
                _in_port = fd.get_in_port(flow)
                assert _in_port is not None

                if is_downstream(_in_port):
                    self.log.debug('downstream-flow')
                elif is_upstream(_in_port):
                    self.log.debug('upstream-flow')
                else:
                    raise Exception('port should be 1 or 2 by our convention')

                _out_port = fd.get_out_port(flow)  # may be None
                self.log.debug('out-port', out_port=_out_port)

                for field in fd.get_ofb_fields(flow):
                    if field.type == fd.ETH_TYPE:
                        _type = field.eth_type
                        self.log.debug('field-type-eth-type',
                                      eth_type=_type)

                    elif field.type == fd.IP_PROTO:
                        _proto = field.ip_proto
                        self.log.debug('field-type-ip-proto',
                                      ip_proto=_proto)

                    elif field.type == fd.IN_PORT:
                        _port = field.port
                        self.log.debug('field-type-in-port',
                                      in_port=_port)

                    elif field.type == fd.VLAN_VID:
                        _vlan_vid = field.vlan_vid & 0xfff
                        self.log.debug('field-type-vlan-vid',
                                      vlan=_vlan_vid)

                    elif field.type == fd.VLAN_PCP:
                        _vlan_pcp = field.vlan_pcp
                        self.log.debug('field-type-vlan-pcp',
                                      pcp=_vlan_pcp)

                    elif field.type == fd.UDP_DST:
                        _udp_dst = field.udp_dst
                        self.log.debug('field-type-udp-dst',
                                      udp_dst=_udp_dst)

                    elif field.type == fd.UDP_SRC:
                        _udp_src = field.udp_src
                        self.log.debug('field-type-udp-src',
                                      udp_src=_udp_src)

                    elif field.type == fd.IPV4_DST:
                        _ipv4_dst = field.ipv4_dst
                        self.log.debug('field-type-ipv4-dst',
                                      ipv4_dst=_ipv4_dst)

                    elif field.type == fd.IPV4_SRC:
                        _ipv4_src = field.ipv4_src
                        self.log.debug('field-type-ipv4-src',
                                      ipv4_dst=_ipv4_src)

                    elif field.type == fd.METADATA:
                        _metadata = field.table_metadata
                        self.log.debug('field-type-metadata',
                                      metadata=_metadata)

                    else:
                        raise NotImplementedError('field.type={}'.format(
                            field.type))

                for action in fd.get_actions(flow):

                    if action.type == fd.OUTPUT:
                        _output = action.output.port
                        self.log.debug('action-type-output',
                                      output=_output, in_port=_in_port)

                    elif action.type == fd.POP_VLAN:
                        self.log.debug('action-type-pop-vlan',
                                      in_port=_in_port)

                    elif action.type == fd.PUSH_VLAN:
                        _push_tpid = action.push.ethertype
                        self.log.debug('action-type-push-vlan',
                                 push_tpid=_push_tpid, in_port=_in_port)
                        if action.push.ethertype != 0x8100:
                            self.log.error('unhandled-tpid',
                                           ethertype=action.push.ethertype)

                    elif action.type == fd.SET_FIELD:
                        _field = action.set_field.field.ofb_field
                        assert (action.set_field.field.oxm_class ==
                                OFPXMC_OPENFLOW_BASIC)
                        self.log.debug('action-type-set-field',
                                      field=_field, in_port=_in_port)
                        if _field.type == fd.VLAN_VID:
                            _set_vlan_vid = _field.vlan_vid & 0xfff
                            self.log.debug('set-field-type-vlan-vid', _set_vlan_vid)
                        else:
                            self.log.error('unsupported-action-set-field-type',
                                           field_type=_field.type)
                    else:
                        self.log.error('unsupported-action-type',
                                  action_type=action.type, in_port=_in_port)

                # TODO: We only set vlan omci flows.  Handle omci matching ethertypes at some point in another task
                if _type is not None:
                    self.log.warn('ignoring-flow-with-ethType', ethType=_type)
                elif _set_vlan_vid is None or _set_vlan_vid == 0:
                    self.log.warn('ignorning-flow-that-does-not-set-vlanid')
                else:
                    self._add_vlan_filter_task(device, _set_vlan_vid)


            except Exception as e:
                self.log.exception('failed-to-install-flow', e=e, flow=flow)

    def _add_vlan_filter_task(self, device, _set_vlan_vid):
        def success(_results):
            self.log.info('vlan-tagging-success', _results=_results)
            device.reason = 'omci-flows-pushed'
            self._vlan_filter_task = None

        def failure(_reason):
            self.log.warn('vlan-tagging-failure', _reason=_reason)
            device.reason = 'omci-flows-failed-retrying'
            self._vlan_filter_task = reactor.callLater(_STARTUP_RETRY_WAIT,
                                                       self._add_vlan_filter_task, device, _set_vlan_vid)
        self.log.info('setting-vlan-tag')
        self._vlan_filter_task = BrcmVlanFilterTask(self.omci_agent, self.device_id, _set_vlan_vid)
        self._deferred = self._onu_omci_device.task_runner.queue_task(self._vlan_filter_task)
        self._deferred.addCallbacks(success, failure)

    def get_tx_id(self):
        self.log.debug('function-entry')
        self.tx_id += 1
        return self.tx_id

    # TODO: Actually conform to or create a proper interface.
    # this and the other functions called from the olt arent very clear.
    # Called each time there is an onu "up" indication from the olt handler
    def create_interface(self, data):
        self.log.debug('function-entry', data=data)
        self._onu_indication = data

        onu_device = self.adapter_agent.get_device(self.device_id)

        self.log.debug('starting-openomci-statemachine')
        self._subscribe_to_events()
        reactor.callLater(1, self._onu_omci_device.start)
        onu_device.reason = "starting-openomci"
        self.adapter_agent.update_device(onu_device)
        self._heartbeat.enabled = True

    # Currently called each time there is an onu "down" indication from the olt handler
    # TODO: possibly other reasons to "update" from the olt?
    def update_interface(self, data):
        self.log.debug('function-entry', data=data)
        oper_state = data.get('oper_state', None)

        onu_device = self.adapter_agent.get_device(self.device_id)

        if oper_state == 'down':
            self.log.debug('stopping-openomci-statemachine')
            reactor.callLater(0, self._onu_omci_device.stop)
            self.disable_ports(onu_device)
            onu_device.reason = "stopping-openomci"
            onu_device.connect_status = ConnectStatus.UNREACHABLE
            onu_device.oper_status = OperStatus.DISCOVERED
            self.adapter_agent.update_device(onu_device)
        else:
            self.log.debug('not-changing-openomci-statemachine')

    # Not currently called by olt or anything else
    def remove_interface(self, data):
        self.log.debug('function-entry', data=data)

        onu_device = self.adapter_agent.get_device(self.device_id)

        self.log.debug('stopping-openomci-statemachine')
        reactor.callLater(0, self._onu_omci_device.stop)
        self.disable_ports(onu_device)
        onu_device.reason = "stopping-openomci"
        self.adapter_agent.update_device(onu_device)

        # TODO: im sure there is more to do here

    # Called when there is an olt up indication, providing the gem port id chosen by the olt handler
    def create_gemport(self, data):
        self.log.debug('create-gemport', data=data)
        gem_portdata = GemportsConfigData()
        gem_portdata.CopyFrom(data)

        # TODO: fill in what i have.  This needs to be provided from the OLT
        # currently its hardcoded/static
        gemdict = dict()
        gemdict['gemport-id'] = gem_portdata.gemport_id
        gemdict['encryption'] = gem_portdata.aes_indicator
        gemdict['tcont-ref'] = int(gem_portdata.tcont_ref)
        gemdict['name'] = gem_portdata.gemport_id
        gemdict['traffic-class'] = gem_portdata.traffic_class
        gemdict['traffic-class'] = gem_portdata.traffic_class

        gem_port = OnuGemPort.create(self, gem_port=gemdict, entity_id=self._pon.next_gem_entity_id)

        self._pon.add_gem_port(gem_port)

        self.log.debug('pon-add-gemport', gem_port=gem_port)


    # Not currently called.  Would be called presumably from the olt handler
    @inlineCallbacks
    def remove_gemport(self, data):
        self.log.debug('remove-gemport', data=data)
        gem_port = GemportsConfigData()
        gem_port.CopyFrom(data)
        device = self.adapter_agent.get_device(self.device_id)
        if device.connect_status != ConnectStatus.REACHABLE:
            self.log.error('device-unreachable')
            returnValue(None)

        #TODO: Create a remove task that encompasses this

    # Called when there is an olt up indication, providing the tcont id chosen by the olt handler
    def create_tcont(self, tcont_data, traffic_descriptor_data):
        self.log.debug('create-tcont', tcont_data=tcont_data, traffic_descriptor_data=traffic_descriptor_data)
        tcontdata = TcontsConfigData()
        tcontdata.CopyFrom(tcont_data)

        # TODO: fill in what i have.  This needs to be provided from the OLT
        # currently its hardcoded/static
        tcontdict = dict()
        tcontdict['alloc-id'] = tcontdata.alloc_id
        tcontdict['name'] = tcontdata.name
        tcontdict['vont-ani'] = tcontdata.interface_reference

        # TODO: Not sure what to do with any of this...
        tddata = dict()
        tddata['name'] = 'not-sure-td-profile'
        tddata['fixed-bandwidth'] = "not-sure-fixed"
        tddata['assured-bandwidth'] = "not-sure-assured"
        tddata['maximum-bandwidth'] = "not-sure-max"
        tddata['additional-bw-eligibility-indicator'] = "not-sure-additional"

        td = OnuTrafficDescriptor.create(tddata)
        tcont = OnuTCont.create(self, tcont=tcontdict, td=td)

        self._pon.add_tcont(tcont)

        self.log.debug('pon-add-tcont', tcont=tcont)

        if tcontdata.interface_reference is not None:
            self.log.debug('tcont', tcont=tcont.alloc_id)
        else:
            self.log.info('received-null-tcont-data', tcont=tcont.alloc_id)

    # Not currently called.  Would be called presumably from the olt handler
    @inlineCallbacks
    def remove_tcont(self, tcont_data, traffic_descriptor_data):
        self.log.debug('remove-tcont', tcont_data=tcont_data, traffic_descriptor_data=traffic_descriptor_data)
        device = self.adapter_agent.get_device(self.device_id)
        if device.connect_status != ConnectStatus.REACHABLE:
            self.log.error('device-unreachable')
            returnValue(None)

        # TODO: Create some omci task that encompases this what intended

    # Not currently called.  Would be called presumably from the olt handler
    def create_multicast_gemport(self, data):
        self.log.debug('function-entry', data=data)

        # TODO: create objects and populate for later omci calls

    @inlineCallbacks
    def disable(self, device):
        self.log.debug('function-entry', device=device)
        try:
            self.log.info('sending-uni-lock-towards-device', device=device)

            def stop_anyway(reason):
                # proceed with disable regardless if we could reach the onu. for example onu is unplugged
                self.log.debug('stopping-openomci-statemachine')
                reactor.callLater(0, self._onu_omci_device.stop)
                self.disable_ports(device)
                device.oper_status = OperStatus.UNKNOWN
                device.reason = "omci-admin-lock"
                self.adapter_agent.update_device(device)

            # lock all the unis
            task = BrcmUniLockTask(self.omci_agent, self.device_id, lock=True)
            self._deferred = self._onu_omci_device.task_runner.queue_task(task)
            self._deferred.addCallbacks(stop_anyway, stop_anyway)
        except Exception as e:
            log.exception('exception-in-onu-disable', exception=e)

    @inlineCallbacks
    def reenable(self, device):
        self.log.debug('function-entry', device=device)
        try:
            # Start up OpenOMCI state machines for this device
            # this will ultimately resync mib and unlock unis on successful redownloading the mib
            self.log.debug('restarting-openomci-statemachine')
            self._subscribe_to_events()
            device.reason = "restarting-openomci"
            self.adapter_agent.update_device(device)
            reactor.callLater(1, self._onu_omci_device.start)
            self._heartbeat.enabled = True
        except Exception as e:
            log.exception('exception-in-onu-reenable', exception=e)

    @inlineCallbacks
    def reboot(self):
        self.log.info('reboot-device')
        device = self.adapter_agent.get_device(self.device_id)
        if device.connect_status != ConnectStatus.REACHABLE:
            self.log.error("device-unreachable")
            returnValue(None)

        def success(_results):
            self.log.info('reboot-success', _results=_results)
            self.disable_ports(device)
            device.connect_status = ConnectStatus.UNREACHABLE
            device.oper_status = OperStatus.DISCOVERED
            device.reason = "rebooting"
            self.adapter_agent.update_device(device)

        def failure(_reason):
            self.log.info('reboot-failure', _reason=_reason)

        self._deferred = self._onu_omci_device.reboot()
        self._deferred.addCallbacks(success, failure)

    def disable_ports(self, onu_device):
        self.log.info('disable-ports', device_id=self.device_id,
                   onu_device=onu_device)

        # Disable all ports on that device
        self.adapter_agent.disable_all_ports(self.device_id)

        parent_device = self.adapter_agent.get_device(onu_device.parent_id)
        assert parent_device
        logical_device_id = parent_device.parent_id
        assert logical_device_id
        ports = self.adapter_agent.get_ports(onu_device.id, Port.ETHERNET_UNI)
        for port in ports:
            port_id = 'uni-{}'.format(port.port_no)
            # TODO: move to UniPort
            self.update_logical_port(logical_device_id, port_id, OFPPS_LINK_DOWN)

    def enable_ports(self, onu_device):
        self.log.info('enable-ports', device_id=self.device_id, onu_device=onu_device)

        # Disable all ports on that device
        self.adapter_agent.enable_all_ports(self.device_id)

        parent_device = self.adapter_agent.get_device(onu_device.parent_id)
        assert parent_device
        logical_device_id = parent_device.parent_id
        assert logical_device_id
        ports = self.adapter_agent.get_ports(onu_device.id, Port.ETHERNET_UNI)
        for port in ports:
            port_id = 'uni-{}'.format(port.port_no)
            # TODO: move to UniPort
            self.update_logical_port(logical_device_id, port_id, OFPPS_LIVE)


    # Called just before openomci state machine is started.  These listen for events from selected state machines,
    # most importantly, mib in sync.  Which ultimately leads to downloading the mib
    def _subscribe_to_events(self):
        self.log.debug('function-entry')

        # OMCI MIB Database sync status
        bus = self._onu_omci_device.event_bus
        topic = OnuDeviceEntry.event_bus_topic(self.device_id,
                                               OnuDeviceEvents.MibDatabaseSyncEvent)
        self._in_sync_subscription = bus.subscribe(topic, self.in_sync_handler)

        # OMCI Capabilities
        bus = self._onu_omci_device.event_bus
        topic = OnuDeviceEntry.event_bus_topic(self.device_id,
                                               OnuDeviceEvents.OmciCapabilitiesEvent)
        self._capabilities_subscription = bus.subscribe(topic, self.capabilties_handler)

    # Called when the mib is in sync
    def in_sync_handler(self, _topic, msg):
        self.log.debug('function-entry', _topic=_topic, msg=msg)
        if self._in_sync_subscription is not None:
            try:
                in_sync = msg[IN_SYNC_KEY]

                if in_sync:
                    # Only call this once
                    bus = self._onu_omci_device.event_bus
                    bus.unsubscribe(self._in_sync_subscription)
                    self._in_sync_subscription = None

                    # Start up device_info load
                    self.log.debug('running-mib-sync')
                    reactor.callLater(0, self._mib_in_sync)

            except Exception as e:
                self.log.exception('in-sync', e=e)

    def capabilties_handler(self, _topic, _msg):
        self.log.debug('function-entry', _topic=_topic, msg=_msg)
        if self._capabilities_subscription is not None:
            self.log.debug('capabilities-handler-done')

    # Mib is in sync, we can now query what we learned and actually start pushing ME (download) to the ONU.
    # Currently uses a basic mib download task that create a bridge with a single gem port and uni, only allowing EAP
    # Implement your own MibDownloadTask if you wish to setup something different by default
    def _mib_in_sync(self):
        self.log.debug('function-entry')

        omci = self._onu_omci_device
        in_sync = omci.mib_db_in_sync

        device = self.adapter_agent.get_device(self.device_id)
        device.reason = 'discovery-mibsync-complete'
        self.adapter_agent.update_device(device)

        if not self._dev_info_loaded:
            self.log.info('loading-device-data-from-mib', in_sync=in_sync, already_loaded=self._dev_info_loaded)

            omci_dev = self._onu_omci_device
            config = omci_dev.configuration

            # TODO: run this sooner somehow. shouldnt have to wait for mib sync to push an initial download
            # In Sync, we can register logical ports now. Ideally this could occur on
            # the first time we received a successful (no timeout) OMCI Rx response.
            try:

                ani_g = config.ani_g_entities
                uni_g = config.uni_g_entities
                pptp = config.pptp_entities

                # TODO: there are onu out there that have uni ports available but no pptp.  These all-in-one devices
                # likely need virtual ethernet interface point ME #329 configured rather than pptp.
                if ani_g is None or uni_g is None or pptp is None:
                    device.reason = 'onu-missing-required-elements'
                    self.adapter_agent.update_device(device)
                    raise Exception("onu-missing-required-elements")

                # Currently logging the ani and uni for information purposes.   Actually act on the pptp as its ME ID
                # is the most correct one to use in later tasks.
                for key, value in ani_g.iteritems():
                    self.log.debug("discovered-ani", key=key, value=value)

                for key, value in uni_g.iteritems():
                    self.log.debug("discovered-uni", key=key, value=value)

                for key, value in pptp.iteritems():
                    self.log.debug("discovered-pptp-uni", key=key, value=value)
                    entity_id = key
                    self._add_uni_port(entity_id)

                    # TODO: only one uni/pptp for now. flow bug in openolt
                    break

                self._total_tcont_count = ani_g.get('total-tcont-count')
                self._qos_flexibility = config.qos_configuration_flexibility or 0
                self._omcc_version = config.omcc_version or OMCCVersion.Unknown
                self.log.debug("set-total-tcont-count", tcont_count=self._total_tcont_count)

                self._dev_info_loaded = True

            except Exception as e:
                self.log.exception('device-info-load', e=e)
                self._deferred = reactor.callLater(_STARTUP_RETRY_WAIT, self._mib_in_sync)

        else:
            self.log.info('device-info-already-loaded', in_sync=in_sync, already_loaded=self._dev_info_loaded)

        if self._dev_info_loaded:
            if device.admin_state == AdminState.ENABLED:
                def success(_results):
                    self.log.info('mib-download-success', _results=_results)
                    device = self.adapter_agent.get_device(self.device_id)
                    device.reason = 'initial-mib-downloaded'
                    device.oper_status = OperStatus.ACTIVE
                    device.connect_status = ConnectStatus.REACHABLE
                    self.enable_ports(device)
                    self.adapter_agent.update_device(device)
                    self._mib_download_task = None

                def failure(_reason):
                    self.log.warn('mib-download-failure-retrying', _reason=_reason)
                    device.reason = 'initial-mib-download-failure-retrying'
                    self.adapter_agent.update_device(device)
                    self._deferred = reactor.callLater(_STARTUP_RETRY_WAIT, self._mib_in_sync)

                # Download an initial mib that creates simple bridge that can pass EAP.  On success (above) finally set
                # the device to active/reachable.   This then opens up the handler to openflow pushes from outside
                self.log.info('downloading-initial-mib-configuration')
                self._mib_download_task = BrcmMibDownloadTask(self.omci_agent, self)
                self._deferred = self._onu_omci_device.task_runner.queue_task(self._mib_download_task)
                self._deferred.addCallbacks(success, failure)
            else:
                self.log.info('admin-down-disabling')
                self.disable(device)
        else:
            self.log.info('device-info-not-loaded-skipping-mib-download')


    def _add_uni_port(self, entity_id):
        self.log.debug('function-entry')

        device = self.adapter_agent.get_device(self.device_id)
        parent_device = self.adapter_agent.get_device(device.parent_id)

        parent_adapter_agent = registry('adapter_loader').get_agent(parent_device.adapter)
        if parent_adapter_agent is None:
            self.log.error('openolt_adapter_agent-could-not-be-retrieved')

        # TODO: This knowledge is locked away in openolt.  and it assumes one onu equals one uni...
        parent_device = self.adapter_agent.get_device(device.parent_id)
        parent_adapter = parent_adapter_agent.adapter.devices[parent_device.id]
        uni_no_start = parent_adapter.platform.mk_uni_port_num(
            self._onu_indication.intf_id, self._onu_indication.onu_id)

        # TODO: Some or parts of this likely need to move to UniPort. especially the format stuff
        working_port = self._next_port_number
        uni_no = uni_no_start + working_port
        uni_name = "uni-{}".format(uni_no)

        mac_bridge_port_num = working_port + 1

        self.log.debug('live-port-number-ready', uni_no=uni_no, uni_name=uni_name)

        uni_port = UniPort.create(self, uni_name, uni_no, uni_name, device.vlan, device.vlan)
        uni_port.entity_id = entity_id
        uni_port.enabled = True
        uni_port.mac_bridge_port_num = mac_bridge_port_num
        uni_port.add_logical_port(uni_port.port_number, subscriber_vlan=device.vlan)

        self.log.debug("created-uni-port", uni=uni_port)

        self.adapter_agent.add_port(device.id, uni_port.get_port())
        parent_adapter_agent.add_port(device.parent_id, uni_port.get_port())

        self._unis[uni_port.port_number] = uni_port

        # TODO: this should be in the PonPortclass
        pon_port = self._pon.get_port()
        self.adapter_agent.delete_port_reference_from_parent(self.device_id,
                                                             pon_port)

        pon_port.peers.extend([Port.PeerPort(device_id=device.parent_id,
                                             port_no=uni_port.port_number)])

        self._pon._port = pon_port

        self.adapter_agent.add_port_reference_to_parent(self.device_id,
                                                        pon_port)
        self.adapter_agent.update_device(device)
