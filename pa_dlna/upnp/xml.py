"""XML utilities."""

import io
import functools
import logging
import collections
import xml.etree.ElementTree as ET

from . import UPnPError

logger = logging.getLogger('xml')

UPNP_NAMESPACE_BEG = 'urn:schemas-upnp-org:'
ENVELOPE_NAMESPACE_BEG = "http://schemas.xmlsoap.org/soap/envelope"
RESP_NAMESPACE_BEG = "urn:schemas-upnp-org:service:"
CTRL_NAMESPACE_BEG = "urn:schemas-upnp-org:control"

class UPnPXMLError(UPnPError): pass

# XML helper functions.
@functools.cache
def namespace_as_dict(xml):
    return dict(reversed(elem) for (event, elem) in ET.iterparse(
            io.StringIO(xml), events=['start-ns']))

def upnp_org_etree(xml):
    """Return the element tree and UPnP namespace from an xml string."""

    upnp_namespace = UPnPNamespace(xml, UPNP_NAMESPACE_BEG)
    return ET.fromstring(xml), upnp_namespace

def build_etree(element):
    """Build an element tree to a bytes sequence and return it as a string."""

    etree = ET.ElementTree(element)
    with io.BytesIO() as output:
        etree.write(output, encoding='utf-8', xml_declaration=True)
        return output.getvalue().decode()

def xml_of_subelement(xml, tag):
    """Return the first 'tag' subelement as an xml string."""

    # Find the 'tag' subelement.
    root, namespace = upnp_org_etree(xml)
    element = root.find(f'{namespace!r}{tag}')

    if element is None:
        return None
    return build_etree(element)

def findall_childless(etree, namespace):
    """Return the dictionary {tag: text} of all chidless subelements."""

    d = {}
    ns_len = len(f'{namespace!r}')
    for e in etree.findall(f'.{namespace!r}*'):
        if e.tag and len(list(e)) == 0:
            tag = e.tag[ns_len:]
            d[tag] = e.text
    return d

def scpd_actionlist(scpd, namespace):
    """Parse the scpd element for 'actionList'."""

    result = {}
    actionList = scpd.find(f'{namespace!r}actionList')
    if actionList is not None:
        for action in actionList:
            action_name = args = None
            for e in action:
                if e.tag == f'{namespace!r}name':
                    action_name = e.text
                elif e.tag == f'{namespace!r}argumentList':
                    args = {}
                    for argument in e:
                        d = findall_childless(argument, namespace)
                        name = d['name']
                        del d['name']
                        args[name] = d
            # Silently ignore malformed actions.
            if action_name is not None and args is not None:
                result[action_name] = args
    return result

def scpd_servicestatetable(scpd, namespace):
    """Parse the scpd element for 'serviceStateTable'."""

    result = {}
    table = scpd.find(f'{namespace!r}serviceStateTable')
    if table is not None:
        for variable in table:
            varname = None
            params = {}
            has_type_attr = False
            for attr in ('sendEvents', 'multicast'):
                val = variable.attrib.get(attr)
                if val is not None:
                    params[attr] = val
            for e in variable:
                if e.tag == f'{namespace!r}name':
                    varname = e.text
                elif e.tag == f'{namespace!r}dataType':
                    if 'type' in e.attrib:
                        has_type_attr = True
                    params['dataType'] = e.text
                elif e.tag == f'{namespace!r}defaultValue':
                    params['defaultValue'] = e.text
                elif e.tag == f'{namespace!r}allowedValueList':
                    allowed_list = []
                    for allowed in e:
                        allowed_list.append(allowed.text)
                    params['allowedValueList'] = allowed_list
                elif e.tag == f'{namespace!r}allowedValueRange':
                    ns_len = len(f'{namespace!r}')
                    val_range = {}
                    for limit in e:
                        if limit.tag in (f'{namespace!r}{x}' for
                                     x in ('minimum', 'maximum', 'step')):
                            tag = limit.tag[ns_len:]
                            val_range[tag] = limit.text
                    params['allowedValueRange'] = val_range
            if varname is not None:
                if has_type_attr:
                    logger.warning(f"<stateVariable> '{varname}': 'type'"
                                   ' attribute of <dataType> not supported')
                result[varname] = params
    return result

def xml_escape(txt):
    txt = txt.replace('&',  '&amp;')
    txt = txt.replace('<',  '&lt;')
    txt = txt.replace('>',  '&gt;')
    txt = txt.replace("\"", '&quot;')
    txt = txt.replace('\'', '&apos;')
    return txt

def dict_to_xml(arguments):
    """Build an xml string from a dict."""

    xml = []
    for tag, element in arguments.items():
        # Escape control chars in xml elements.
        if isinstance(element, str):
            element = xml_escape(element)
        xml.append(f'<{tag}>{element}</{tag}>')

    return '\n'.join(xml)

def parse_soap_response(xml, action):
    # Find the 'Body' subelement.
    env_namespace = UPnPNamespace(xml, ENVELOPE_NAMESPACE_BEG)
    root = ET.fromstring(xml)
    body = root.find(f'{env_namespace!r}Body')
    if body is None:
        raise UPnPXMLError("No 'Body' element in SOAP response")

    # Find the response subelement.
    resp_namespace = UPnPNamespace(xml, RESP_NAMESPACE_BEG)
    resp = body.find(f'{resp_namespace!r}{action}Response')
    if resp is None:
        raise UPnPXMLError(f"No '{action}Response' element in"
                           f' SOAP response')
    return dict((e.tag, e.text) for e in resp)

def parse_soap_fault(xml):
    # Find the 'Fault' subelement.
    env_namespace = UPnPNamespace(xml, ENVELOPE_NAMESPACE_BEG)
    root = ET.fromstring(xml)
    fault = root.find(f'.//{env_namespace!r}Fault')
    if fault is None:
        raise UPnPXMLError("No 'Fault' element in SOAP fault response")

    # Find the 'UPnPError' subelement.
    ctrl_namespace = UPnPNamespace(xml, CTRL_NAMESPACE_BEG)
    error = fault.find(f'.//{ctrl_namespace!r}UPnPError')
    if error is None:
        raise UPnPXMLError("No 'UPnPError' element in SOAP fault response")

    ns_len = len(f'{ctrl_namespace!r}')
    d = dict((e.tag[ns_len:], e.text) for e in error)
    return SoapFault(**d)

def pformat_xml(xml):
    """Pretty format an UPnP xml string."""

    root, namespace = upnp_org_etree(xml)
    ET.register_namespace('', str(namespace))
    tree = ET.ElementTree(root)
    ET.indent(tree)

    with io.StringIO() as out:
        tree.write(out, encoding='unicode')
        return out.getvalue()

# Helper classes.
SoapFault = collections.namedtuple('SoapFault', 'errorCode errorDescription',
                                   defaults=['Not specified'])
class UPnPNamespace:
    """A namespace uri."""

    def __init__(self, xml, prefix):
        """The first namespace in 'xml' starting with 'prefix'."""

        ns = namespace_as_dict(xml)
        for uri in ns:
            if uri.startswith(prefix):
                self.uri = uri
                return
        raise UPnPXMLError(f'No namespace starting with {prefix}')

    def __str__(self):
        return self.uri

    def __repr__(self):
        return f'{{{self.uri}}}' if self.uri else ''
