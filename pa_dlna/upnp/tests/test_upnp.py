"""UPnP test cases."""

import re
import asyncio
import psutil
import logging
import urllib
from unittest import mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import (loopback_datagrams, find_in_logs, search_in_logs, UDN, HOST,
               HTTP_PORT, SSDP_NOTIFY, SSDP_PARAMS, SSDP_ALIVE, URL,
               bind_mcast_address, HTTPRequestHandler,
               LoggingIsolatedAsyncioTestCase)
from .test_network import snicaddr, snicstats
from .device_resps import device_description, scpd, soap_response, soap_fault
from ..util import shorten, log_unhandled_exception
from ..upnp import (UPnPControlPoint, UPnPRootDevice, UPnPDevice, UPnPService,
                    UPnPSoapFaultError, UPnPClosedDeviceError)
from ..xml import UPnPXMLError

SSDP_BYEBYE = SSDP_NOTIFY.format(nts='NTS: ssdp:byebye', **SSDP_PARAMS)
SSDP_UPDATE = SSDP_NOTIFY.format(nts='NTS: ssdp:update', **SSDP_PARAMS)
CONNECTIONMANAGER = 'urn:upnp-org:serviceId:ConnectionManager'

logger = logging.getLogger('test_upnp')

class HTTPServer:
    def __init__(self, soap_response, icons, devices):
        self.soap_response = soap_response
        self.icons = icons
        self.devices = devices
        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    def get_response(self, uri_path):
        header = ['HTTP/1.1 200 OK']
        if uri_path == '/MediaRenderer/desc.xml':
            body = device_description(icons=self.icons, devices=self.devices)
        else:
            for service in ('AVTransport', 'RenderingControl',
                            'ConnectionManager'):
                if uri_path == f'/{service}/desc.xml':
                    body = scpd()
                    break
                elif uri_path == f'/{service}/ctrl':
                    body = self.soap_response
                    if 'Fault>' in body:
                        header = ['HTTP/1.1 500 Internal Server Error']
                    break
            else:
                raise AssertionError(f'Unknown uri_path: {uri_path}')

        self.body = body.encode()
        header.extend([('Content-Length: ' + str(len(self.body))), '', ''])
        self.header = '\r\n'.join(header).encode('ascii')

    @log_unhandled_exception(logger)
    async def client_connected(self, reader, writer):
        """Handle an HTTP GET request and return the response."""

        peername = writer.get_extra_info('peername')
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()
            if not hasattr(handler, 'path'):
                return
            uri_path = urllib.parse.unquote(handler.path)
            self.get_response(uri_path)

            # Write the response.
            writer.write(self.header)
            writer.write(self.body)
        finally:
            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def run(self):
        aio_server = await asyncio.start_server(self.client_connected,
                                                HOST, HTTP_PORT)
        async with aio_server:
            self.startup.set_result(None)
            await aio_server.serve_forever()

async def start_http_server(soap_response=None, icons='', devices=''):
    http_server = HTTPServer(soap_response, icons, devices)
    task = asyncio.create_task(http_server.run())
    await http_server.startup
    return task

