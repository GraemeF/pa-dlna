"""XML test cases."""

import re
import logging

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import search_in_logs, LoggingTestCase
from .device_resps import (device_description, scpd, soap_response,
                           soap_fault)
from ..xml import (xml_of_subelement, dict_to_xml, pformat_xml,
                   upnp_org_etree, scpd_actionlist, scpd_servicestatetable,
                   parse_soap_response, UPnPXMLError, parse_soap_fault,
                   UPnPNamespace)

class XML(LoggingTestCase):
    """XML test cases."""

    def test_subelement(self):
        friendly_name = 'Some friendly name'
        description = device_description(friendly_name=friendly_name)
        device = xml_of_subelement(description, 'device')
        self.assertIn(friendly_name,
                      xml_of_subelement(device, 'friendlyName'))

    def test_missing_subelement(self):
        description = device_description()
        device = xml_of_subelement(description, 'device')
        self.assertEqual(xml_of_subelement(device, 'FOO'), None)

    def test_dict_to_xml(self):
        expect = '<foo>&lt;testing escape characters&gt;</foo>'
        elements = dict_to_xml({
            'foo': '<testing escape characters>',
            'bar': 123,
            })
        text = [
            '<root xmlns="urn:schemas-upnp-org:device-1-0">',
            elements,
            '</root>'
        ]
        output = pformat_xml('\n'.join(text))
        self.assertIn(expect, output)

    def test_action_list(self):
        expect = {'GetProtocolInfo':
                  {'Source': {'direction': 'out',
                              'relatedStateVariable': 'SourceProtocolInfo'},
                   'Sink': {'direction': 'out',
                            'relatedStateVariable': 'SinkProtocolInfo'}}
                  }
        etree, namespace = upnp_org_etree(scpd())
        actions = scpd_actionlist(etree, namespace)
        self.assertEqual(actions, expect)

    def test_service_state(self):
        state_variable = """<stateVariable sendEvents="yes">
                              <name>FOO</name>
                              <dataType type="xsd:byte">string</dataType>
                            </stateVariable>
        """
        expect = {'SourceProtocolInfo': {'sendEvents': 'yes',
                                         'dataType': 'string'},
                  'SinkProtocolInfo': {'sendEvents': 'yes',
                                       'dataType': 'string'},
                  'CurrentPlayMode': {'sendEvents': 'no',
                                      'dataType': 'string',
                                      'allowedValueList': ['NORMAL'],
                                      'defaultValue': 'NORMAL'},
                  'NumberOfTracks': {'sendEvents': 'no',
                                     'dataType': 'ui4',
                                     'allowedValueRange': {'minimum': '0',
                                                           'maximum': '1'}},
                  'FOO': {'sendEvents': 'yes',
                          'dataType': 'string'},
                  }

        etree, namespace = upnp_org_etree(
                                    scpd(state_variable=state_variable))
        service_states = scpd_servicestatetable(etree, namespace)

        self.assertTrue(search_in_logs(self.logging_output, 'xml',
            re.compile("<stateVariable> 'FOO'.*attribute.*not supported")))
        self.assertEqual(service_states, expect)

    def test_soap_response(self):
        expect = {'CurrentTransportState': 'PLAYING'}
        action = 'GetTransportInfo'
        response = f"""
          <u:{action}Response
                xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
            <CurrentTransportState>PLAYING</CurrentTransportState>
          </u:GetTransportInfoResponse>
        """
        xml_string = soap_response(response)
        result = parse_soap_response(xml_string, action)
        self.assertEqual(result, expect)

    def test_empty_response(self):
        action = 'GetTransportInfo'
        response = f"""
          <u:DummyResponse
                xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
          </u:DummyResponse>
        """
        with self.assertRaises(UPnPXMLError) as cm:
            xml_string = soap_response(response)
            parse_soap_response(xml_string, action)
        self.assertIn(f"No '{action}Response' element", cm.exception.args[0])

    def test_soap_fault(self):
        expect = {'errorCode': '401',
                  'errorDescription': 'Invalid Action'}
        xml_string = soap_fault()
        fault = parse_soap_fault(xml_string)
        self.assertEqual(fault._asdict(), expect)

    def test_UPnPNamespace(self):
        with self.assertRaises(UPnPXMLError):
            UPnPNamespace(soap_fault(), 'urn:schemas-FOO-org:')

if __name__ == '__main__':
    unittest.main(verbosity=2)
