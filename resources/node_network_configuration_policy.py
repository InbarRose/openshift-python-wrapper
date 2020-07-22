import logging

from openshift.dynamic.exceptions import ConflictError
from resources.utils import TimeoutExpiredError, TimeoutSampler

from .node_network_state import NodeNetworkState
from .resource import Resource


LOGGER = logging.getLogger(__name__)


class NNCPConfigurationFailed(Exception):
    pass


class NodeNetworkConfigurationPolicy(Resource):

    api_group = "nmstate.io"

    class Interface:
        class State:
            UP = "up"
            DOWN = "down"
            ABSENT = "absent"

    class Conditions:
        class Type:
            FAILING = "Failing"
            AVAILABLE = "Available"
            PROGRESSING = "Progressing"
            MATCHING = "Matching"

        class Reason:
            SUCCESS = "SuccessfullyConfigured"
            FAILED = "FailedToConfigure"

    def __init__(
        self,
        name,
        worker_pods=None,
        node_selector=None,
        teardown=True,
        mtu=None,
        ports=None,
        ipv4_enable=False,
        ipv4_dhcp=False,
        ipv4_addresses=None,
        ipv6_enable=False,
        node_active_nics=None,
    ):
        """
        ipv4_addresses should be sent in this format:
        [{"ip": <ip1-string>, "prefix-length": <prefix-len1-int>},
         {"ip": <ip2-string>, "prefix-length": <prefix-len2-int>}, ...]
        For example:
        [{"ip": "10.1.2.3", "prefix-length": 24},
         {"ip": "10.4.5.6", "prefix-length": 24},
         {"ip": "10.7.8.9", "prefix-length": 23}]
        """
        super().__init__(name=name, teardown=teardown)
        self.desired_state = {"interfaces": []}
        self.worker_pods = worker_pods
        self.mtu = mtu
        self.mtu_dict = {}
        self.ports = ports or []
        self.iface = None
        self.ifaces = []
        self.node_active_nics = node_active_nics or []
        self.ipv4_enable = ipv4_enable
        self._ipv4_dhcp = ipv4_dhcp
        self.ipv4_addresses = ipv4_addresses or []
        self.ipv6_enable = ipv6_enable
        self.ipv4_iface_state = {}
        self.node_selector = node_selector
        if self.node_selector:
            for pod in self.worker_pods:
                if pod.node.name == node_selector:
                    self.worker_pods = [pod]
                    self._node_selector = {"kubernetes.io/hostname": self.node_selector}
                    break
        else:
            self._node_selector = {"node-role.kubernetes.io/worker": ""}

    def set_interface(self, interface):
        # First drop the interface if it's already in the list
        interfaces = [
            i
            for i in self.desired_state["interfaces"]
            if not (i["name"] == interface["name"])
        ]

        # Add the interface
        interfaces.append(interface)
        self.desired_state["interfaces"] = interfaces

    def to_dict(self):
        res = super()._base_body()
        res.update({"spec": {"desiredState": self.desired_state}})
        if self._node_selector:
            res["spec"]["nodeSelector"] = self._node_selector

        """
        It's the responsibility of the caller to verify the desired configuration they send.
        For example: "ipv4.dhcp.enabled: false" without specifying any static IP address is a valid desired state and
        therefore not blocked in the code, but nmstate would reject it. Such configuration might be used for negative
        tests.
        """
        self.iface["ipv4"] = {"enabled": self.ipv4_enable, "dhcp": self.ipv4_dhcp}
        if self.ipv4_addresses:
            self.iface["ipv4"]["address"] = self.ipv4_addresses

        self.iface["ipv6"] = {"enabled": self.ipv6_enable}

        self.set_interface(interface=self.iface)
        if self.iface not in self.ifaces:
            self.ifaces.append(self.iface)

        return res

    def apply(self):
        resource = self.to_dict()
        samples = TimeoutSampler(
            timeout=3,
            sleep=1,
            exceptions=ConflictError,
            func=self.update,
            resource_dict=resource,
        )
        for _sample in samples:
            return

    def __enter__(self):
        if self._ipv4_dhcp:
            self._ipv4_state_backup()

        if self.mtu:
            for pod in self.worker_pods:
                for port in self.ports:
                    mtu = pod.execute(
                        command=["cat", f"/sys/class/net/{port}/mtu"]
                    ).strip()
                    LOGGER.info(
                        f"Backup MTU: {pod.node.name} interface {port} MTU is {mtu}"
                    )
                    self.mtu_dict[port] = mtu

        super().__enter__()

        try:
            self.wait_for_status_success()
            self.validate_create()
            return self
        except Exception as e:
            LOGGER.error(e)
            self.clean_up()
            raise

    def __exit__(self, exception_type, exception_value, traceback):
        if not self.teardown:
            return
        self.clean_up()

    @property
    def ipv4_dhcp(self):
        return self._ipv4_dhcp

    @ipv4_dhcp.setter
    def ipv4_dhcp(self, ipv4_dhcp):
        if ipv4_dhcp != self._ipv4_dhcp:
            self._ipv4_dhcp = ipv4_dhcp

            if self._ipv4_dhcp:
                self._ipv4_state_backup()
                self.iface["ipv4"] = {"dhcp": True, "enabled": True}

            self.set_interface(interface=self.iface)
            self.apply()

    def clean_up(self):
        if self.mtu:
            for port in self.ports:
                _port = {
                    "name": port,
                    "type": "ethernet",
                    "state": self.Interface.State.UP,
                    "mtu": int(self.mtu_dict[port]),
                }
                self.set_interface(interface=_port)

        for iface in self.ifaces:
            """
            If any physical interfaces are part of the policy - we will skip them,
            because we don't want to delete them (and we actually can't, and this attempt
            would end with failure).
            """
            if iface["name"] in self.node_active_nics:
                continue
            try:
                self._absent_interface()
                self.wait_for_interface_deleted()
            except TimeoutExpiredError as e:
                LOGGER.error(e)

        self.delete()

    def wait_for_interface_deleted(self):
        for pod in self.worker_pods:
            for iface in self.ifaces:
                node_network_state = NodeNetworkState(name=pod.node.name)
                node_network_state.wait_until_deleted(name=iface["name"])

    def validate_create(self):
        for pod in self.worker_pods:
            for bridge in self.ifaces:
                node_network_state = NodeNetworkState(name=pod.node.name)
                node_network_state.wait_until_up(name=bridge["name"])

    def _ipv4_state_backup(self):
        # Backup current state of dhcp for the interfaces which arent veth or current bridge
        for pod in self.worker_pods:
            node_network_state = NodeNetworkState(name=pod.node.name)
            self.ipv4_iface_state[pod.node.name] = {}
            for interface in node_network_state.instance.status.currentState.interfaces:
                if interface["name"] in self.ports:
                    self.ipv4_iface_state[pod.node.name].update(
                        {
                            interface["name"]: {
                                k: interface["ipv4"][k] for k in ("dhcp", "enabled")
                            }
                        }
                    )

    def _absent_interface(self):
        for bridge in self.ifaces:
            bridge["state"] = self.Interface.State.ABSENT
            self.set_interface(interface=bridge)

            if self._ipv4_dhcp:
                temp_ipv4_iface_state = {}
                for pod in self.worker_pods:
                    node_network_state = NodeNetworkState(name=pod.node.name)
                    temp_ipv4_iface_state[pod.node.name] = {}
                    # Find which interfaces got changed (of those that are connected to bridge)
                    for (
                        interface
                    ) in node_network_state.instance.status.currentState.interfaces:
                        if interface["name"] in self.ports:
                            x = {k: interface["ipv4"][k] for k in ("dhcp", "enabled")}
                            if (
                                self.ipv4_iface_state[pod.node.name][interface["name"]]
                                != x
                            ):
                                temp_ipv4_iface_state[pod.node.name].update(
                                    {
                                        interface["name"]: self.ipv4_iface_state[
                                            pod.node.name
                                        ][interface["name"]]
                                    }
                                )

                previous_state = next(iter(temp_ipv4_iface_state.values()))

                # Restore DHCP state of the changed bridge connected ports
                for iface_name, ipv4 in previous_state.items():
                    interface = {"name": iface_name, "ipv4": ipv4}
                    self.set_interface(interface=interface)

        self.apply()

    def status(self):
        for condition in self.instance.status.conditions:
            if condition["type"] == self.Conditions.Type.AVAILABLE:
                return condition["reason"]

    def wait_for_status_success(self):
        # if we get here too fast there are no conditions, we need to wait.
        self.wait_for_conditions()

        samples = TimeoutSampler(timeout=30, sleep=1, func=self.status)
        try:
            for sample in samples:
                if sample == self.Conditions.Reason.SUCCESS:
                    LOGGER.info("NNCP configured Successfully")
                    return sample

                if sample == self.Conditions.Reason.FAILED:
                    raise NNCPConfigurationFailed(
                        f"Reason: {self.Conditions.Reason.FAILED}"
                    )

        except (TimeoutExpiredError, NNCPConfigurationFailed):
            LOGGER.error("Unable to configure NNCP for node")
            raise

    def wait_for_conditions(self):
        samples = TimeoutSampler(
            timeout=30, sleep=1, func=lambda: self.instance.status.conditions
        )
        for sample in samples:
            if sample:
                return