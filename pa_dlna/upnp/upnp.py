"""A basic UPnP Control Point asyncio library.

The library does not have any external dependency.
Here is an example of using the Control Point. Allow it a few seconds to
discover an UPnP device on the 'enp0s31f6' ethernet interface.

>>> import asyncio
>>> import upnp
>>>
>>> async def main(nics):
...   with upnp.UPnPControlPoint(nics=nics) as control_point:
...     notification, root_device = await control_point.get_notification()
...     print(f"  Got '{notification}' from {root_device.peer_ipaddress}")
...     print(f'  deviceType: {root_device.deviceType}')
...     print(f'  friendlyName: {root_device.friendlyName}')
...     for service in root_device.serviceList.values():
...       print(f'    serviceId: {service.serviceId}')
...
>>> try:
...   asyncio.run(main(['enp0s31f6']))
... except KeyboardInterrupt:
...   pass
...
  Got 'alive' from 192.168.0.212
  deviceType: urn:schemas-upnp-org:device:MediaRenderer:1
  friendlyName: Yamaha RN402D
    serviceId: urn:upnp-org:serviceId:AVTransport
    serviceId: urn:upnp-org:serviceId:RenderingControl
    serviceId: urn:upnp-org:serviceId:ConnectionManager
>>>

The API is made of the methods and attibutes of the UPnPControlPoint,
UPnPRootDevice, UPnPDevice and UPnPService classes.
See "UPnP Device Architecture 2.0".

Not implemented:

- The extended data types in the service xml description - see
  "2.5.1 Defining and processing extended data types" in UPnP 2.0.

- SOAP <Header> elements are ignored - see "3.1.1 SOAP Profile" in UPnP 2.0.

- Unicast and multicast eventing are not implemented.
"""

import asyncio
import sys
import logging
import time
import re
import collections
import urllib.parse
from ipaddress import IPv4Interface, IPv4Address
from signal import SIGINT, SIGTERM, strsignal

from . import UPnPError, TEST_LOGLEVEL
from .util import NL_INDENT, shorten, log_unhandled_exception, AsyncioTasks
from .network import (ipaddr_from_nics, parse_ssdp, msearch, send_mcast,
                      Notify, http_get, http_soap)
from .xml import (upnp_org_etree, build_etree, xml_of_subelement,
                  findall_childless, scpd_actionlist, scpd_servicestatetable,
                  dict_to_xml, parse_soap_response, parse_soap_fault,
                  UPnPXMLError)

logger = logging.getLogger('upnp')

QUEUE_CLOSED = ('closed', None)
ICON_ELEMENTS = ('mimetype', 'width', 'height', 'depth', 'url')
SERVICEID_PREFIX = 'urn:upnp-org:serviceId:'
RE_MAX_AGE = re.compile(r'\s*max-age\s*=\s*(\d+)')

class UPnPClosedControlPointError(UPnPError): pass
class UPnPClosedDeviceError(UPnPError): pass
class UPnPInvalidSoapError(UPnPError): pass
class UPnPSoapFaultError(UPnPError): pass

# Components of an UPnP root device.
Icon = collections.namedtuple('Icon', ICON_ELEMENTS)
class UPnPElement:
    """An UPnP device or service."""

    def __init__(self, parent_device, root_device):
        self.parent_device = parent_device
        self.root_device = root_device

    @property
    def closed(self):
        return self.root_device._closed

