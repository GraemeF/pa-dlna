"""Command line tool for introspection and control of UPnP devices."""

import io
import cmd
import pprint
import functools
import logging
import asyncio
import threading
import textwrap
import traceback

from .init import padlna_main, UPnPApplication
from .upnp import (UPnPControlPoint, UPnPSoapFaultError,
                   UPnPClosedDeviceError, pformat_xml,
                   log_unhandled_exception, QUEUE_CLOSED)

logger = logging.getLogger('upnpcmd')
pprint_pprint = functools.partial(pprint.pprint, sort_dicts=False)

class MissingElementError(Exception): pass

# Utilities.
def _dedent(txt):
    """A dedent that does not use the first line to compute the margin.

    And that removes lines only made of space characters.
    """

    lines = txt.splitlines()
    return lines[0] + '\n' + textwrap.dedent('\n'.join(l for l in lines[1:] if
                                                       l == '' or l.strip()))

class DoMethod:
    # The implementation of generic do_* methods.

    def __init__(self, func, arg, doc=None):
        self.func = func
        self.arg = arg
        if doc is not None:
            self.__doc__ = doc

    def __call__(self, unused):
        return self.func(self.arg)

def build_commands_from(instance, obj, exclude=()):
    """Build do_* commands from 'obj'."""

    for key, value in vars(obj).items():
        if key.startswith('_') or key in exclude:
            continue
        funcname = f'do_{key}'
        if not hasattr(instance, funcname):
            setattr(instance, funcname, DoMethod(print, value,
                                            f"Print the value of '{key}'."))

def check_required(obj, attributes):
    """Check that all in 'attributes' are attributes of 'obj'."""

    for name in attributes:
        if not hasattr(obj, name):
            msg = ''
            if hasattr(obj, 'peer_ipaddress'):
                msg = f' at {obj.peer_ipaddress}'
            raise MissingElementError(f"Missing '{name}' xml element in"
                            f" description of '{str(obj)}'{msg}")

def device_name(dev):
    attr = 'friendlyName'
    return getattr(dev, attr) if hasattr(dev, attr) else str(dev)

def comma_split(txt):
    """Split 'txt' on commas handling backslash escaped commas."""

    escaped = None
    result = []
    for line in txt.split(','):
        if escaped:
            line = escaped + line
            escaped = None
        if line.endswith('\\'):
            escaped = line[:-1] + ','
        else:
            result.append(line.strip())
    return result

def pprint_soap(response):
    """Pretty print an soap response."""

    if not response:
        print('SOAP response OK')
        return

    for arg, value in response.items():
        # A comma separated list becomes a Python list.
        if isinstance(value, str):
            splitted = comma_split(value)
            if len(splitted) > 1:
                response[arg] = splitted
            else:
                try:
                    response[arg] = int(value)
                except ValueError:
                    pass
    print('SOAP response:')
    pprint_pprint(response)

# Class(es).
class _Cmd(cmd.Cmd):
    def __init__(self):
        super().__init__()

    def select_device(self, devices, idx):
        """Select a device in a list and print some device attributes."""

        if not devices:
            print('*** No device')
            return

        try:
            for dev in devices:
                check_required(dev, ('deviceType', 'UDN'))
        except MissingElementError as e:
            print(f'*** {e.args[0]}')
            return

        idx = 0 if idx == '' else idx
        try:
            idx = int(idx)
            dev = devices[idx]
            print('Selected device:')
            print('  friendlyName:', device_name(dev))
            print('  deviceType:', dev.deviceType)
            print('  UDN:', dev.UDN)
            print()
        except Exception as e:
            print(f'*** {e!r}')
        else:
            return  dev

    def do_EOF(self, unused):
        """Quit the application."""
        print()
        return self.do_quit(unused)

    def get_names(self):
        return dir(self)

    def do_help(self, arg):
        if not arg:
            self.stdout.write(_dedent(self.__class__.__doc__))
        super().do_help(arg)

    do_help.__doc__ = cmd.Cmd.do_help.__doc__

    def get_help(self):
        """Return the help as a string."""

        _stdout = self.stdout
        try:
            with io.StringIO() as out:
                self.stdout = out
                self.do_help(None)
                return out.getvalue()
        finally:
            self.stdout = _stdout

    def emptyline(self):
        """Do not run the last command."""

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except Exception:
            traceback.print_exc(limit=-10)

    def cmdloop(self):
        super().cmdloop(intro=self.get_help())

