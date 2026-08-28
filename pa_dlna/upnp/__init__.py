# UPnP exceptions.
class UPnPError(Exception): pass

# TEST_LOGLEVEL is below logging.DEBUG and is only used by the test suite.
TEST_LOGLEVEL = 5

# All exported objects.
from .upnp import (UPnPClosedDeviceError, UPnPInvalidSoapError,
                   UPnPSoapFaultError,
                   UPnPControlPoint, UPnPRootDevice, UPnPDevice, UPnPService,
                   QUEUE_CLOSED)
from .network import ipaddr_from_nics, UPnPNoHTTPResponseError
from .util import (NL_INDENT, shorten, AsyncioTasks, log_exception,
                   log_unhandled_exception)
from .xml import UPnPXMLError, pformat_xml, xml_escape