class UPnPService(UPnPElement):
    """An UPnP service.

    Attributes:
      parent_device the UPnPDevice instance providing this service
      root_device   the UPnPRootDevice instance

      serviceType   UPnP service type
      serviceId     Service identifier
      description   the device xml descrition as a string

      actionList    dict {action name: arguments} where arguments is a dict
                    indexed by the argument name with a value that is another
                    dict whose keys are in.
                    ('direction', 'relatedStateVariable')

      serviceStateTable  dict {variable name: params} where params is a dict
                    with keys in ('sendEvents', 'multicast', 'dataType',
                    'defaultValue', 'allowedValueList',
                    'allowedValueRange').
                    The value of 'allowedValueList' is a list.
                    The value of 'allowedValueRange' is a dict with keys in
                    ('minimum', 'maximum', 'step').

    Properties:
      closed        True if the root device is closed

    Methods:
      soap_action   coroutine - send a SOAP action
    """

    def __init__(self, parent_device, root_device, attributes):
        super().__init__(parent_device, root_device)

        # Set the attributes found in the 'service' element of the device
        # description.
        for k, v in attributes.items():
            setattr(self, k, v)
        urlbase = root_device.urlbase
        self.SCPDURL = urllib.parse.urljoin(urlbase, self.SCPDURL)
        self.controlURL = urllib.parse.urljoin(urlbase, self.controlURL)
        if self.eventSubURL is not None:
            self.eventSubURL = urllib.parse.urljoin(urlbase, self.eventSubURL)

        self.actionList = {}
        self.serviceStateTable = {}
        self.description = None

    async def soap_action(self, action, args, log_debug=True):
        """Send a SOAP action.

        'action'    action name
        'args'      dict {argument name: value}
                    the dict keys MUST be in the same order as specified in
                    the service description (SCPD) that is available from the
                    device (UPnP 2.0), raises UPnPInvalidSoapError otherwise

        Return the dict {argumentName: out arg value} if successfull,
        otherwise raise an UPnPSoapFaultError exception with the instance of
        the upnp.xml.SoapFault namedtuple defined by field names in
        ('errorCode', 'errorDescription').
        """

        if self.closed:
            raise UPnPClosedDeviceError(
                f"Error while requesting '{action}' SOAP action:"
                f'{NL_INDENT}{self.root_device} is closed')

        # Validate action and args.
        if action not in self.actionList:
            raise UPnPInvalidSoapError(f"action '{action}' not in actionList"
                                       f" of '{self.serviceId}'")
        arguments = self.actionList[action]
        if list(args) != list(name for name in arguments if
                                  arguments[name]['direction'] == 'in'):
            raise UPnPInvalidSoapError(f'argument mismatch in action'
                                       f" '{action}' of '{self.serviceId}'")

        # Build the headers and the 'POST' request body.
        post_body = (
            f'<?xml version="1.0"?>\n'
            f'<s:Envelope'
            f' xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            f' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
            f'  <s:Body>\n'
            f'   <u:{action} xmlns:u="{self.serviceType}">\n'
            f'    {dict_to_xml(args)}\n'
            f'   </u:{action}>\n'
            f'  </s:Body>\n'
            f'</s:Envelope>'
        )
        post_body = ''.join(line.strip() for line in post_body.splitlines())
        post_body = post_body.encode()

        headers = [
            ('Content-length', str(len(post_body))),
            ('Content-type', 'text/xml; charset="utf-8"'),
            ('Soapaction',  f'"{self.serviceType}#{action}"'),
            ]

        # Send the soap action.
        is_fault, body =  await http_soap(self.controlURL, headers, post_body)

        # The specification of the serviceType format is
        # 'urn:schemas-upnp-org:device:serviceType:ver'.
        serviceType = self.serviceType.split(':')[-2]

        # Handle the response.
        body = body.decode()
        if is_fault:
            fault = parse_soap_fault(body)
            logger.warning(f"soap_action('{action}', '{serviceType}') ="
                           f' {fault}')
            raise UPnPSoapFaultError(fault)

        response = parse_soap_response(body, action)
        if log_debug:
            logger.debug(f'soap_action({action}, {serviceType}, {args}) ='
                         f' {response}')
        return response

    async def _run(self):
        unused, description = await http_get(self.SCPDURL)
        self.description = description.decode()

        # Parse the actionList.
        scpd, namespace = upnp_org_etree(self.description)
        self.actionList = scpd_actionlist(scpd, namespace)

        # Parse the serviceStateTable.
        self.serviceStateTable = scpd_servicestatetable(scpd, namespace)

        # Start the eventing task.
        # Not implemented.

        return self

    def __str__(self):
        return (self.serviceId[len(SERVICEID_PREFIX):] if
                self.serviceId.startswith(SERVICEID_PREFIX) else
                self.serviceId)