class ControlPoint(LoggingIsolatedAsyncioTestCase):
    """Control Point test cases."""

    @staticmethod
    @bind_mcast_address()
    async def _run_until_patch(datagrams, setup=None,
                               patch_method='_put_notification'):
        await start_http_server()
        return await loopback_datagrams(datagrams, setup=setup,
                                        patch_method=patch_method)

    async def test_alive(self):
        await self._run_until_patch([SSDP_ALIVE])

        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                        'New UPnP services: AVTransport, RenderingControl,'
                        ' ConnectionManager'))
        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
                re.compile('UPnPRootDevice uuid:fffff.* has been created')))

    async def test_update(self):
        await self._run_until_patch([SSDP_UPDATE, SSDP_ALIVE])
        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                f'Ignore not supported ssdp:update notification from {HOST}'))

    async def test_bad_nts(self):
        nts_field = 'ssdp:FOO'
        nts = f'NTS: {nts_field}'
        ssdp_bad_nts = SSDP_NOTIFY.format(nts=nts, **SSDP_PARAMS)
        await self._run_until_patch([ssdp_bad_nts, SSDP_ALIVE])

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
                                re.compile(f"Unknown NTS field '{nts_field}'")))

    async def test_byebye(self):
        async def setup(control_point):
            root_device = mock.MagicMock()
            root_device.udn = UDN
            root_device.__str__.side_effect = [device_name]
            control_point._devices[UDN] = root_device

        device_name = '_Some root device name_'
        await self._run_until_patch([SSDP_BYEBYE], setup=setup)

        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                                     f'{device_name} has been deleted'))

    async def test_faulty_device(self):
        async def setup(control_point):
            control_point._faulty_devices.add(udn)

        udn = 'uuid:ffffffff-ffff-ffff-ffff-000000000000'
        ssdp_params = { 'url': URL,
                        'max_age': '1800',
                        'nts': 'NTS: ssdp:alive',
                        'udn': udn
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        control_point = await self._run_until_patch(
                        [ssdp_alive, SSDP_ALIVE],
                        setup=setup, patch_method='_create_root_device')

        control_point._create_root_device.assert_called_once()

    async def test_remove_device(self):
        class RootDevice:
            def __init__(self, udn): self.udn = udn
            def close(self): pass
            def __str__(self): return shorten(udn)

        async def setup(control_point):
            control_point._devices[udn] = root_device
            control_point._remove_root_device(root_device, exc=OSError())

        udn = 'uuid:ffffffff-ffff-ffff-ffff-000000000000'
        root_device = RootDevice(udn)
        ssdp_params = { 'url': URL,
                        'max_age': '1800',
                        'nts': 'NTS: ssdp:alive',
                        'udn': udn
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        control_point = await self._run_until_patch(
                        [ssdp_alive, SSDP_ALIVE], setup=setup,
                        patch_method='_create_root_device')

        self.assertTrue(control_point.is_disabled(root_device))
        self.assertTrue(find_in_logs(self.logging_output,
            'upnp', f'Disable the {shorten(udn)} UPnP device permanently'))

    async def test_bad_max_age(self):
        max_age = 'FOO'
        ssdp_params = { 'url': URL,
                        'max_age': f'{max_age}',
                        'nts': 'NTS: ssdp:alive',
                        'udn': UDN
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        await self._run_until_patch([ssdp_alive, SSDP_ALIVE])

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
            re.compile(f'Invalid CACHE-CONTROL field.*\n.*max-age={max_age}')))

    async def test_issue_50(self):
        ssdp_params = { 'url': URL,
                        'max_age': '1800',
                        'nts': 'NTS: ssdp:alive',
                        'udn': UDN
                       }
        ssdp_alive = SSDP_NOTIFY.format(**ssdp_params)
        ssdp_alive = ssdp_alive.replace('max-age=', 'max-age = ')
        await self._run_until_patch([ssdp_alive, SSDP_ALIVE,])

        self.assertFalse(search_in_logs(self.logging_output, 'upnp',
            re.compile(f'Invalid CACHE-CONTROL field')))

    async def test_refresh(self):
        ssdp_params = { 'url': URL,
                        'nts': 'NTS: ssdp:alive',
                        'udn': UDN
                       }
        ssdp_alive_first = SSDP_NOTIFY.format(max_age=10, **ssdp_params)
        ssdp_alive_second = SSDP_NOTIFY.format(max_age=20, **ssdp_params)
        await self._run_until_patch([ssdp_alive_first, ssdp_alive_second])

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
                            re.compile('Refresh with max-age=20')))

    async def test_close(self):
        async def is_called(mock):
            while True:
                await asyncio.sleep(0)
                if mock.called:
                    return True

        control_point = UPnPControlPoint(nics=['lo'], msearch_interval=3600)
        with mock.patch.object(UPnPControlPoint, 'msearch_once') as msearch:
            msearch.side_effect = OSError('FOO')
            control_point.open()
            await asyncio.wait_for(is_called(msearch), 1)

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
                                       re.compile(r"OSError\('FOO'\)")))
        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                                     'Close UPnPControlPoint'))

    @bind_mcast_address()
    async def test_local_ip_address(self):
        # Test that 'root_device' created by an SSDP notification gets a
        # 'local_ipaddress' later through an SSDP msearch.
        header = { 'LOCATION': URL }
        control_point = UPnPControlPoint(nics=['lo'], msearch_interval=3600)
        with mock.patch.object(control_point, '_put_notification'):
            await start_http_server()
            # The SSDP notification.
            # Using '192.168.0.1' instead of HOST to prevent the root device
            # local_ipaddress to be found by matching the network of 'lo'.
            control_point._create_root_device(header, UDN, '192.168.0.1',
                                              False, None)
            root_device = control_point._devices[UDN]
            self.assertEqual(root_device.local_ipaddress, None)

            # Wait for the HTTP request to complete, otherwise Python 3.11
            # complains with 'Error on transport creation for incoming
            # connection'.
            while root_device.urlbase is None:
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            # The SSDP msearch provides the local_ipaddress.
            control_point._create_root_device(header, UDN, HOST, True, HOST)
            self.assertEqual(root_device.local_ipaddress, HOST)

    def test_update_ip_addresses(self):
        init_nics = {
            'eth0': [snicaddr('192.168.0.1', '255.255.255.0')],
            'eth1': [snicaddr('192.168.1.1', '255.255.255.0')],
        }
        next_nics = {
            'eth0': [snicaddr('192.168.0.1', '255.255.255.0')],
            'wlan2': [snicaddr('192.168.2.1', '255.255.255.0')],
        }
        nics_stat = {
            'eth0': snicstats(True),
            'eth1': snicstats(True),
            'wlan2': snicstats(True),
        }
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_addrs.side_effect = [init_nics, init_nics,
                                        next_nics, next_nics]
            net_if_stats.side_effect = [nics_stat, nics_stat,
                                        nics_stat, nics_stat]
            control_point = UPnPControlPoint(ip_addresses=['192.168.1.1'],
                                             nics=['eth0', 'wlan2'])
            new_ips, stale_ips = control_point._update_ip_addresses()
            self.assertEqual(new_ips, {'192.168.2.1'})
            self.assertEqual(stale_ips, {'192.168.1.1'})

    @bind_mcast_address()
    async def test_stale_ip_address(self):
        ip = '192.168.0.1'
        nics = {'eth0': [snicaddr(ip, '255.255.255.0')]}
        nics_stat = {'eth0': snicstats(True)}
        next_nics_stat = {'eth0': snicstats(False)}
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_addrs.side_effect = [nics, nics]
            net_if_stats.side_effect = [nics_stat, next_nics_stat]
            control_point = UPnPControlPoint(nics=['eth0'])

            header = { 'LOCATION': URL }
            with mock.patch.object(control_point, '_put_notification'):
                await start_http_server()
                control_point._create_root_device(header, UDN, ip, True, ip)

                # Wait for the HTTP request to complete (see
                # test_local_ip_address).
                root_device = control_point._devices[UDN]
                while root_device.urlbase is None:
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)

                self.assertTrue(UDN in control_point._devices)
                await control_point.msearch_once(None, 0, do_msearch=False)
                self.assertTrue(UDN not in control_point._devices)