class ActionCommand(cmd.Cmd):
    """Cmd interpreter used to prompt for an argument."""

    def __init__(self, argument):
        super().__init__()
        self.prompt = f'  {argument} =  '
        self.result = None

    def complete(self, text, state):
        return None

    def cmdloop(self, intro=None):
        # Disable history.
        try:
            if self.use_rawinput and self.completekey:
                try:
                    import readline
                    readline.set_auto_history(False)
                except ImportError:
                    pass
            super().cmdloop(intro)
        finally:
            if self.use_rawinput and self.completekey:
                try:
                    import readline
                    readline.set_auto_history(True)
                except ImportError:
                    pass

    def onecmd(self, line):
        line = line.strip()
        if line:
            self.result = line
            return True

class UPnPServiceCmd(_Cmd):
    """Use the 'previous' command to return to the device.
    Enter an action command to be prompted for the value of each of
    its arguments. Type <Ctl-D> to abort entering those values.

    """

    def __init__(self, upnp_service, loop):
        super().__init__()
        self.upnp_service = upnp_service
        self.loop = loop
        build_commands_from(self, upnp_service)

        # Build the do_* action commands.
        self.doc_header = 'Main commands:'
        self.undoc_header = 'Action commands:'
        for action in self.upnp_service.actionList:
            funcname = f'do_{action}'
            setattr(self, funcname, DoMethod(self.soap_action, action))

        self.prompt = f'[{str(self.upnp_service)}] '
        self.quit = False

    def do_quit(self, unused):
        """Quit the application."""

        # Tell cmdloop() to return True.
        self.quit = True
        # Stop the current interpreter and return to the previous one.
        return True

    def do_previous(self, unused):
        """Return to the device."""
        return True

    def help_parent_device(self):
        print('Shortened UDN of the parent device.')

    def do_description(self, unused):
        """Print the xml 'description'."""
        print(pformat_xml(self.upnp_service.description))

    def help_root_device(self):
        print('Shortened UDN of the root device.')

    def help_actionList(self):
        print(_dedent("""Print an action or list actions.

        With a numeric argument such as 'actionList DEPTH':
          When DEPTH is 1, print the list of the actions.
          When DEPTH is 2, print the list of the actions with their arguments.
          When DEPTH is 3, print the list of the actions with their arguments
            and the values of 'direction' and 'relatedStateVariable' for each
            argument.
        With no argument, it is the same as 'actionList 1'.
        With the action name as argument, print the full description of the
        action.

        Completion is enabled on the action names.

        """))

    def complete_actionList(self, text, line, begidx, endidx):
        return [a for a in self.upnp_service.actionList if a.startswith(text)]

    def do_actionList(self, arg):
        depth = None
        if arg == '':
            depth = 1
        else:
            try:
                depth = int(arg)
                if depth <= 0 or depth > 3:
                    print('*** Depth must be > 0 and < 4')
                    return
            except ValueError:
                pass

        if depth is not None:
            pprint_pprint(self.upnp_service.actionList, depth=depth)
        else:
            try:
                action = {arg: self.upnp_service.actionList[arg]}
                pprint_pprint(action)
            except KeyError:
                print(f"*** '{arg}' is not an action")

    def help_serviceStateTable(self):
        print(_dedent("""Print a stateVariable or list the stateVariables.

        With a numeric argument such as 'serviceStateTable DEPTH':
          When DEPTH is 1, print the list of the stateVariables.
          When DEPTH is 2, print the list of the stateVariables with their
            parameters.
          When DEPTH is 3, print also the list of the 'allowedValueList' or
           'allowedValuerange' parameter if any.

        With no argument, it is the same as 'serviceStateTable 1'.
        With the stateVariable name as argument, print the full description of
        the stateVariable.

        Completion is enabled on the stateVariable names.

        """))

    def complete_serviceStateTable(self, text, line, begidx, endidx):
        return [s for s in self.upnp_service.serviceStateTable if
                s.startswith(text)]

    def do_serviceStateTable(self, arg):
        depth = None
        if arg == '':
            depth = 1
        else:
            try:
                depth = int(arg)
                if depth <= 0 or depth > 3:
                    print('*** Depth must be > 0 and < 4')
                    return
            except ValueError:
                pass

        if depth is not None:
            pprint_pprint(self.upnp_service.serviceStateTable, depth=depth)
        else:
            try:
                action = {arg: self.upnp_service.serviceStateTable[arg]}
                pprint_pprint(action)
            except KeyError:
                print(f"*** '{arg}' is not an action")

    def soap_action(self, action):
        """Invoke a soap action on asyncio event loop from this thread."""

        if self.loop is None or self.loop.is_closed():
            print('*** The control point is closed')
            return

        args = {}
        for arg, params in self.upnp_service.actionList[action].items():
            if params['direction'] == 'in':
                cmd = ActionCommand(arg)
                cmd.cmdloop()
                if cmd.result == 'EOF':
                    print('*** Action interrupted')
                    return
                args[arg] = cmd.result

        future = asyncio.run_coroutine_threadsafe(
                            self.upnp_service.soap_action(action, args),
                            self.loop)
        try:
            response = future.result()
            pprint_soap(response)
        except UPnPSoapFaultError as e:
            print(f'*** Fault {e.args[0]}')
        except UPnPClosedDeviceError:
            print(f'*** {self.upnp_service.root_device} is closed')
        except Exception as e:
            print(f'*** Got exception {e!r}')

    def cmdloop(self):
        super().cmdloop()
        # Tell the previous interpreter to just quit when self.quit is True.
        return self.quit