class UPnPDevice(UPnPElement):
    """An UPnP device.

    Attributes:
      parent_device the parent UPnPDevice instance or the root device
      root_device   the UPnPRootDevice instance

      description   the device xml description as a string
      urlbase       the url used to retrieve the description of the root
                    device or the 'URLBase' element (deprecated from UPnP 1.1
                    onwards)

      All the subelements of the 'device' element in the xml description are
      attributes of the UPnPDevice instance: 'deviceType', 'friendlyName',
      'manufacturer', 'UDN', etc... (see the specification). Note that a
      RootDevice has both an 'UDN' attribute and an 'udn' one that hold the
      same value.
      Their value is the value (text) of the element except for:

      serviceList   dict {serviceId value: UPnPService instance}
      deviceList    list of embedded devices
      iconList      list of instances of the Icon namedtuple; use 'urlbase'
                    and the (relative) 'url' attribute of the namedtuple to
                    retrieve the icon.

    Properties:
      closed        True if the root device is closed
    """

    def __init__(self, parent_device, root_device):
        super().__init__(parent_device, root_device)
        self.description = None
        self.urlbase = None
        if root_device is not None:
            self.urlbase = root_device.urlbase

        self.serviceList = {}
        self.deviceList = []
        self.iconList = []

    def _create_icons(self, icons, namespace):
        if icons is None:
            return

        for element in icons:
            if element.tag != f'{namespace!r}icon':
                raise UPnPXMLError(f"Found '{element.tag}' instead"
                                   f" of '{namespace!r}icon'")

            d = dict((k, val) for (k, val) in
                            findall_childless(element, namespace).items() if
                            k in ICON_ELEMENTS)
            if all(d.get(tag) for tag in ICON_ELEMENTS):
                self.iconList.append(Icon(**d))
            else:
                logger.warning("Missing required subelement of 'icon' in"
                               ' device description')

    async def _create_services(self, services, namespace):
        """Create each UPnPService instance with its attributes.

        And await until its xml description has been parsed and the soap task
        started. 'services' is an etree element.
        """

        if services is None:
            return

        service_ids = []
        for element in services:
            if element.tag != f'{namespace!r}service':
                raise UPnPXMLError(f"Found '{element.tag}' instead"
                                   f" of '{namespace!r}service'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLError("Empty 'service' element")
            if 'serviceId' not in d:
                raise UPnPXMLError("Missing 'serviceId' element")

            serviceId = d['serviceId']
            self.serviceList[serviceId] = await (
                                UPnPService(self, self.root_device, d)._run())

            # The specification of the serviceId format is
            # 'urn:upnp-org:serviceId:service'.
            service = serviceId.split(':')[-1]
            service_ids.append(service)

        return service_ids

    async def _create_devices(self, devices, namespace):
        """Instantiate the embedded UPnPDevice(s)."""

        if devices is None:
            return

        for element in devices:
            if element.tag != f'{namespace!r}device':
                raise UPnPXMLError(f"Found '{element.tag}' instead"
                                   f" of '{namespace!r}device'")

            d = findall_childless(element, namespace)
            if not d:
                raise UPnPXMLError("Empty 'device' element")

            description = build_etree(element)
            embedded = await (UPnPDevice(self,
                            self.root_device)._parse_description(description))
            self.deviceList.append(embedded)

    @staticmethod
    def embedded_devices_generator(device):
        """Recursive generator yielding the embedded devices."""

        yield device
        for dev in device.deviceList:
            yield from UPnPDevice.embedded_devices_generator(dev)

    async def _parse_description(self, description):
        """Parse the xml 'description'.

        Recursively instantiate the tree of embedded devices and their
        services. When this method returns, each UPnPService instance has
        parsed its description and started a task to handle soap requests.
        """

        self.description = description
        device_etree, namespace = upnp_org_etree(description)

        # Add the childless elements of the device element as instance
        # attributes of the UPnPDevice instance.
        for k, v in findall_childless(device_etree, namespace).items():
            if k not in ('serviceList', 'deviceList', 'iconList'):
                setattr(self, k, v)

        if not hasattr(self, 'deviceType'):
            raise UPnPXMLError("Missing 'deviceType' element")
        # The specification of the deviceType format is
        # 'urn:schemas-upnp-org:device:deviceType:ver'.
        self.device_type = self.deviceType.split(':')[-2]
        if not isinstance(self, UPnPRootDevice):
            logger.info(f'New {self.device_type} embedded device')

        icons = device_etree.find(f'{namespace!r}iconList')
        self._create_icons(icons, namespace)

        services = device_etree.find(f'{namespace!r}serviceList')
        services  = await self._create_services(services, namespace)
        if services:
            logger.info(f"New UPnP services: {', '.join(services)}")

        # Recursion here: _create_devices() calls _parse_description()
        devices = device_etree.find(f'{namespace!r}deviceList')
        await self._create_devices(devices, namespace)

        return self

    def __str__(self):
        return f'{shorten(self.UDN)}'

class UPnPRootDevice(UPnPDevice):
    """An UPnP root device.

    An UPnP root device is also an UPnPDevice, see the UPnPDevice __doc__ for
    the other attributes and methods available.

    Attributes:
      udn               Unique Device Name
      peer_ipaddress    IP address of the UPnP device
      local_ipaddress   IP address of the local network interface receiving
                        msearch response datagrams
      location          'Location' field value in the header of the notify or
                        msearch SSDP

    Methods:
      close             Close the root device
    """

    def __init__(self, control_point, udn, peer_ipaddress, local_ipaddress,
                 location, max_age):
        super().__init__(self, self)
        self._control_point = control_point     # UPnPControlPoint instance
        self.udn = udn
        self.peer_ipaddress = peer_ipaddress
        self.local_ipaddress = local_ipaddress
        self.location = location
        self._curtask = None                    # UPnPRootDevice._run() task
        self._set_valid_until(max_age)
        self._max_age = max_age
        self._closed = False

    def close(self, exc=None):
        if not self._closed:
            self._closed = True
            logger.info(f'Close {self}')
            if (self._curtask is not None and
                    asyncio.current_task() != self._curtask):
                self._curtask.cancel()
            self._control_point._remove_root_device(self, exc=exc)

    def _set_valid_until(self, max_age):
        # The '_valid_until' attribute is the monotonic date when the root
        # device and its services and embedded devices become disabled.
        # '_valid_until' None means no aging is performed.
        if max_age:
            self._valid_until = time.monotonic() + max_age
        else:
            self._valid_until = None

    def _get_timeleft(self):
        if self._valid_until is not None:
            return self._valid_until - time.monotonic()
        return None

    async def _age_root_device(self):
        # Age the root device using SSDP alive notifications.
        while True:
            timeleft = self._get_timeleft()
            # Missing or invalid 'CACHE-CONTROL' field in SSDP.
            # Wait for a change in _valid_until.
            if not timeleft:
                await asyncio.sleep(60)
            elif timeleft > 0:
                await asyncio.sleep(timeleft)
            else:
                logger.warning(f'Aging expired on {self}')
                self.close()
                break

    @log_unhandled_exception(logger)
    async def _run(self):
        self._curtask = asyncio.current_task()
        try:
            unused, description = await http_get(self.location)
            description = description.decode()
            if not description:
                raise UPnPError(f"The 'description' of {self} is empty")

            # Find the 'URLBase' subelement (UPnP version 1.1).
            root, namespace = upnp_org_etree(description)
            element = root.find(f'{namespace!r}URLBase')
            self.urlbase = (element.text if element is not None else
                            self.location)

            device_description = xml_of_subelement(description, 'device')
            if device_description is None:
                raise UPnPXMLError("Missing 'device' subelement in root"
                                   ' device description')
            await self._parse_description(device_description)

            logger.info(
                f'New {self.device_type} root device at {self.peer_ipaddress}'
                f' with UDN:' + NL_INDENT + f'{self.udn}')
            self._control_point._put_notification('alive', self)
            logger.debug(f'{self} has been created'
                         f' with max-age={self._max_age}')

            await self._age_root_device()
        except asyncio.CancelledError:
            self.close()
        except OSError as e:
            logger.error(f'UPnPRootDevice._run(): {e!r}')
            self.close(exc=e)
        except Exception as e:
            self.close(exc=e)
            raise

    def __str__(self):
        """Return a short representation of udn."""

        return f'UPnPRootDevice {shorten(self.udn)}'

# UPnP control point.
class UPnPControlPoint:
    """An UPnP control point.

    Attributes:
      ip_configured List of the IPv4 addresses of the networks where UPnP
                    devices may be discovered.
      nics          List of the network interfaces where UPnP devices may be
                    discovered.
      ip_monitored  List of the IPv4 addresses currently monitored by UPnP
                    discovery.
      msearch_interval
                    The time interval in seconds between the sending of the
                    MSEARCH datagrams used for device discovery.
      msearch_port  The local UDP port for receiving MSEARCH response messages
                    from UPnP devices, a value of '0' means letting the
                    operating system choose an ephemeral port.
      ttl           The IP packets time to live.

    Methods:
      open          Coroutine - start the UPnP Control Point.
      close         Close the UPnP Control Point.
      disable_root_device
                    Disable permanently a root device.
      is_disabled   Return True if the root device is disabled.
      get_notification
                    Coroutine - return a notification and the corresponding
                    UPnPRootDevice instance.
      __enter__     UPnPControlPoint supports the context manager protocol.
      __close__
    """

    def __init__(self, ip_addresses=[], nics=[], msearch_interval=60,
                 msearch_port=0, ttl=2):
        self.ip_configured = ip_addresses
        self.nics = nics
        self.msearch_interval = msearch_interval
        self.msearch_port = msearch_port
        self.ttl = ttl

        # Get the list of active IPv4 addresses used for UPnP discovery.
        self.ip_monitored = set()
        self._update_ip_addresses()

        self._closed = False
        self._notify = None
        self._upnp_queue = asyncio.Queue()
        self._devices = {}              # {udn: UPnPRootDevice}
        self._faulty_devices = set()    # set of the udn of root devices
                                        # permanently disabled
        self._last_msearch = time.monotonic()
        self._msearch_task = None
        self._notify_task = None
        self._upnp_tasks = AsyncioTasks()

    def open(self):
        """Start the UPnP Control Point."""

        # Start the msearch task.
        self._msearch_task = self._upnp_tasks.create_task(
                                    self._ssdp_msearch(), name='ssdp msearch')

        # Start the notify task.
        self._notify = Notify(self._process_ssdp, self.ip_monitored)
        self._notify_task = self._upnp_tasks.create_task(self._ssdp_notify(),
                                                        name='ssdp notify')

    def close(self):
        """Close the UPnP Control Point."""

        if not self._closed:
            self._closed = True

            self._put_notification(*QUEUE_CLOSED)
            if self._msearch_task is not None:
                self._msearch_task.cancel()
            if self._notify_task is not None:
                self._notify_task.cancel()
                self._notify.close()

            for root_device in list(self._devices.values()):
                root_device.close()

            logger.info('Close UPnPControlPoint')

    def disable_root_device(self, root_device, name=None):
        """Disable permanently a root device."""

        udn = root_device.udn
        if udn in self._faulty_devices:
            return

        self._faulty_devices.add(udn)
        root_device.close()
        devname = f'{root_device} UPnP' if name is None else f'{name} DLNA'
        logger.warning(f'Disable the {devname} device permanently')

    def is_disabled(self, root_device):
        return root_device.udn in self._faulty_devices

    async def get_notification(self):
        """Return the tuple ('alive' or 'byebye', UPnPRootDevice instance).

        A coroutine waiting on the queue gets the QUEUE_CLOSED tuple when the
        control point is closing.
        When the control point is closed, the method raises
        UPnPClosedControlPointError.
        """

        if self._closed:
            raise UPnPClosedControlPointError
        else:
            return await self._upnp_queue.get()

    def _put_notification(self, kind, root_device):
        self._upnp_queue.put_nowait((kind, root_device))

    def _skip_log_max_age(self, max_age, root_device, is_msearch):
        # Avoid cluttering the logs when the aging refresh occurs within 5
        # seconds of the last one.
        if not max_age:
            if is_msearch:
                previous = self._last_msearch
                self._last_msearch = time.monotonic()
            if not is_msearch or self._last_msearch - previous > 5:
                return False
        else:
            timeleft = root_device._get_timeleft()
            if timeleft is not None and max_age - timeleft > 5:
                return False
        return True

    def _create_root_device(self, header, udn, peer_ipaddress,
                            is_msearch, local_ipaddress):
        # Get the max-age.
        # No aging if 'max_age' in (None, 0).
        max_age = None
        cache = header.get('CACHE-CONTROL')
        if cache is not None:
            match = RE_MAX_AGE.match(cache)
            if match is not None:
                max_age = int(match.group(1))
            else:
                logger.warning(
                    f'Invalid CACHE-CONTROL field in'
                    f' SSDP notify from {peer_ipaddress}:\n{header}')
                return

        if udn not in self._devices:
            # Instantiate the UPnPDevice and start its task.
            if local_ipaddress is None:
                ip_addr = IPv4Address(peer_ipaddress)
                for obj in ipaddr_from_nics(self.nics, as_string=False):
                    if (isinstance(obj, IPv4Interface) and
                            ip_addr in obj.network):
                        local_ipaddress = str(obj.ip)
                        break
                else:
                    logger.info(f'{peer_ipaddress} does not belong to one'
                                f' of the known network interfaces')

            root_device = UPnPRootDevice(self, udn, peer_ipaddress,
                                local_ipaddress, header['LOCATION'], max_age)
            self._upnp_tasks.create_task(root_device._run(),
                                         name=str(root_device))
            self._devices[udn] = root_device

        else:
            root_device = self._devices[udn]
            # The root device had been created from reception of a notify
            # SSDP, this handles the reception of an msearch SSDP so update
            # 'local_ipaddress' if not already done.
            if (not root_device.closed and
                    local_ipaddress is not None and
                    root_device.local_ipaddress is None):
                root_device.local_ipaddress = local_ipaddress
                self._put_notification('alive', root_device)

            if not self._skip_log_max_age(max_age, root_device, is_msearch):
                msg = ('msearch response' if is_msearch else
                       'notify advertisement')
                logger.debug(f'Got {msg} from {peer_ipaddress}')
                logger.debug(f'Refresh with max-age={max_age}'
                             f' for {root_device}')

            # Refresh the aging time.
            root_device._set_valid_until(max_age)

    def _remove_root_device(self, root_device, exc=None):
        udn = root_device.udn
        if udn in self._devices:
            del self._devices[udn]
            root_device.close()
            logger.debug(f'{root_device} has been deleted')
            self._put_notification('byebye', root_device)

        if exc is not None:
            self.disable_root_device(root_device)

    def _update_ip_addresses(self):
        """Set the new IPv4 addresses set."""

        current_ips = set()
        if self.ip_configured:
            all_ips = list(ipaddr_from_nics(nics=[]))
            for ip in self.ip_configured:
                if ip in all_ips:
                    current_ips.add(ip)

        if self.nics or not self.ip_configured:
            for ip in ipaddr_from_nics(nics=self.nics, skip_loopback=True):
                current_ips.add(ip)

        new_ips = current_ips.difference(self.ip_monitored)
        if new_ips:
            logger.info(f'Start UPnP discovery on new IPs {new_ips}')
        stale_ips =  self.ip_monitored.difference(current_ips)
        if stale_ips:
            logger.info(f'Stop UPnP discovery on stale IPs {stale_ips}')
        self.ip_monitored = current_ips

        return new_ips, stale_ips

    def _process_ssdp(self, datagram, peer_ipaddress, local_ipaddress):
        """Process the received datagrams."""

        if (local_ipaddress is not None and local_ipaddress not in
                                                self.ip_monitored):
            logger.warning(
                f'Ignore msearch SSDP received on {local_ipaddress}')
            return

        logger.log(TEST_LOGLEVEL, datagram.decode())

        # 'is_msearch' is True when processing a msearch response,
        # otherwise it is a notify advertisement.
        is_msearch = True if local_ipaddress is not None else False

        header = parse_ssdp(datagram, peer_ipaddress, is_msearch)
        if header is None:
            return

        if is_msearch or (header['NTS'] == 'ssdp:alive'):
            udn = header['USN'].split('::')[0]
            if udn not in self._faulty_devices:
                self._create_root_device(header, udn, peer_ipaddress,
                                         is_msearch, local_ipaddress)
        else:
            nts = header['NTS']
            if nts == 'ssdp:byebye':
                udn = header['USN'].split('::')[0]
                root_device = self._devices.get(udn)
                if root_device is not None:
                    self._remove_root_device(root_device)

            elif nts == 'ssdp:update':
                logger.warning(f'Ignore not supported {nts} notification'
                               f' from {peer_ipaddress}')

            else:
                logger.warning(f"Unknown NTS field '{nts}' in SSDP notify"
                               ' from {peer_ipaddress}')

    async def msearch_once(self, coro, port, do_msearch=True):
        new_ips, stale_ips = self._update_ip_addresses()

        for ip in stale_ips:
            for root_device in self._devices.values():
                if ip == root_device.local_ipaddress:
                    root_device.close()
                    break

        # Update the notify task with the new and stale sets.
        if self._notify is not None:
            self._notify.manage_membership(new_ips, stale_ips)

        if not do_msearch and not new_ips:
            return

        for ip_addr in self.ip_monitored:
            # 'coro' is a coroutine *function*.
            result = await send_mcast(ip_addr, port, ttl=self.ttl,
                                      coro=coro)
            if result:
                for (data, peer_addr, local_addr) in result:
                    self._process_ssdp(data, peer_addr, local_addr)
            else:
                logger.debug(f'No response on {ip_addr} to all'
                             f' M-SEARCH messages, next try in'
                             f' {self.msearch_interval} seconds')

    @log_unhandled_exception(logger)
    async def _ssdp_msearch(self, coro=msearch):
        """Send msearch multicast SSDPs and process unicast responses."""

        try:
            last_time = -1
            while True:
                do_msearch = False
                cur_time = time.monotonic()
                if (last_time == -1 or
                        cur_time - last_time >= self.msearch_interval):
                    do_msearch = True
                    last_time = cur_time
                await self.msearch_once(coro, self.msearch_port,
                                        do_msearch=do_msearch)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self.close()

    @log_unhandled_exception(logger)
    async def _ssdp_notify(self):
        """Listen to SSDP notifications."""

        try:
            await self._notify.run()
        except asyncio.CancelledError:
            pass
        finally:
            self.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