@bind_mcast_address()
class RootDevice(LoggingIsolatedAsyncioTestCase):
    """Root device test cases."""

    def setUp(self):
        super().setUp()
        self.control_point = UPnPControlPoint(nics=['lo'],
                                              msearch_interval=3600)
        self.root_device = UPnPRootDevice(self.control_point, UDN, HOST, HOST,
                                          URL, 1800)

    async def test_OSError(self):
        exc = OSError('FOO')
        with mock.patch.object(self.root_device,
                                  '_parse_description') as parse:
            parse.side_effect = exc
            await start_http_server()
            await self.root_device._run()

        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                                     f'UPnPRootDevice._run(): {exc!r}'))
        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
            re.compile('Disable the UPnPRootDevice .* device permanently')))

    async def test_missing_device(self):
        with mock.patch('pa_dlna.upnp.upnp.xml_of_subelement') as subelement:
            subelement.side_effect = [None]
            await start_http_server()
            await self.root_device._run()

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
                        re.compile("Missing 'device' subelement in root"
                                   ' device description')))

    async def test_age_device(self):
        # A max_age value of 0 means no aging. Set this value of 0.1 to
        # pass the test.
        max_age = 0.1
        self.root_device = UPnPRootDevice(self.control_point, UDN, HOST,
                                          HOST, URL, max_age)
        await start_http_server()
        await self.root_device._run()

        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                        f'Aging expired on UPnPRootDevice {shorten(UDN)}'))
        self.assertTrue(find_in_logs(self.logging_output, 'upnp',
                        f'UPnPRootDevice {shorten(UDN)} has been created'
                        f' with max-age={max_age}'))

    async def test_soap_action(self):
        response = soap_response(
            f"""
            <u:GetProtocolInfoResponse
                    xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">
                <Source></Source>
                <Sink></Sink>
            </u:GetProtocolInfoResponse>
        """)

        with mock.patch.object(self.root_device, '_age_root_device') as age:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(response)
            await self.root_device._run()

            service = self.root_device.serviceList[CONNECTIONMANAGER]
            self.assertTrue(isinstance(service, UPnPService))
            response = await service.soap_action('GetProtocolInfo', {},
                                                 log_debug=True)

        self.assertEqual(response, {'Source': None, 'Sink': None})

    async def test_soap_closed(self):
        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertRaises(UPnPClosedDeviceError) as cm:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server()
            await self.root_device._run()

            self.root_device.close()
            service = self.root_device.serviceList[CONNECTIONMANAGER]
            response = await service.soap_action('GetProtocolInfo', {})

    async def test_soap_fault(self):
        response = soap_fault()
        with mock.patch.object(self.root_device, '_age_root_device') as age,\
                self.assertRaises(UPnPSoapFaultError) as cm:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(response)
            await self.root_device._run()

            service = self.root_device.serviceList[CONNECTIONMANAGER]
            response = await service.soap_action('GetProtocolInfo', {},
                                                 log_debug=True)

        self.assertEqual(cm.exception.args[0]._asdict(),
                {'errorCode': '401', 'errorDescription': 'Invalid Action'})

    async def test_icons(self):
        # Testing a valid iconList and an invalid one.
        for element in ('', '<id>0</id>'):
            with self.subTest(element=element):
                # Reset self.root_device on each iteration.
                self.setUp()

                icons = f"""<iconList>
                             <icon>
                               {element}
                               <mimetype>image/jpeg</mimetype>
                               <width>48</width>
                               <height>48</height>
                               <depth>24</depth>
                               <url>/Icons/48x48.jpg</url>
                             </icon>
                           </iconList>"""

                with mock.patch.object(
                        self.root_device, '_age_root_device') as age:
                    # Make the UPnPRootDevice._run() coroutine terminate.
                    age.side_effect = [None]
                    task = await start_http_server(icons=icons)
                    await self.root_device._run()
                    task.cancel()

                self.assertEqual(self.root_device.iconList[0]._asdict(),
                                 {'mimetype': 'image/jpeg',
                                  'width': '48',
                                  'height': '48',
                                  'depth': '24'
                                  , 'url': '/Icons/48x48.jpg'})

    async def test_icons_namespace(self):
        icons = """<iconList>
                     <yamaha:icon>
                       <mimetype>image/jpeg</mimetype>
                       <width>48</width>
                       <height>48</height>
                       <depth>24</depth>
                       <url>/Icons/48x48.jpg</url>
                     </yamaha:icon>
                   </iconList>"""

        with mock.patch.object(self.root_device, '_age_root_device') as age:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(icons=icons)
            await self.root_device._run()

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
            re.compile("UPnPXMLError.*Found "
                "'{urn:schemas-yamaha-com:device-1-0}icon' instead of"
                " '{urn:schemas-upnp-org:device-1-0}icon'")))

    async def test_icons_missing(self):
        icons = """<iconList>
                     <icon>
                       <width>48</width>
                       <height>48</height>
                       <depth>24</depth>
                       <url>/Icons/48x48.jpg</url>
                     </icon>
                   </iconList>"""

        with mock.patch.object(self.root_device, '_age_root_device') as age:
            # Make the UPnPRootDevice._run() coroutine terminate.
            age.side_effect = [None]
            await start_http_server(icons=icons)
            await self.root_device._run()

        self.assertTrue(search_in_logs(self.logging_output, 'upnp',
                    re.compile("Missing required subelement of 'icon' in"
                               " device description")))

    async def test_devices(self):
        device_type = 'urn:schemas-upnp-org:device:MediaRenderer:1'
        device_name = 'Embedded device name'
        # Root device udn set by device_description() in the device_resps
        # module.
        root_device_udn = 'uuid:ffffffff-ffff-ffff-ffff-ffffffffffff'
        udn = 'uuid:embedded-ffff-ffff-ffff-ffffffffffff'

        # Testing a valid deviceList and xml namespaces with same prefix
        # within the same scope.
        for nested in ('',
                       '<X_Rhapsody-Extension xmlns="http://www.real.com"/>'):
            with self.subTest(nested=nested):
                # Reset self.root_device on each iteration.
                self.setUp()

                devices = f"""<deviceList>
                                <device>
                                  <deviceType>{device_type}</deviceType>
                                  <UDN>{udn}</UDN>
                                  <friendlyName>{device_name}</friendlyName>
                                  {nested}
                                </device>
                              </deviceList>"""

                with mock.patch.object(self.root_device, '_age_root_device') as age:
                    # Make the UPnPRootDevice._run() coroutine terminate.
                    age.side_effect = [None]
                    task = await start_http_server(devices=devices)
                    await self.root_device._run()
                    task.cancel()

                embedded = self.root_device.deviceList[0]
                self.assertEqual(embedded.friendlyName, device_name)

                self.assertEqual(embedded.UDN, udn)
                self.assertTrue(not hasattr(embedded, 'udn'))
                self.assertEqual(self.root_device.UDN, root_device_udn)

                all_devices = list(
                            UPnPDevice.embedded_devices_generator(self.root_device))
                self.assertEqual(all_devices, [self.root_device, embedded])

    async def test_empty_device_list(self):
        empty_list = '<deviceList></deviceList>'
        newline_list = '<deviceList>\n</deviceList>'
        for devices in (empty_list, newline_list):
            with self.subTest(devices=devices):
                with mock.patch.object(
                            self.root_device, '_age_root_device') as age:
                    # Make the UPnPRootDevice._run() coroutine terminate.
                    age.side_effect = [None]
                    task = await start_http_server(devices=devices)
                    await self.root_device._run()
                    task.cancel()

        self.assertTrue(isinstance(self.root_device.deviceList, list))
        self.assertTrue(len(self.root_device.deviceList) == 0)

    def tearDown(self):
        self.control_point.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