class UPnPDeviceCmd(_Cmd):
    """Use the 'embedded' or 'service' command to select an embedded device or
    service. Use the 'previous' command to return to the previous device or to
    the control point.

    """

    def __init__(self, upnp_device, loop):
        super().__init__()
        self.upnp_device = upnp_device
        self.loop = loop
        build_commands_from(self, upnp_device,
                            exclude=('deviceList', 'serviceList'))
        self.prompt = f'[{device_name(upnp_device)}] '
        self.quit = False

    def do_quit(self, unused):
        """Quit the application."""

        # Tell cmdloop() to return True.
        self.quit = True
        # Stop the current interpreter and return to the previous one.
        return True

    def do_embedded_list(self, unused):
        """List the embedded UPnP devices"""
        print([device_name(dev) for dev in self.upnp_device.deviceList])

    def help_embedded(self):
        print(_dedent(f"""Select an embedded device.

        Use the command 'embedded IDX' to select the device at index IDX
        (starting at zero) in the list printed by the 'embedded_list' command.
        With no argument, do this for the device at index 0.

        """))

    def do_embedded(self, idx):
        selected = self.select_device(self.upnp_device.deviceList, idx)
        if selected is not None:
            interpreter = UPnPDeviceCmd(selected, self.loop)
            if interpreter.cmdloop():
                return self.do_quit(None)

    def do_service_list(self, unused):
        """List the services."""
        print([str(serv) for serv in self.upnp_device.serviceList.values()])

    def complete_service(self, text, line, begidx, endidx):
        return [s for s in
                (str(serv) for serv in self.upnp_device.serviceList.values())
                    if s.startswith(text)]

    def help_service(self):
        print(_dedent("""Select a service.

        Use the command 'service NAME' to select the service named NAME.
        Completion is enabled on the service names.

        """))

    def do_service(self, arg):
        services = list(self.upnp_device.serviceList.values())

        if not services:
            print('*** No service')
            return

        try:
            for serv in services:
                check_required(serv, ('serviceType', 'serviceId'))
        except MissingElementError as e:
            print(f'*** {e.args[0]}')
            return

        for serv in services:
            str_serv = str(serv)
            if str_serv == arg:
                print('Selected service:')
                print('  serviceId:', str_serv)
                print('  serviceType:', serv.serviceType)
                print()
                break
        else:
            print(f"*** Unkown service '{arg}'")
            return

        interpreter = UPnPServiceCmd(serv, self.loop)
        if interpreter.cmdloop():
            return self.do_quit(None)

    def help_previous(self):
        if self.upnp_device.parent_device is self.upnp_device.root_device:
            print('Return to the control point.')
        else:
            print('Return to the previous device.')

    def do_previous(self, unused):
        return True

    def help_peer_ipaddress(self):
        print('Print the IP address of the UPnP device.')

    def help_parent_device(self):
        print('Shortened UDN of the parent device.')

    def do_description(self, unused):
        """Print the xml 'description'."""
        print(pformat_xml(self.upnp_device.description))

    def do_iconList(self, unused):
        """Print the value of 'iconList'."""

        device = self.upnp_device
        if hasattr(device, 'iconList'):
            pprint_pprint(device.iconList, indent=2)
        else:
            print('None')

    def help_root_device(self):
        print('Shortened UDN of the root device.')

    def cmdloop(self):
        super().cmdloop()
        # Tell the previous interpreter to just quit when self.quit is True.
        return self.quit

class UPnPControlCmd(UPnPApplication, _Cmd):
    """Interactive interface to an UPnP control point

    List available commands with 'help' or '?'. List detailed help with
    'help COMMAND'. Use tab completion and command history when the readline
    Python module is available. Type 'quit' or <Ctl-D> here or at each
    sub-menu to quit the session.

    Use the 'device' command to select a device among the discovered devices.

    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        super(UPnPApplication, self).__init__()

        # Control point attributes.
        self.loop = None
        self.control_point = None
        self.cp_thread = None
        self.devices = set()

        # Cmd attributes.
        self.prompt = '[Control Point] '

    def do_quit(self, unused):
        """Quit the application."""
        self.close()
        return True

    def do_device_list(self, unused):
        """List the discovered UPnP devices."""
        print([device_name(dev) for dev in self.devices])

    def help_device(self):
        print(_dedent(f"""Select a discovered device.

        Use the command 'device IDX' to select the device at index IDX
        (starting at zero) in the list printed by the 'device_list' command.
        With no argument, do this for the device at index 0.

        """))

    def do_device(self, idx):
        dev_list = list(self.devices)
        selected = self.select_device(dev_list, idx)
        if selected is not None:
            interpreter = UPnPDeviceCmd(selected, self.loop)
            if interpreter.cmdloop():
                self.close()
                return True

    def help_ip_configured (self):
        print(_dedent("""Print the list of the configured IPv4 addresses of
        the networks where UPnP devices may be discovered. All addresses may
        be monitored when both 'ip_configured' and 'nics' are empty.
        """))

    def help_nics(self):
        print(_dedent("""Print the list of the network interfaces where UPnP
        devices may be discovered. All IPv4 addresses may be monitored when
        both 'ip_configured' and 'nics' are empty.
        """))

    def help_ip_monitored(self):
        print(_dedent("""Print the list of the IPv4 addresses currently
        monitored by UPnP discovery.
        """))

    def help_ttl(self):
        print('Print the IP packets time to live.')

    def close(self):
        if self.control_point is not None:
            if threading.current_thread() is not self.cp_thread:
                if self.loop is not None and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.control_point.close)
                self.cp_thread.join(timeout=5)
            else:
                self.control_point.close()

    def run(self, cp_thread, event):
        self.cp_thread = cp_thread
        event.wait()
        build_commands_from(self, self.control_point)
        try:
            self.cmdloop()
        except KeyboardInterrupt as e:
            print(f'Got {e!r}')
            self.close()
            return 1

    @log_unhandled_exception(logger)
    async def run_control_point(self, event):
        self.loop = asyncio.get_running_loop()
        try:
            # Run the UPnP control point.
            with UPnPControlPoint(
                    self.ip_addresses, self.nics, self.msearch_interval,
                    self.msearch_port, self.ttl) as self.control_point:
                event.set()
                while True:
                    notif, root_device = (await
                                          self.control_point.get_notification())
                    if (notif, root_device) == QUEUE_CLOSED:
                        logger.debug('UPnP queue is closed')
                        break
                    logger.info(f"Got '{notif}' notification for "
                                f' {root_device}')
                    if notif == 'alive':
                        self.devices.add(root_device)
                    elif root_device in self.devices:
                        self.devices.remove(root_device)
        finally:
            self.close()

    def __str__(self):
        return 'upnp-cmd'

# The main function.
def main():
    padlna_main(UPnPControlCmd, __doc__)

if __name__ == '__main__':
    padlna_main(UPnPControlCmd, __doc__)
